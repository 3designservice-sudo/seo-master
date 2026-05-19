"""Pydantic models for Designservice _bot_api.php responses."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


# Statuses recognised by _bot_api.php (mirror of valid_statuses list).
ArticleStatus = Literal[
    "planned",
    "writing",
    "published",
    "blocked",
    "indexed_low",
]


class Article(BaseModel):
    """One article row from data/seo/article_roadmap.json.

    All optional fields default empty so partial responses (from older
    roadmap versions) don't break parsing.
    """

    id: int
    service: str = ""
    service_label: str = ""
    service_url: str = ""
    service_group: str = ""
    kind: str = ""
    h1: str
    title_seo: str = ""
    meta_description: str = ""
    kw_primary: str = ""
    kw_secondary: list[str] = Field(default_factory=list)
    kw_total_freq: int = 0
    intent: str = ""
    customer_pain: str = ""
    answer_points: list[str] = Field(default_factory=list)
    geo: str = "Крым"
    city_slug: str = ""
    zhk_slug: str = ""
    target_words: int = 1500
    target_url: str = ""
    schema_types: str = "Article"
    faq_entries: int = 3
    llm_entities: str = ""
    llm_citable_sources: str = ""
    llm_brief: str = ""
    planned_date: str = ""
    status: ArticleStatus = "planned"
    # Set when status=published.
    published_date: str = ""
    published_url: str = ""
    word_count: int = 0
    humanizer_score: float = 0.0
    last_updated: str = ""
    block_reason: str = ""
    notes: str = ""


class PlannedResponse(BaseModel):
    """Response of ?action=get_planned."""

    date: str
    count: int
    articles: list[Article]


class StatsResponse(BaseModel):
    """Response of ?action=stats — roadmap pipeline dashboard."""

    total: int
    by_status: dict[str, int] = Field(default_factory=dict)
    by_service: dict[str, int] = Field(default_factory=dict)
    by_kind: dict[str, int] = Field(default_factory=dict)
    planned_by_date: dict[str, int] = Field(default_factory=dict)
    overdue_planned: int = 0


class MarkStatusResponse(BaseModel):
    """Response of ?action=mark_status — updated article + ok flag."""

    ok: bool
    article: Article


class PublishResult(BaseModel):
    """Result of publish_html() — wraps _receiver.php response.

    _receiver.php returns plain text 'OK <bytes> -> <path>'. We parse that
    into structured fields for easier UX in admin panel.
    """

    ok: bool
    dst_path: str = ""
    bytes_written: int = 0
    raw_response: str = ""
