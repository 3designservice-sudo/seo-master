"""Article pipeline keyboards (UX_PIPELINE.md §4, §12)."""

from __future__ import annotations

from aiogram.enums import ButtonStyle
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from bot.texts.emoji import TOGGLE_ON
from db.models import Category, Project
from keyboards.pagination import _safe_cb, paginate
from services.readiness import ReadinessReport
from services.tokens import COST_PER_IMAGE

__all__ = [
    "pipeline_back_to_checklist_kb",
    "pipeline_categories_kb",
    "pipeline_confirm_kb",
    "pipeline_description_options_kb",
    "pipeline_exit_confirm_kb",
    "pipeline_generation_error_kb",
    "pipeline_images_options_kb",
    "pipeline_insufficient_balance_kb",
    "pipeline_keywords_city_kb",
    "pipeline_keywords_options_kb",
    "pipeline_no_categories_kb",
    "pipeline_no_projects_kb",
    "pipeline_no_wp_kb",
    "pipeline_preview_kb",
    "pipeline_preview_no_wp_kb",
    "pipeline_prices_options_kb",
    "pipeline_projects_kb",
    "pipeline_readiness_kb",
    "pipeline_result_kb",
]


# ---------------------------------------------------------------------------
# Step 1: Select project (shared article/social)
# ---------------------------------------------------------------------------


def pipeline_projects_kb(
    projects: list[Project],
    page: int = 1,
    pipeline_type: str = "article",
) -> InlineKeyboardMarkup:
    """Project selection for pipeline step 1.

    callback_data: pipeline:{type}:{project_id}:select
    pagination: page:pipeline_{type}_projects:{page}
    """
    return paginate(
        items=projects,
        page=page,
        cb_prefix=f"pipeline_{pipeline_type}_projects",
        item_text="name",
        item_cb=f"pipeline:{pipeline_type}:{{id}}:select",
    )[0]


def pipeline_no_projects_kb(pipeline_type: str = "article") -> InlineKeyboardMarkup:
    """No projects — offer to create one inline."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="Создать проект",
                    callback_data=f"pipeline:{pipeline_type}:create_project",
                    style=ButtonStyle.PRIMARY,
                ),
            ],
            [
                InlineKeyboardButton(
                    text="Отмена",
                    callback_data=f"pipeline:{pipeline_type}:cancel",
                ),
            ],
        ]
    )


# ---------------------------------------------------------------------------
# Step 2: Select WP connection
# ---------------------------------------------------------------------------


def pipeline_no_wp_kb() -> InlineKeyboardMarkup:
    """No WP connections — offer connect or preview-only."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="Подключить сайт",
                    callback_data="pipeline:article:connect_wp",
                    style=ButtonStyle.PRIMARY,
                ),
            ],
            [
                InlineKeyboardButton(
                    text="Только превью (без публикации)",
                    callback_data="pipeline:article:preview_only",
                ),
            ],
            [
                InlineKeyboardButton(
                    text="Назад",
                    callback_data="pipeline:article:back_project",
                ),
            ],
        ]
    )


# ---------------------------------------------------------------------------
# Step 3: Select category
# ---------------------------------------------------------------------------


def pipeline_categories_kb(
    categories: list[Category],
    project_id: int,
    page: int = 1,
    pipeline_type: str = "article",
) -> InlineKeyboardMarkup:
    """Category selection for pipeline step 3.

    callback_data: pipeline:{type}:{project_id}:cat:{cat_id}
    """
    kb, _ = paginate(
        items=categories,
        page=page,
        cb_prefix=f"pipeline_{pipeline_type}_categories",
        item_text="name",
        item_cb=f"pipeline:{pipeline_type}:{project_id}:cat:{{id}}",
    )
    # Add back button to return to previous step
    back_cb = (
        "pipeline:article:back_wp"
        if pipeline_type == "article"
        else "pipeline:social:back_connection"
    )
    kb.inline_keyboard.append(
        [InlineKeyboardButton(text="Назад", callback_data=back_cb)]
    )
    return kb


def pipeline_no_categories_kb(pipeline_type: str = "article") -> InlineKeyboardMarkup:
    """No categories — prompt for inline creation."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="Создать категорию",
                    callback_data=f"pipeline:{pipeline_type}:create_category",
                ),
            ],
            [
                InlineKeyboardButton(
                    text="Отмена",
                    callback_data=f"pipeline:{pipeline_type}:cancel",
                ),
            ],
        ]
    )


# ---------------------------------------------------------------------------
# Step 4: Readiness check
# ---------------------------------------------------------------------------


def pipeline_readiness_kb(report: ReadinessReport) -> InlineKeyboardMarkup:
    """Readiness checklist keyboard (UX_PIPELINE.md §4, step 4).

    Shows buttons only for missing items.
    """
    rows: list[list[InlineKeyboardButton]] = []

    if not report.has_description:
        rows.append(
            [
                InlineKeyboardButton(
                    text="Описание",
                    callback_data="pipeline:readiness:description",
                ),
            ]
        )

    if not report.has_keywords:
        rows.append(
            [
                InlineKeyboardButton(
                    text="Подобрать ключевики",
                    callback_data="pipeline:readiness:keywords",
                ),
            ]
        )

    if "prices" in report.missing_items:
        rows.append(
            [
                InlineKeyboardButton(
                    text="Добавить цены",
                    callback_data="pipeline:readiness:prices",
                ),
            ]
        )

    if "images" in report.missing_items:
        img_cost = report.image_count * COST_PER_IMAGE
        rows.append(
            [
                InlineKeyboardButton(
                    text=f"Изображения: {report.image_count} шт. ({img_cost} ток.) — настроить",
                    callback_data="pipeline:readiness:images",
                ),
            ]
        )

    # Main CTA: generate
    rows.append(
        [
            InlineKeyboardButton(
                text=TOGGLE_ON + "Готово \u2014 генерировать",
                callback_data="pipeline:readiness:done",
                style=ButtonStyle.SUCCESS,
            ),
        ]
    )

    # Cancel pipeline
    rows.append(
        [
            InlineKeyboardButton(
                text="Отмена",
                callback_data="pipeline:article:cancel",
            ),
        ]
    )

    return InlineKeyboardMarkup(inline_keyboard=rows)


# ---------------------------------------------------------------------------
# Step 5: Confirm cost
# ---------------------------------------------------------------------------


def pipeline_confirm_kb() -> InlineKeyboardMarkup:
    """Confirmation keyboard for pipeline step 5 (G6: includes cancel)."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="Создать статью",
                    callback_data="pipeline:article:confirm",
                    style=ButtonStyle.SUCCESS,
                ),
            ],
            [
                InlineKeyboardButton(
                    text="К чеклисту",
                    callback_data="pipeline:article:back_readiness",
                ),
            ],
            [
                InlineKeyboardButton(
                    text="Отмена",
                    callback_data="pipeline:article:cancel",
                ),
            ],
        ]
    )


def pipeline_insufficient_balance_kb() -> InlineKeyboardMarkup:
    """Insufficient balance — offer to top up (E01, UX_PIPELINE §8.1)."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="Пополнить баланс",
                    callback_data="nav:tokens",
                    style=ButtonStyle.PRIMARY,
                ),
            ],
            [
                InlineKeyboardButton(
                    text="Отмена",
                    callback_data="pipeline:article:cancel",
                ),
            ],
        ]
    )


# ---------------------------------------------------------------------------
# Step 7: Preview
# ---------------------------------------------------------------------------


def pipeline_preview_kb(
    telegraph_url: str | None = None,
    *,
    can_publish: bool = True,
    regen_count: int = 0,
    regen_cost: int = 0,
) -> InlineKeyboardMarkup:
    """Preview keyboard with publish/regenerate/cancel options.

    Args:
        telegraph_url: Telegraph preview link. None when E05 Telegraph down.
        can_publish: Whether WP connection is available.
        regen_count: Current regeneration count (0-based).
        regen_cost: Token cost for next regeneration (shown when count >= 2).
    """
    rows: list[list[InlineKeyboardButton]] = []

    # Preview link (E05: omitted when Telegraph is down)
    if telegraph_url:
        rows.append(
            [
                InlineKeyboardButton(
                    text="Открыть превью",
                    url=telegraph_url,
                ),
            ]
        )

    if can_publish:
        rows.append(
            [
                InlineKeyboardButton(
                    text="Опубликовать",
                    callback_data="pipeline:article:publish",
                    style=ButtonStyle.SUCCESS,
                ),
            ]
        )

    # Regen button: show cost when count >= MAX_REGENERATIONS_FREE (2)
    regen_text = "Перегенерировать"
    if regen_count >= 2 and regen_cost > 0:
        regen_text = f"Перегенерировать (~{regen_cost} ток.)"
    rows.append(
        [
            InlineKeyboardButton(
                text=regen_text,
                callback_data="pipeline:article:regenerate",
            ),
        ]
    )

    rows.append(
        [
            InlineKeyboardButton(
                text="Отмена (возврат)",
                callback_data="pipeline:article:cancel_refund",
                style=ButtonStyle.DANGER,
            ),
        ]
    )

    return InlineKeyboardMarkup(inline_keyboard=rows)


def pipeline_preview_no_wp_kb(
    telegraph_url: str | None = None,
    *,
    regen_count: int = 0,
    regen_cost: int = 0,
) -> InlineKeyboardMarkup:
    """Preview-only keyboard — Variant B, no WP (G1).

    Shows: preview link, connect WP, copy HTML, regenerate, cancel.
    """
    rows: list[list[InlineKeyboardButton]] = []

    if telegraph_url:
        rows.append(
            [
                InlineKeyboardButton(
                    text="Открыть превью",
                    url=telegraph_url,
                ),
            ]
        )

    rows.append(
        [
            InlineKeyboardButton(
                text="Подключить сайт и опубликовать",
                callback_data="pipeline:article:connect_wp_publish",
                style=ButtonStyle.SUCCESS,
            ),
        ]
    )

    rows.append(
        [
            InlineKeyboardButton(
                text="Скопировать HTML",
                callback_data="pipeline:article:copy_html",
            ),
        ]
    )

    regen_text = "Перегенерировать"
    if regen_count >= 2 and regen_cost > 0:
        regen_text = f"Перегенерировать (~{regen_cost} ток.)"
    rows.append(
        [
            InlineKeyboardButton(
                text=regen_text,
                callback_data="pipeline:article:regenerate",
            ),
        ]
    )

    rows.append(
        [
            InlineKeyboardButton(
                text="Отмена (возврат)",
                callback_data="pipeline:article:cancel_refund",
                style=ButtonStyle.DANGER,
            ),
        ]
    )

    return InlineKeyboardMarkup(inline_keyboard=rows)


# ---------------------------------------------------------------------------
# Step 8: Result
# ---------------------------------------------------------------------------


def pipeline_result_kb(post_url: str | None = None) -> InlineKeyboardMarkup:
    """Result keyboard after successful article publication."""
    rows: list[list[InlineKeyboardButton]] = []

    if post_url:
        rows.append(
            [
                InlineKeyboardButton(
                    text="Открыть на сайте",
                    url=post_url,
                ),
            ]
        )

    rows.append(
        [
            InlineKeyboardButton(
                text="Меню",
                callback_data="nav:dashboard",
            ),
        ]
    )

    return InlineKeyboardMarkup(inline_keyboard=rows)


# ---------------------------------------------------------------------------
# Step 6: Generation error
# ---------------------------------------------------------------------------


def pipeline_generation_error_kb() -> InlineKeyboardMarkup:
    """Error keyboard after failed generation (UX_PIPELINE §8.3, E35).

    3 buttons: Повторить / Другая тема / Главное меню.
    """
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="Повторить",
                    callback_data="pipeline:article:confirm",
                    style=ButtonStyle.PRIMARY,
                ),
            ],
            [
                InlineKeyboardButton(
                    text="Другая тема",
                    callback_data="pipeline:article:change_topic",
                ),
            ],
            [
                InlineKeyboardButton(
                    text="Меню",
                    callback_data="nav:dashboard",
                ),
            ],
        ]
    )


# ---------------------------------------------------------------------------
# Shared: Exit protection (§7.5)
# ---------------------------------------------------------------------------


def pipeline_exit_confirm_kb() -> InlineKeyboardMarkup:
    """Exit confirmation for steps 4-7."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="Да, выйти",
                    callback_data="pipeline:article:exit_confirm",
                    style=ButtonStyle.DANGER,
                ),
                InlineKeyboardButton(
                    text="Продолжить",
                    callback_data="pipeline:article:exit_cancel",
                ),
            ],
        ]
    )


# ---------------------------------------------------------------------------
# Readiness sub-flow: Keywords
# ---------------------------------------------------------------------------


def pipeline_keywords_options_kb(prefix: str = "pipeline:readiness") -> InlineKeyboardMarkup:
    """Keyword generation options in readiness sub-flow."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="Авто · 100 фраз",
                    callback_data=f"{prefix}:keywords:auto:100",
                ),
            ],
            [
                InlineKeyboardButton(
                    text="Авто · 200 фраз",
                    callback_data=f"{prefix}:keywords:auto:200",
                ),
            ],
            [
                InlineKeyboardButton(
                    text="Авто · 300 фраз",
                    callback_data=f"{prefix}:keywords:auto:300",
                ),
            ],
            [
                InlineKeyboardButton(
                    text="Настроить параметры",
                    callback_data=f"{prefix}:keywords:configure",
                ),
            ],
            [
                InlineKeyboardButton(
                    text="Загрузить свои",
                    callback_data=f"{prefix}:keywords:upload",
                ),
            ],
            [
                InlineKeyboardButton(
                    text="К чеклисту",
                    callback_data=f"{prefix}:back",
                ),
            ],
        ]
    )


def pipeline_keywords_city_kb(prefix: str = "pipeline:readiness") -> InlineKeyboardMarkup:
    """Quick city selection for auto-keywords when project has no company_city.

    UX_PIPELINE.md §4a: "В каком городе ваш бизнес?"
    """
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="Москва",
                    callback_data=_safe_cb(f"{prefix}:keywords:city:Москва"),
                ),
                InlineKeyboardButton(
                    text="Санкт-Петербург",
                    callback_data=_safe_cb(f"{prefix}:keywords:city:СПб"),
                ),
            ],
            [
                InlineKeyboardButton(
                    text="Вся Россия",
                    callback_data=_safe_cb(f"{prefix}:keywords:city:Россия"),
                ),
            ],
            [
                InlineKeyboardButton(
                    text="К чеклисту",
                    callback_data=f"{prefix}:keywords:cancel",
                ),
            ],
        ]
    )


# ---------------------------------------------------------------------------
# Readiness sub-flow: Description
# ---------------------------------------------------------------------------


def pipeline_description_options_kb(prefix: str = "pipeline:readiness") -> InlineKeyboardMarkup:
    """Description options in readiness sub-flow."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="Сгенерировать AI",
                    callback_data=f"{prefix}:description:ai",
                ),
            ],
            [
                InlineKeyboardButton(
                    text="Написать вручную",
                    callback_data=f"{prefix}:description:manual",
                ),
            ],
            [
                InlineKeyboardButton(
                    text="К чеклисту",
                    callback_data=f"{prefix}:back",
                ),
            ],
        ]
    )


# ---------------------------------------------------------------------------
# Readiness sub-flow: Prices
# ---------------------------------------------------------------------------


def pipeline_prices_options_kb() -> InlineKeyboardMarkup:
    """Prices input options in readiness sub-flow."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="Добавить текстом",
                    callback_data="pipeline:readiness:prices:text",
                ),
            ],
            [
                InlineKeyboardButton(
                    text="Загрузить Excel",
                    callback_data="pipeline:readiness:prices:excel",
                ),
            ],
            [
                InlineKeyboardButton(
                    text="К чеклисту",
                    callback_data="pipeline:readiness:back",
                ),
            ],
        ]
    )


# ---------------------------------------------------------------------------
# Readiness sub-flow: Images
# ---------------------------------------------------------------------------


def pipeline_images_options_kb(current_count: int = 4) -> InlineKeyboardMarkup:
    """Image count options in readiness sub-flow."""
    options = [0, 1, 2, 3, 4, 6, 8, 10]
    rows: list[list[InlineKeyboardButton]] = []

    row: list[InlineKeyboardButton] = []
    for count in options:
        label = f"{count}" if count != current_count else f"[{count}]"
        row.append(
            InlineKeyboardButton(
                text=label,
                callback_data=f"pipeline:readiness:images:{count}",
            )
        )
        if len(row) == 4:
            rows.append(row)
            row = []
    if row:
        rows.append(row)

    rows.append(
        [
            InlineKeyboardButton(
                text="К чеклисту",
                callback_data="pipeline:readiness:back",
            ),
        ]
    )

    return InlineKeyboardMarkup(inline_keyboard=rows)


def pipeline_back_to_checklist_kb(prefix: str = "pipeline:readiness") -> InlineKeyboardMarkup:
    """Single 'Back to checklist' button for text-input sub-flows (M5)."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="К чеклисту",
                    callback_data=f"{prefix}:back",
                ),
            ],
        ]
    )
