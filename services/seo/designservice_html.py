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
}

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


def _author_block_html() -> str:
    return f"""<section class="art-author-block" style="margin-top:48px;padding:32px;background:var(--bg2,#eae6df);border-radius:16px">
<h2 style="margin-top:0">Об авторе</h2>
<p><strong>{_escape(_DEFAULT_AUTHOR["name"])}</strong> — директор и ведущий дизайнер ООО «Дизайн-Сервис». Работает в Крыму с 1997 года. За 27 лет студия выполнила более 320 проектов — от квартир под сдачу в новостройках Симферополя до частных домов на южном берегу Крыма. <a href="{_DEFAULT_AUTHOR["url"]}">Подробнее об авторе</a>.</p>
</section>"""


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

    # ld+json blocks
    ld_scripts = [
        _json_ld_script(_build_blogposting_ld(article, url, cover, date_iso)),
        _json_ld_script(_build_breadcrumb_ld(article, url, base_url)),
    ]
    faq_items = _extract_faq_from_body(body_html)
    if faq_items:
        ld_scripts.append(_json_ld_script(_build_faq_ld(faq_items)))

    # Head section
    head = f"""<!DOCTYPE html>
<html lang="ru">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{_escape(article.title_seo or article.h1)}</title>
<meta name="description" content="{_escape(article.meta_description)}">
<link rel="canonical" href="{url}">
<meta property="og:type" content="article">
<meta property="og:title" content="{_escape(article.title_seo or article.h1)}">
<meta property="og:description" content="{_escape(article.meta_description)}">
<meta property="og:image" content="{cover}">
<meta property="og:url" content="{url}">
<meta property="og:site_name" content="Дизайн-Сервис">
<meta property="og:locale" content="ru_RU">
<meta property="article:published_time" content="{article.planned_date or '2026-05-19'}">
<meta property="article:author" content="{_DEFAULT_AUTHOR['name']}">
<meta name="twitter:card" content="summary_large_image">
<meta name="twitter:title" content="{_escape(article.title_seo or article.h1)}">
<meta name="twitter:description" content="{_escape(article.meta_description)}">
<meta name="twitter:image" content="{cover}">
<link rel="icon" type="image/png" href="/img/Favicon.png">
<link rel="stylesheet" href="/css/article.css?v=12_s12v2">
<link rel="stylesheet" href="/css/blog.css?v=12_s12v2">
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

    body_inner = f"""<div class="art-wrap">
<nav class="art-breadcrumbs">{breadcrumbs_html}</nav>

<header class="art-header">
<h1 class="art-title">{_escape(article.h1)}</h1>
<p class="art-excerpt">{_escape(article.meta_description)}</p>
<div class="art-meta">
<span class="art-author"><span class="art-author-initials">{_DEFAULT_AUTHOR['initials']}</span>
<span class="art-author-text"><span class="art-author-name">{_escape(_DEFAULT_AUTHOR['name'])}</span>
<span class="art-author-job">{_escape(_DEFAULT_AUTHOR['jobTitle'])}</span></span></span>
<span class="art-meta-dot"></span>
<span><time datetime="{date_iso}">{(article.planned_date or '').replace('-', '.')}</time></span>
</div>
</header>

<img class="art-cover" src="{cover}" alt="{_escape(article.h1)}" loading="eager" fetchpriority="high">

<div class="art-body">
{body_html}
</div>

{_author_block_html()}

{_cross_links_html(exclude_url=article.service_url)}
</div>"""

    body = '<body class="art-page">\n' + body_inner + '\n</body>'
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
