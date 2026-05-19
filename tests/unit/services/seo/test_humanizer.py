"""Unit tests for services.seo.humanizer.humanize_html."""
from __future__ import annotations

from services.seo import humanize_html


def test_no_em_dashes_passes_through() -> None:
    text = "<p>Этот текст без длинных тире.</p>"
    out, stats = humanize_html(text)
    assert stats["em_dash_before"] == 0
    assert stats["em_dash_after"] == 0
    assert stats["score"] >= 0.9


def test_em_dash_trimming_reduces_density() -> None:
    # ~250 words with many em-dashes
    text = "<p>" + " ".join(
        ["Слово — " for _ in range(50)] + ["конец."] * 50
    ) + "</p>"
    out, stats = humanize_html(text)
    assert stats["em_dash_after"] < stats["em_dash_before"]
    assert stats["em_dash_per_1k_after"] <= 16  # close to target 8/1k after trim


def test_ai_phrase_removed() -> None:
    text = "<p>Важно отметить, что ремонт квартир требует подготовки.</p>"
    out, stats = humanize_html(text)
    assert stats["phrases_removed"] >= 1
    assert "Важно отметить" not in out


def test_protected_script_block_unchanged() -> None:
    text = (
        '<p>Важно отметить</p>'
        '<script type="application/ld+json">{"important":"keep"}</script>'
        '<p>Нормальный текст.</p>'
    )
    out, stats = humanize_html(text)
    assert '"important":"keep"' in out
    assert "Важно отметить" not in out


def test_protected_style_unchanged() -> None:
    text = (
        '<style>body { color: red; }</style>'
        '<p>Важно отметить, что текст важен.</p>'
    )
    out, _ = humanize_html(text)
    assert "body { color: red; }" in out
    assert "Важно отметить" not in out


def test_promotional_replaced() -> None:
    text = "<p>Премиальное качество — наш стандарт.</p>"
    out, stats = humanize_html(text)
    assert "Премиальное качество" not in out
    assert "качество" in out.lower()


def test_score_clamped_to_unit_range() -> None:
    text = "<p>Текст без проблем.</p>"
    _, stats = humanize_html(text)
    assert 0.0 <= stats["score"] <= 1.0


def test_removed_examples_listed() -> None:
    text = (
        "<p>Важно отметить факт. Стоит подчеркнуть это. В конечном счёте всё ясно.</p>"
    )
    _, stats = humanize_html(text)
    assert len(stats["removed_examples"]) <= 5
    assert any("Важно отметить" in ex for ex in stats["removed_examples"])
