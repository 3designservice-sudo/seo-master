"""Auto-update /blog.html article grid after a new article is published.

Pipeline (called from api/designservice_publish.py after mark_published):
    1. Fetch current blog.html via _receiver.php read endpoint (or HTTP GET).
    2. Parse the JS-rendered article-grid: find e('div',{className:'article-grid',...},
       <existing cards>). The cards are e('a',{...},e('div',{...}),e('div',{...})).
    3. Build a new card matching that structure: href, cover (data-cat), title,
       excerpt, date, reading time.
    4. Insert as FIRST child of the grid (newest first).
    5. Bump 'Все статьи · 14' → 'Все статьи · 15'.
    6. Add new pill 'data-cat' value if missing.
    7. Upload back via _receiver.php (full file replace).

This is best-effort — any parse failure logs and skips, article still works
on its own URL.
"""

from __future__ import annotations

import base64
import re
from typing import Any

import httpx
import structlog

log = structlog.get_logger()


def _escape_js_string(text: str) -> str:
    """Escape single quotes and newlines for use in JS string literal 'x'."""
    return text.replace("\\", "\\\\").replace("'", "\\'").replace("\n", " ")


def _build_card_js(
    *,
    slug: str,
    category: str,
    title: str,
    excerpt: str,
    cover_url: str,
    date_ru: str,
    reading_time: int,
) -> str:
    """Build the JS expression for one article-card in blog.html grid."""
    return (
        f"e('a',{{className:'article-card',href:'/blog/{_escape_js_string(slug)}/','data-cat':'{_escape_js_string(category)}'}},"
        f"e('div',{{className:'article-card-cover',style:{{backgroundImage:\"url('{cover_url}')\"}}}}),"
        f"e('div',{{className:'article-card-body'}},"
        f"e('div',{{className:'article-card-cat'}},'{_escape_js_string(category)}'),"
        f"e('h3',null,'{_escape_js_string(title)}'),"
        f"e('p',null,'{_escape_js_string(excerpt)}'),"
        f"e('div',{{className:'article-card-meta'}},"
        f"e('span',null,'{_escape_js_string(date_ru)}'),"
        f"e('span',{{className:'dot'}},'•'),"
        f"e('span',null,'{reading_time} мин'))))"
    )


async def update_blog_index(
    *,
    article: Any,
    cover_url: str,
    reading_time: int,
    date_ru: str,
    http_client: httpx.AsyncClient,
    designservice_client: Any,
    base_url: str = "https://designservice.group",
    reader_key: str = "ds_read_2026",
) -> bool:
    """Insert new article card into /blog.html. Returns True on success.

    Never raises — failures logged. Idempotent: if slug already in blog.html,
    no-op (returns True).
    """
    slug = (article.target_url or "").strip("/").removeprefix("blog/").rstrip("/")
    if not slug:
        log.warning("blog_index.update.no_slug")
        return False

    # 1. Fetch current blog.html — use _reader.php (lines with N: prefix)
    try:
        r = await http_client.get(
            f"{base_url.rstrip('/')}/_reader.php",
            params={"t": reader_key, "f": "blog.html", "a": 0, "b": 99999},
            timeout=30.0,
        )
        if r.status_code != 200:
            log.warning("blog_index.update.fetch_failed", status=r.status_code)
            return False
        # Strip "N: " prefix from each line (reader's debug format)
        html = re.sub(r"^[0-9]+: ", "", r.text, flags=re.MULTILINE)
    except Exception as exc:
        log.warning("blog_index.update.fetch_exception", err=str(exc))
        return False

    # 2. Idempotency check: if our slug already in blog.html, skip
    if f"href:'/blog/{slug}/'" in html:
        log.info("blog_index.update.already_present", slug=slug)
        return True

    # 3. Build new card JS
    service_label = article.service_label or "Блог"
    title_short = article.h1
    excerpt = (article.meta_description or "")[:200]
    new_card_js = _build_card_js(
        slug=slug,
        category=service_label,
        title=title_short,
        excerpt=excerpt,
        cover_url=cover_url,
        date_ru=date_ru,
        reading_time=reading_time,
    )

    # 4. Find article-grid section: e('div',{className:'article-grid',id:'articleGrid'}, FIRST_CARD, ...)
    # Pattern: ,e('div',{className:'article-grid',id:'articleGrid'},
    grid_pattern = r"(e\('div',\{className:'article-grid',id:'articleGrid'\},)"
    m = re.search(grid_pattern, html)
    if not m:
        log.warning("blog_index.update.grid_not_found")
        return False
    insert_pos = m.end()
    # Insert new card as first child + comma
    new_html = html[:insert_pos] + new_card_js + "," + html[insert_pos:]

    # 5. Bump "Все статьи · N" count
    def _bump(match: re.Match[str]) -> str:
        n = int(match.group(1)) + 1
        return f"'Все статьи · {n}'"
    new_html = re.sub(r"'Все статьи · (\d+)'", _bump, new_html, count=1)

    # 6. Upload back via _receiver.php (full file replace)
    try:
        b64 = base64.b64encode(new_html.encode("utf-8")).decode("ascii")
        r = await http_client.post(
            f"{base_url.rstrip('/')}/_receiver.php",
            params={
                "k": designservice_client.receiver_key,
                "p": "blog.html",
            },
            content=b64,
            timeout=30.0,
        )
        if r.status_code != 200 or "OK" not in r.text:
            log.warning("blog_index.update.upload_failed", status=r.status_code, body=r.text[:200])
            return False
    except Exception as exc:
        log.warning("blog_index.update.upload_exception", err=str(exc))
        return False

    log.info("blog_index.update.success", slug=slug)
    return True
