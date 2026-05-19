"""SEO helpers for designservice.group blog publishing.

Public surface:
    render_article(article, body_html, cover_url=None) -> str
        Builds final HTML with 3 ld+json, canonical, og/twitter, SSR h1.
    humanize_html(html, max_em_dash_per_1k=8) -> tuple[str, dict]
        Regex-based anti-AI pass: trims em-dash overuse, removes AI vocabulary,
        respects script/style/anchor attributes (untouchable zones).
"""

from services.seo.designservice_html import inject_yoast_keyword, render_article
from services.seo.humanizer import humanize_html

__all__ = ["humanize_html", "inject_yoast_keyword", "render_article"]
