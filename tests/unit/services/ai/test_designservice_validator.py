"""Unit tests for services.ai.designservice_validator."""
from __future__ import annotations

from types import SimpleNamespace

from services.ai.designservice_validator import validate_article


def _sample_article(**overrides) -> SimpleNamespace:
    base = dict(
        id=1,
        h1="Что такое ремонт квартир и как заказать в Крыму",
        kw_primary="ремонт квартир",
        kw_secondary=["ремонт квартир крым"],
        target_words=1500,
        faq_entries=3,
    )
    base.update(overrides)
    return SimpleNamespace(**base)


def _body_close_to_target() -> str:
    """Build a body that passes most checks (used for green-path test)."""
    paragraphs = []
    # Lead
    paragraphs.append(
        '<p class="art-lead">Ремонт квартир в Крыму — это полный цикл работ '
        'от демонтажа до приёмки. Студия Дизайн-Сервис работает в Симферополе '
        'с 1997 года под руководством Александра Шульман. Подробнее об услуге '
        '<a href="/services/renovation.html">ремонт квартир</a>.</p>'
    )
    # Six h2 sections, each ~200 words
    for i in range(6):
        paragraphs.append(
            f'<h2>Раздел {i+1}: ремонт квартир — детали</h2>'
        )
        long_para = (
            "Студия Дизайн-Сервис делает ремонт квартир по фиксированной "
            "смете 25 000-35 000 ₽/м² для эконом-сегмента в Севастополе. "
            "Срок 12 недель для квартиры 60 м². Подробности у Александра "
            "Шульман. " * 10
        )
        paragraphs.append(f'<p>{long_para}</p>')

    # External authority + internal link mix
    paragraphs.append(
        '<p>Подробности <a href="https://ru.wikipedia.org/wiki/Ремонт" rel="noopener">'
        'в Википедии</a> и на странице <a href="/services/design.html">дизайна интерьера</a>. '
        'Смотри также <a href="/projects.html">проекты</a> и <a href="/about.html">о студии</a>.</p>'
    )

    # Expert quote
    paragraphs.append(
        '<p>«Ремонт квартир в Крыму отличается сезонностью и логистикой материалов '
        'с материка.» — Александр Шульман, директор студии.</p>'
    )

    # 3 FAQ
    for i in range(3):
        paragraphs.append(
            f'<details class="faq"><summary>Вопрос {i+1}?</summary>'
            f'<p>Ответ на вопрос {i+1}, ремонт квартир обсуждается подробно.</p></details>'
        )
    return "\n".join(paragraphs)


def test_passing_body_scores_high() -> None:
    body = _body_close_to_target()
    result = validate_article(_sample_article(), body)
    assert result.score >= 70  # not 100 (humanizer might dock), but most checks pass
    assert not any(c.name == "no_h1_in_body" and not c.passed for c in result.checks)


def test_h1_in_body_fails() -> None:
    body = '<h1>Bad h1</h1><p class="art-lead">x</p>'
    result = validate_article(_sample_article(), body)
    failed = [c.name for c in result.failed]
    assert "no_h1_in_body" in failed


def test_missing_faq_fails() -> None:
    article = _sample_article(faq_entries=3)
    body = _body_close_to_target().replace("<details", "<div hidden")
    result = validate_article(article, body)
    failed = [c.name for c in result.failed]
    assert "faq_count" in failed


def test_no_external_authority_fails() -> None:
    body = _body_close_to_target().replace("ru.wikipedia.org", "example.com")
    result = validate_article(_sample_article(), body)
    failed = [c.name for c in result.failed]
    assert "external_authority_link" in failed


def test_ai_phrase_detection() -> None:
    body = _body_close_to_target() + "<p>Важно отметить, что ремонт квартир сложен.</p>"
    result = validate_article(_sample_article(), body)
    failed = [c.name for c in result.failed]
    assert "no_ai_phrases" in failed


def test_word_count_too_short_fails() -> None:
    body = '<p class="art-lead">Слишком короткий текст про ремонт квартир.</p>'
    result = validate_article(_sample_article(target_words=1500), body)
    failed = [c.name for c in result.failed]
    assert "word_count" in failed


def test_kw_primary_too_few_fails() -> None:
    body = (
        '<p class="art-lead">текст без ключа</p>'
        + "<p>" + ("сухой текст. " * 200) + "</p>"
    )
    result = validate_article(_sample_article(), body)
    failed = [c.name for c in result.failed]
    assert "kw_primary_count" in failed


def test_score_is_percent() -> None:
    body = _body_close_to_target()
    result = validate_article(_sample_article(), body)
    assert 0 <= result.score <= 100


def test_feedback_messages_are_strings() -> None:
    body = '<h1>bad</h1>'  # многочисленные fails
    result = validate_article(_sample_article(), body)
    msgs = result.feedback_messages()
    assert all(isinstance(m, str) for m in msgs)
    assert len(msgs) >= 5  # многоошибочный body
