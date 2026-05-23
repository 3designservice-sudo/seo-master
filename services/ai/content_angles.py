"""Content-angle taxonomy + selection for topic diversity (guided-flow track 1).

The bot (not the user) picks the topic from category keywords; this module adds
a *content angle* (format) around the niche so consecutive publications differ:
product overview, customer pain, how-to guide, technical specs, comparison, case.

Pure module — no Telegram/DB deps. The injection (see services/ai/articles.py)
is INERT until BOTH are true:
  1) Settings.content_angle_rotation_enabled is True, and
  2) the active article prompt in DB renders the `content_angle` variable.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ContentAngle:
    """A content format/angle around the niche."""

    id: str
    name: str
    instruction: str


# Distilled from tenant taxonomies (bamboodom/designservice) into a generic set.
CONTENT_ANGLES: list[ContentAngle] = [
    ContentAngle(
        "product",
        "Описание продукта/услуги",
        "Сделай акцент на подробном описании продукта/услуги: что это, состав или "
        "комплектация, для кого, ключевые свойства и выгоды.",
    ),
    ContentAngle(
        "pain",
        "Боль клиента → решение",
        "Построй статью вокруг типичной проблемы клиента и покажи, как продукт или "
        "услуга её решает (формат «проблема — решение»).",
    ),
    ContentAngle(
        "guide",
        "Гид / пошаговая инструкция",
        "Сделай практический гид: пошаговая инструкция, чек-лист, как выбрать, "
        "сделать или подготовиться.",
    ),
    ContentAngle(
        "specs",
        "Профессиональные характеристики",
        "Сделай технический разбор: характеристики, параметры, стандарты, на что "
        "смотреть профессионалу.",
    ),
    ContentAngle(
        "comparison",
        "Сравнение и выбор",
        "Сравни варианты, материалы или решения, дай критерии выбора и "
        "рекомендации (buyer's guide).",
    ),
    ContentAngle(
        "case",
        "Кейс / пример",
        "Построй статью как кейс или реальный пример: задача, решение, результат, "
        "выводы.",
    ),
]


def select_angle(keyword: str, *, index: int | None = None) -> ContentAngle:
    """Pick a content angle for a topic.

    - If ``index`` is given, rotate deterministically by it (e.g. publication
      count) — useful for autopublish so consecutive posts vary by format.
    - Otherwise pick deterministically by the keyword, so the same topic keeps a
      consistent angle while different topics naturally differ.
    """
    n = len(CONTENT_ANGLES)
    if index is not None:
        return CONTENT_ANGLES[index % n]
    bucket = sum(ord(c) for c in (keyword or "")) % n
    return CONTENT_ANGLES[bucket]
