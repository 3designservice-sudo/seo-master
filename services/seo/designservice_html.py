"""Final HTML renderer for designservice.group blog articles.

The renderer takes:
  - article: integrations.designservice.Article (from _bot_api.php roadmap)
  - body_html: AI-generated article body (without <html>/<head>/<body>)
  - cover_url: optional absolute URL for og:image / hero image

And produces full HTML document with all SEO obvious:
  - <head>: title (≤65 chars), description (140-180 chars), canonical, og:*, twitter:*
  - 3 ld+json: BlogPosting, BreadcrumbList, FAQPage (last only if FAQ present)
  - <body>: visible h1 in SSR, breadcrumbs, article-cover img, body content,
    author block with E-E-A-T signals, cross-links to /services/* and /blog.html

Design choices:
  - No React/JS dependency in the rendered HTML — pure SSR for Yandex/Google bots.
  - Inline minimal CSS (.art-* classes) so article looks readable even before
    main /css/article.css loads.
  - kw_primary mention count and structure follow on-page SEO checklist
    (kw 5-15 times in body, h2 sections 100-300 words each).
"""

from __future__ import annotations

import html as html_lib
import json
import re
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from integrations.designservice.models import Article

_DEFAULT_AUTHOR = {
    "name": "Александр Шульман",
    "url": "https://designservice.group/about.html",
    "jobTitle": "Директор ООО «Дизайн-Сервис»",
    "initials": "АШ",
    "avatar": "/img/author-shulman.webp",
}

# DS_FOOTER_v1: SSR footer mirroring shared.js ContactFooter+PageFooter components.
# Injected inside <div id=root>; mount script passes hideFooter:true so PageShell
# doesn't render a second copy on JS-enabled clients.
_SSR_FOOTER_HTML = (
    '<div class="ds-ssr-footer-v1">'
    '<div class="contact-footer">'
    '<div class="contact-footer-left">'
    '<h3>Начнём ваш проект?</h3>'
    '<p>Первая консультация бесплатно. Приедем, обмеряем, посчитаем.</p>'
    '</div>'
    '<div class="contact-footer-right">'
    '<div class="cf-item"><div class="cf-label">Телефон</div>'
    '<a href="tel:+79780223222">+7 (978) 022-32-22</a></div>'
    '<div class="cf-item"><div class="cf-label">Email</div>'
    '<a href="mailto:info@designservice.group">info@designservice.group</a></div>'
    '<div class="cf-item"><div class="cf-label">Telegram</div>'
    '<a href="https://t.me/designservice_group" target="_blank">@designservice</a></div>'
    '</div></div>'
    '<footer class="page-footer">'
    '<span>2015 — 2026 © ООО «Дизайн-Сервис». Все права защищены.</span>'
    '<div style="display:flex;gap:16px">'
    '<a href="/privacy.html">Политика конфиденциальности</a>'
    '<a href="/terms.html">Пользовательское соглашение</a>'
    '</div></footer></div>'
)

_PUBLISHER = {
    "name": "Дизайн-Сервис",
    "url": "https://designservice.group",
    "logo": "https://designservice.group/Logo_DS.png",
}

# Cross-links pool — appended at the bottom of every article.
_CROSS_LINKS = [
    ("/services/design.html", "Дизайн интерьера"),
    ("/services/renovation.html", "Ремонт квартир"),
    ("/services/architecture.html", "Архитектурное проектирование"),
    ("/services/construction.html", "Строительство домов"),
    ("/services/supervision.html", "Авторский надзор"),
    ("/projects.html", "Реализованные проекты"),
    ("/reviews.html", "Отзывы клиентов"),
    ("/contacts.html", "Связаться со студией"),
]


def _escape(text: str) -> str:
    """HTML-escape for attribute values and visible text."""
    return html_lib.escape(text or "", quote=True)


def _json_ld_script(payload: dict) -> str:
    """Compact JSON serialization with ensure_ascii=False for Cyrillic."""
    body = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    return f'<script type="application/ld+json">{body}</script>'


def _build_blogposting_ld(article: "Article", url: str, cover_url: str, date_iso: str) -> dict:
    """Schema.org BlogPosting (or Article if no faq)."""
    return {
        "@context": "https://schema.org",
        "@type": "BlogPosting",
        "headline": article.h1,
        "description": article.meta_description,
        "url": url,
        "image": cover_url,
        "datePublished": date_iso,
        "dateModified": date_iso,
        "author": {
            "@type": "Person",
            "name": _DEFAULT_AUTHOR["name"],
            "url": _DEFAULT_AUTHOR["url"],
            "jobTitle": _DEFAULT_AUTHOR["jobTitle"],
        },
        "publisher": {
            "@type": "Organization",
            "name": _PUBLISHER["name"],
            "url": _PUBLISHER["url"],
            "logo": {"@type": "ImageObject", "url": _PUBLISHER["logo"]},
        },
        "mainEntityOfPage": url,
        "articleSection": article.service_label or "Блог",
        "keywords": article.kw_primary,
    }


def _build_breadcrumb_ld(article: "Article", url: str, base_url: str) -> dict:
    items = [
        {"@type": "ListItem", "position": 1, "name": "Главная", "item": f"{base_url}/"},
        {"@type": "ListItem", "position": 2, "name": "Блог", "item": f"{base_url}/blog.html"},
    ]
    pos = 3
    if article.service_label and article.service_url:
        items.append(
            {
                "@type": "ListItem",
                "position": pos,
                "name": article.service_label,
                "item": f"{base_url}{article.service_url}",
            }
        )
        pos += 1
    items.append(
        {"@type": "ListItem", "position": pos, "name": article.h1, "item": url}
    )
    return {
        "@context": "https://schema.org",
        "@type": "BreadcrumbList",
        "itemListElement": items,
    }


def _extract_faq_from_body(body_html: str) -> list[tuple[str, str]]:
    """Parse <details><summary>Q</summary><p>A</p></details> blocks.

    Returns list of (question, answer) tuples. Robust to nesting and to
    plaintext or wrapped <p> answer.
    """
    out: list[tuple[str, str]] = []
    # Capture summary (question) + everything until </details>
    pat = re.compile(
        r"<details[^>]*>\s*<summary[^>]*>(.+?)</summary>\s*(.*?)\s*</details>",
        re.IGNORECASE | re.DOTALL,
    )
    for m in pat.finditer(body_html):
        q_raw = m.group(1)
        a_raw = m.group(2)
        # Strip inner tags for plain Q/A text
        q = re.sub(r"<[^>]+>", "", q_raw).strip()
        a = re.sub(r"<[^>]+>", " ", a_raw).strip()
        a = re.sub(r"\s+", " ", a)
        if q and a:
            out.append((q, a))
    return out


def _build_faq_ld(faq_items: list[tuple[str, str]]) -> dict:
    return {
        "@context": "https://schema.org",
        "@type": "FAQPage",
        "mainEntity": [
            {
                "@type": "Question",
                "name": q,
                "acceptedAnswer": {"@type": "Answer", "text": a},
            }
            for q, a in faq_items
        ],
    }


def _format_date_ru(date_iso: str) -> str:
    """Convert '2026-05-19' → '19 мая 2026 г.' for visible date in article meta."""
    if not date_iso or len(date_iso) < 10:
        return ""
    try:
        y, m, d = date_iso[:10].split("-")
    except ValueError:
        return date_iso
    months = ["", "января", "февраля", "марта", "апреля", "мая", "июня",
              "июля", "августа", "сентября", "октября", "ноября", "декабря"]
    try:
        return f"{int(d)} {months[int(m)]} {y} г."
    except (ValueError, IndexError):
        return date_iso


def _reading_minutes(body_html: str) -> int:
    """Estimate reading time at ~180 words/minute (Russian average)."""
    plain = re.sub(r"<[^>]+>", " ", body_html)
    words = len(re.findall(r"[А-Яа-яA-Za-zЁё0-9]+", plain))
    minutes = max(1, round(words / 180))
    return minutes


def _ensure_description_length(desc: str, h1: str, kw_primary: str = "") -> str:
    """Ensure meta description is 100-180 chars AND contains Yoast focus keyword
    (first 2-3 content words of h1).

    Strategy:
      1. If empty → seed from h1 + studio info
      2. If yoast_kw NOT in desc → prepend 'h1_prefix — ' or append
      3. If < 100 → append context tail
      4. If 100-180 and yoast_kw present → return unchanged
      5. If > 180 → truncate at sentence boundary
    """
    # Compute Yoast focus keyword: first 2-3 content words of h1 (>=3 chars)
    h1_content_words = [w for w in (h1 or "").split() if len(w) >= 3]
    yoast_kw = " ".join(h1_content_words[:2]) if len(h1_content_words) >= 2 else ""

    if not desc:
        desc = f"{h1}. Опыт студии Дизайн-Сервис в Крыму с 1997 года, 320+ проектов."
    desc = desc.strip()

    # PR 33: GUARANTEE yoast_kw is in description — prepend if missing
    if yoast_kw and yoast_kw.lower() not in desc.lower():
        prefix = f"{yoast_kw}: "
        # If desc already long, replace start with prefix + sentence reset
        if len(desc) >= 100:
            # Truncate desc to keep room for prefix, ensure 100-180 final
            max_orig = 180 - len(prefix)
            desc = desc[:max_orig].rstrip()
            # Try to end at sentence boundary
            for sep in (". ", "? ", "! "):
                idx = desc.rfind(sep)
                if idx > 60:
                    desc = desc[:idx + 1]
                    break
        desc = prefix + desc

    if 100 <= len(desc) <= 180:
        return desc
    if len(desc) < 100:
        # Include first 2-3 words of h1 in description for Yoast focus-keyword match
        h1_words = [w for w in (h1 or "").split() if w]
        h1_prefix = " ".join(h1_words[:3]) if len(h1_words) >= 2 else ""
        tail_options = [
            f" {h1_prefix} — гид от студии Дизайн-Сервис в Крыму с 1997 года." if h1_prefix else " Опыт студии Дизайн-Сервис в Крыму с 1997 года.",
            f" Опыт студии Дизайн-Сервис в Крыму с 1997 года, 320+ проектов.",
            f" Подробный гид от студии Дизайн-Сервис.",
            f" Студия Дизайн-Сервис — 27 лет в Крыму, 320+ проектов.",
        ]
        for tail in tail_options:
            extended = desc + tail
            if 100 <= len(extended) <= 180:
                return extended
        # Fallback: take longest combination that fits
        for tail in tail_options:
            extended = desc + tail
            if len(extended) <= 180:
                return extended if len(extended) >= 100 else extended + " Крым."
        return desc
    # > 180 — truncate at sentence
    for sep in (". ", "! ", "? "):
        idx = desc.rfind(sep, 0, 178)
        if idx > 100:
            return desc[:idx + 1].rstrip()
    # Fallback: hard truncate at word
    truncated = desc[:177].rsplit(" ", 1)[0] + "..."
    return truncated


def _author_block_html() -> str:
    return f"""<section class="art-author-block" style="margin-top:48px;padding:32px;background:var(--bg2,#eae6df);border-radius:16px">
<h2 style="margin-top:0">Об авторе</h2>
<p><strong>{_escape(_DEFAULT_AUTHOR["name"])}</strong> — директор и ведущий дизайнер ООО «Дизайн-Сервис». Работает в Крыму с 1997 года. За 27 лет студия выполнила более 320 проектов — от квартир под сдачу в новостройках Симферополя до частных домов на южном берегу Крыма. <a href="{_DEFAULT_AUTHOR["url"]}">Подробнее об авторе</a>.</p>
</section>"""


def inject_yoast_keyword(body_html: str, h1: str) -> str:
    """Ensure the Yoast focus keyword (first 2 content words of h1) appears in:
      - first <p class="art-lead">
      - at least one <h2>
    If LLM didn't include them, do a soft inject. This guarantees the Yoast
    widget checks pass without depending on LLM compliance.

    Description is handled separately by _ensure_description_length.
    """
    if not body_html or not h1:
        return body_html

    # Compute focus keyword: first 2 content words of h1 (length >= 3)
    words = [w for w in re.findall(r"[А-Яа-яA-Za-zЁё0-9]+", h1) if len(w) >= 3]
    if len(words) < 2:
        return body_html
    yoast_kw = " ".join(words[:2])
    yoast_kw_lower = yoast_kw.lower()

    # 1. Check art-lead — inject if missing
    lead_pattern = re.compile(
        r'(<p[^>]*class="[^"]*art-lead[^"]*"[^>]*>)(.+?)(</p>)',
        re.IGNORECASE | re.DOTALL,
    )
    m = lead_pattern.search(body_html)
    if m:
        lead_inner = m.group(2)
        if yoast_kw_lower not in re.sub(r"<[^>]+>", " ", lead_inner).lower():
            # Inject as opening: "Yoast_KW — original lead"
            new_inner = f"{yoast_kw} — {lead_inner.lstrip()}"
            body_html = body_html[: m.start()] + m.group(1) + new_inner + m.group(3) + body_html[m.end():]

    # 2. Check h2 — find first h2, inject if yoast_kw is absent in ALL h2
    h2_pattern = re.compile(r"(<h2[^>]*>)(.+?)(</h2>)", re.IGNORECASE | re.DOTALL)
    all_h2 = list(h2_pattern.finditer(body_html))
    if all_h2:
        any_h2_has_kw = any(
            yoast_kw_lower in re.sub(r"<[^>]+>", " ", m.group(2)).lower()
            for m in all_h2
        )
        if not any_h2_has_kw:
            # Inject yoast_kw as prefix of FIRST h2
            first = all_h2[0]
            first_inner = first.group(2)
            new_h2_inner = f"{yoast_kw}: {first_inner.lstrip()}"
            body_html = (
                body_html[: first.start()]
                + first.group(1) + new_h2_inner + first.group(3)
                + body_html[first.end():]
            )

    return body_html


def _read_more_html(items: list[dict] | None) -> str:
    """Build «Читать дальше» block with up to 3 article-card previews.

    Uses the SAME .article-card / .article-card-cover / .article-card-body /
    .article-card-cat / .article-card-meta classes as /blog.html so styling
    is identical (rounded, hover shadow, accent category, Playfair title).

    Args:
        items: list of dicts with keys:
            h1 (required), published_url, cover_url, published_date,
            excerpt (meta_description, optional), category (optional),
            reading_time (optional, e.g. "10 мин")

    Returns empty string if no items.
    """
    if not items:
        return ""
    cards = []
    for item in items[:3]:
        h1 = _escape(item.get("h1", ""))[:120]
        url = item.get("published_url", "")
        cover = item.get("cover_url") or "/Logo_DS.png"
        date_ru = _format_date_ru(item.get("published_date", ""))
        excerpt = _escape((item.get("excerpt") or "")[:200])
        category = _escape(item.get("category") or "Блог")
        reading_time = _escape(item.get("reading_time") or "")
        meta_html = f'<span>{date_ru}</span><span class="dot">•</span><span>{reading_time}</span>' if reading_time else f'<span>{date_ru}</span>'
        cards.append(
            f'<a class="article-card" href="{url}">'
            f'<div class="article-card-cover" style="background-image:url(\'' + cover + '\')"></div>'
            f'<div class="article-card-body">'
            f'<div class="article-card-cat">{category}</div>'
            f'<h3>{h1}</h3>'
            f'<p>{excerpt}</p>'
            f'<div class="article-card-meta">{meta_html}</div>'
            f'</div>'
            f'</a>'
        )
    return (
        '<section class="art-read-more section" style="margin-top:48px;padding:0">'
        '<h2 class="section-title" style="font-family:Playfair Display,serif;font-size:30px;margin:0 0 14px;font-weight:500">Читать дальше</h2>'
        '<div class="article-grid" style="display:grid;grid-template-columns:repeat(auto-fill,minmax(280px,1fr));gap:20px;margin-top:24px">'
        + "".join(cards) +
        '</div>'
        '</section>'
    )


def _cross_links_html(exclude_url: str = "") -> str:
    items = []
    for url, label in _CROSS_LINKS:
        if url == exclude_url:
            continue
        items.append(f'<li><a href="{url}">{_escape(label)}</a></li>')
    return f"""<section class="art-related" style="margin-top:32px">
<h2>Связанные материалы</h2>
<ul>
{chr(10).join(items)}
</ul>
</section>"""


def render_article(
    article: "Article",
    body_html: str,
    *,
    cover_url: str | None = None,
    date_iso: str = "",
    base_url: str = "https://designservice.group",
    recent_articles: list[dict] | None = None,
) -> str:
    """Render full HTML document for blog article.

    Args:
        article: Article model from designservice _bot_api.php.
        body_html: AI-generated body fragment — h1/h2/p/img/ul/details… без
            обёртки <html>/<head>/<body>. h1 в самом начале НЕ нужен —
            render добавит его автоматически из article.h1.
        cover_url: optional absolute URL for hero image. Defaults to site logo.
        date_iso: ISO timestamp for datePublished/dateModified. Defaults to
            article.published_date or planned_date with 10:00:00+03:00 suffix.
        base_url: site URL без trailing slash.

    Returns:
        Complete HTML document as string. UTF-8 encoding implied via meta tag.
    """
    base_url = base_url.rstrip("/")
    url = base_url + (article.target_url or f"/blog/{article.id}/")
    cover = cover_url or f"{base_url}/Logo_DS.png"

    if not date_iso:
        date_part = article.published_date or article.planned_date or "2026-05-19"
        date_iso = f"{date_part}T10:00:00+03:00"

    # Ensure description meets Yoast 100-180 chars requirement
    meta_description = _ensure_description_length(
        article.meta_description, article.h1, article.kw_primary or ""
    )

    # ld+json blocks
    ld_scripts = [
        _json_ld_script(_build_blogposting_ld(article, url, cover, date_iso)),
        _json_ld_script(_build_breadcrumb_ld(article, url, base_url)),
    ]
    faq_items = _extract_faq_from_body(body_html)
    if faq_items:
        ld_scripts.append(_json_ld_script(_build_faq_ld(faq_items)))

    # Head section — mirror existing /blog/*/index.html pattern (COFAB_NO_FOUC + shared.js)
    head = f"""<!DOCTYPE html>
<html lang="ru">
<head>
<meta charset="UTF-8">
<!-- COFAB_NO_FOUC_v1 -->
<script>
(function(){{
  try{{
    var t = localStorage.getItem('ds_theme');
    if(t === 'dark') document.documentElement.setAttribute('data-theme','dark');
  }}catch(e){{}}
}})();
</script>
<style id="ds-no-fouc">
:root{{--bg:#f5f3ef;--bg2:#eae6df;--bg3:#fff;--text:#1a1a1a;--border:#ddd}}
[data-theme="dark"]{{--bg:#0c0c0e;--bg2:#151518;--bg3:#1c1c21;--text:#e8e6e1;--border:#2a2a2e}}
html{{background:var(--bg)}}
body{{background:var(--bg);color:var(--text)}}
</style>
<!-- /COFAB_NO_FOUC_v1 -->
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{_escape(article.title_seo or article.h1)}</title>
<meta name="description" content="{_escape(meta_description)}">
<link rel="canonical" href="{url}">
<meta property="og:type" content="article">
<meta property="og:title" content="{_escape(article.title_seo or article.h1)}">
<meta property="og:description" content="{_escape(meta_description)}">
<meta property="og:image" content="{cover}">
<meta property="og:url" content="{url}">
<meta property="og:site_name" content="Дизайн-Сервис">
<meta property="og:locale" content="ru_RU">
<meta property="article:published_time" content="{article.planned_date or '2026-05-19'}">
<meta property="article:author" content="{_DEFAULT_AUTHOR['name']}">
<meta name="twitter:card" content="summary_large_image">
<meta name="twitter:title" content="{_escape(article.title_seo or article.h1)}">
<meta name="twitter:description" content="{_escape(meta_description)}">
<meta name="twitter:image" content="{cover}">
<link rel="icon" type="image/png" href="/img/Favicon.png">
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Manrope:wght@300;400;500;600;700&family=Playfair+Display:wght@400;500;600&display=swap" rel="stylesheet" media="print" onload="this.media='all'">
<noscript><link href="https://fonts.googleapis.com/css2?family=Manrope:wght@300;400;500;600;700&family=Playfair+Display:wght@400;500;600&display=swap" rel="stylesheet"></noscript>
<link rel="stylesheet" href="/css/article.css?v=12_s12v2">
<link rel="stylesheet" href="/css/blog.css?v=12_s12v2">

<script src="https://unpkg.com/react@18/umd/react.production.min.js" crossorigin></script>
<script src="https://unpkg.com/react-dom@18/umd/react-dom.production.min.js" crossorigin></script>
<script src="/shared.js?v=12_phase14_202605120400"></script>
<script src="/page_stats_widget.js?v=12_phase14_202605120400"></script>
<script>
(function(m,e,t,r,i,k,a){{m[i]=m[i]||function(){{(m[i].a=m[i].a||[]).push(arguments)}};m[i].l=1*new Date();for(var j=0;j<document.scripts.length;j++){{if(document.scripts[j].src===r){{return;}}}}k=e.createElement(t),a=e.getElementsByTagName(t)[0],k.async=1,k.src=r,a.parentNode.insertBefore(k,a)}})(window,document,"script","https://mc.yandex.ru/metrika/tag.js","ym");
ym(48007919,"init",{{clickmap:true,trackLinks:true,accurateTrackBounce:true,webvisor:true}});
</script>
<noscript><div><img src="https://mc.yandex.ru/watch/48007919" style="position:absolute;left:-9999px" alt="Yandex Metrika"></div></noscript>
{chr(10).join(ld_scripts)}
</head>"""

    # Body section — visible SSR
    breadcrumbs_parts = ['<a href="/">Главная</a>', '<a href="/blog.html">Блог</a>']
    if article.service_label and article.service_url:
        breadcrumbs_parts.append(
            f'<a href="{article.service_url}">{_escape(article.service_label)}</a>'
        )
    breadcrumbs_parts.append(f'<span>{_escape(article.h1)}</span>')
    breadcrumbs_html = '<span> / </span>'.join(breadcrumbs_parts)

    # Article HTML as JS template literal — wrapped by React PageShell at runtime.
    # Mirror pattern used by existing /blog/*/index.html.
    article_html = f"""<div class="art-progress"><div class="art-progress-fill"></div></div><div class="art-wrap"><div class="art-breadcrumbs">{breadcrumbs_html}</div><header class="art-header"><div class="art-tags"><span class="art-tag">{_escape(article.kw_primary)}</span><span class="art-tag">{_escape(article.service_label or "Блог")}</span></div><h1 class="art-title">{_escape(article.h1)}</h1><p class="art-excerpt">{_escape(meta_description)}</p><div class="art-meta"><span class="art-author"><img class="art-author-avatar" src="{_DEFAULT_AUTHOR['avatar']}" alt="{_escape(_DEFAULT_AUTHOR['name'])}" width="84" height="84" loading="lazy"><span class="art-author-text"><span class="art-author-name">{_escape(_DEFAULT_AUTHOR['name'])}</span><span class="art-author-job">Автор статьи • {_escape(_DEFAULT_AUTHOR['jobTitle'])}</span></span></span><span class="art-meta-dot"></span><span><time datetime="{date_iso}">{_format_date_ru(article.planned_date)}</time></span><span class="art-meta-dot"></span><span>{_reading_minutes(body_html)} мин чтения</span></div></header><img class="art-cover" src="{cover}" alt="{_escape(article.h1)}" loading="eager" fetchpriority="high"><div class="art-body">{body_html}</div>{_author_block_html()}{_read_more_html(recent_articles)}{_cross_links_html(exclude_url=article.service_url)}</div>"""

    # Strip any backticks from article_html — they would break JS template literal.
    article_html_js_safe = article_html.replace("\\", "\\\\").replace("`", "\\`").replace("${{", "\\${{")
    # HYBRID SSR + sample-matching mount pattern:
    # - SSR content lives in <div id=root>...</div> for server-side parsers
    # - React mount reads innerHTML, builds Page() function returning
    #   PageShell wrapping <main class="main-content"> (matches existing
    #   site's blog template so footer/sidebar render correctly)
    body = f"""<body class="art-page">
<div id="root">{article_html}{_SSR_FOOTER_HTML}</div>
<script>
(function(){{
var e=React.createElement;
var root=document.getElementById('root');
function mount(){{
  if (typeof window.PageShell !== 'function') {{
    return;  // SSR content already visible
  }}
  var ARTICLE_HTML = root.innerHTML;
  root.innerHTML = '';
  function Page(){{
    return e(window.PageShell, {{ activePage: '/blog.html', hideFooter: true }},
      e('main', {{ className: 'main-content', dangerouslySetInnerHTML: {{ __html: ARTICLE_HTML }} }})
    );
  }}
  ReactDOM.createRoot(root).render(e(Page));
  // Article progress bar
  window.addEventListener('scroll', function(){{
    var bar = document.querySelector('.art-progress-fill');
    if (!bar) return;
    var s = window.scrollY;
    var H = document.documentElement.scrollHeight - window.innerHeight;
    bar.style.width = Math.min(100, (s / Math.max(1, H)) * 100) + '%';
  }}, {{ passive: true }});
}}
if (document.readyState === 'loading') {{
  document.addEventListener('DOMContentLoaded', mount);
}} else {{
  mount();
}}
}})();
</script>
</body>"""
    return head + "\n" + body + "\n</html>\n"


def render_article_summary(article: "Article", body_html: str) -> dict[str, Any]:
    """Return SEO metrics of rendered article without returning full HTML.

    Useful for Telegram preview — show GRAD the counts before publishing.
    """
    full = render_article(article, body_html)
    word_count = len(re.findall(r"[А-Яа-яA-Za-z0-9]+", re.sub(r"<[^>]+>", " ", body_html)))
    return {
        "size_bytes": len(full.encode("utf-8")),
        "word_count": word_count,
        "h2_count": len(re.findall(r"<h2\b", body_html, re.IGNORECASE)),
        "faq_count": len(_extract_faq_from_body(body_html)),
        "internal_links": len(re.findall(r'href="/[^"]', body_html)),
        "external_links": len(
            re.findall(r'href="https?://(?!designservice\.group)', body_html)
        ),
        "ld_json_count": full.count('application/ld+json'),
        "kw_primary_mentions": len(
            re.findall(
                re.escape(article.kw_primary),
                re.sub(r"<[^>]+>", " ", body_html),
                re.IGNORECASE,
            )
        )
        if article.kw_primary
        else 0,
    }
