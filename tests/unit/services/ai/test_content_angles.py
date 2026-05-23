"""Unit tests for content-angle taxonomy + selection (guided-flow track 1)."""

from services.ai.content_angles import CONTENT_ANGLES, ContentAngle, select_angle


def test_taxonomy_nonempty_and_well_formed() -> None:
    assert len(CONTENT_ANGLES) >= 6
    assert all(isinstance(a, ContentAngle) and a.id and a.name and a.instruction for a in CONTENT_ANGLES)
    assert len({a.id for a in CONTENT_ANGLES}) == len(CONTENT_ANGLES)  # unique ids


def test_select_by_index_rotates() -> None:
    n = len(CONTENT_ANGLES)
    assert select_angle("x", index=0).id == CONTENT_ANGLES[0].id
    assert select_angle("x", index=1).id == CONTENT_ANGLES[1].id
    assert select_angle("x", index=n).id == CONTENT_ANGLES[0].id  # wraps around


def test_select_by_keyword_is_deterministic() -> None:
    assert select_angle("кухни на заказ").id == select_angle("кухни на заказ").id


def test_select_by_keyword_varies_across_topics() -> None:
    kws = ["кухни", "шкафы", "двери", "полы", "окна", "ремонт", "плитка", "мебель"]
    assert len({select_angle(k).id for k in kws}) >= 2
