"""Unit tests for services.seo.designservice_html.render_article."""
from __future__ import annotations

import re

from integrations.designservice.models import Article
from services.seo import render_article


def _sample_article() -> Article:
    return Article(
        id=1,
        service="renovation",
        service_label="Ремонт квартир",
        service_url="/services/renovation.html",
        kind="info_basic",
        h1="Что такое ремонт квартир и как заказать в Крыму",
        title_seo="Что такое ремонт квартир и как заказать в Крыму | ДС",
        meta_description="Что входит в ремонт квартир под ключ, какие этапы и сроки.",
        kw_primary="ремонт квартир",
        target_url="/blog/chto-takoe-remont-kvartir-i-kak-zakazat-v-krymu/",
        target_words=1500,
        faq_entries=2,
        planned_date="2026-05-19",
        status="planned",
    )


def test_render_article_returns_valid_html() -> None:
    body = "<p>Test body</p><h2>Section</h2><p>More.</p>"
    out = render_article(_sample_article(), body)
    assert "<!DOCTYPE html>" in out
    assert "<html lang=\"ru\">" in out
    assert "</html>" in out


def test_canonical_url_is_set() -> None:
    article = _sample_article()
    out = render_article(article, "<p>x</p>")
    assert (
        '<link rel="canonical" href="https://designservice.group/blog/chto-takoe-remont-kvartir-i-kak-zakazat-v-krymu/">'
        in out
    )


def test_three_ld_json_blocks_present_with_faq() -> None:
    article = _sample_article()
    body = (
        '<p>x</p>'
        '<details><summary>Question 1?</summary><p>Answer 1.</p></details>'
        '<details><summary>Question 2?</summary><p>Answer 2.</p></details>'
    )
    out = render_article(article, body)
    assert out.count('application/ld+json') == 3
    assert '"@type":"BlogPosting"' in out
    assert '"@type":"BreadcrumbList"' in out
    assert '"@type":"FAQPage"' in out


def test_two_ld_json_blocks_without_faq() -> None:
    out = render_article(_sample_article(), "<p>no faq here</p>")
    assert out.count('application/ld+json') == 2
    assert '"@type":"FAQPage"' not in out


def test_h1_appears_in_body_ssr() -> None:
    article = _sample_article()
    out = render_article(article, "<p>x</p>")
    # H1 must be in body, not just in head/title
    h1_match = re.search(r'<h1[^>]*>(.+?)</h1>', out)
    assert h1_match
    assert article.h1 in h1_match.group(1)


def test_og_and_twitter_meta_present() -> None:
    out = render_article(_sample_article(), "<p>x</p>")
    assert '<meta property="og:type" content="article">' in out
    assert '<meta name="twitter:card" content="summary_large_image">' in out


def test_cover_url_override() -> None:
    custom = "https://designservice.group/data/articles/img1.jpg"
    out = render_article(_sample_article(), "<p>x</p>", cover_url=custom)
    assert f'content="{custom}"' in out
    assert f'src="{custom}"' in out


def test_breadcrumbs_include_service() -> None:
    article = _sample_article()
    out = render_article(article, "<p>x</p>")
    # JSON-LD BreadcrumbList must include service
    assert article.service_label in out
    assert "/services/renovation.html" in out


def test_meta_description_escaped() -> None:
    article = _sample_article()
    article.meta_description = 'Test with "quotes" & ampersand <tag>.'
    out = render_article(article, "<p>x</p>")
    # In meta content attribute, quotes must be escaped
    assert '&quot;quotes&quot;' in out
    assert '&amp; ampersand' in out
    assert '&lt;tag&gt;' in out
