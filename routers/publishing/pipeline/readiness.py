"""Readiness check handlers for Article Pipeline step 4 (F5.3).

Sub-flows: keywords (auto/configure/upload), description (AI/manual),
prices (text/Excel), images (count selection).
UX: UX_PIPELINE.md SS4.1 step 4, SS4.4 progressive readiness.
Rules: .claude/rules/pipeline.md -- inline handlers, NOT FSM delegation.

Common keyword/description/navigation sub-flows are registered via
register_readiness_subflows() from _readiness_common.py.
Article-specific sub-flows (prices, images) and checklist/show functions remain here.
"""

from __future__ import annotations

import html

import structlog
from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from bot.config import get_settings
from bot.helpers import safe_edit_text, safe_message
from bot.service_factory import CategoryServiceFactory
from bot.texts import strings as S
from bot.texts.emoji import E
from bot.texts.screens import Screen
from cache.client import RedisClient
from db.client import SupabaseClient
from db.models import User
from keyboards.inline import cancel_kb, menu_kb
from keyboards.pipeline import (
    pipeline_images_options_kb,
    pipeline_prices_options_kb,
    pipeline_readiness_kb,
)
from routers.publishing.pipeline._common import (
    ArticlePipelineFSM,
    clear_checkpoint,
    save_checkpoint,
)
from routers.publishing.pipeline._readiness_common import (
    ReadinessConfig,
    register_readiness_subflows,
    run_keyword_generation,
)
from services.readiness import ReadinessReport, ReadinessService
from services.tokens import (
    COST_PER_IMAGE,
    TokenService,
)

log = structlog.get_logger()
router = Router()

# Limits for prices (mirrors categories/prices.py)
_MAX_PRICE_ROWS = 1000
_MAX_PRICE_TEXT_LEN = 50_000
_MAX_EXCEL_FILE_SIZE = 5 * 1024 * 1024  # 5 MB


# ---------------------------------------------------------------------------
# Checklist display helpers
# ---------------------------------------------------------------------------


def _build_checklist_text(report: ReadinessReport, fsm_data: dict) -> str:  # type: ignore[type-arg]
    """Build readiness checklist text (UX_PIPELINE.md SS4.1 step 4)."""
    project_name = html.escape(fsm_data.get("project_name", ""))
    category_name = html.escape(fsm_data.get("category_name", ""))

    s = Screen(E.DOC, S.PIPELINE_READINESS_TITLE)
    s.progress(4, 5, "Готовность")
    s.blank()
    s.line(f"{E.FOLDER} Проект: {project_name}")
    s.line(f"{E.HASHTAG} Тема: {category_name}")
    s.blank()

    # Description status (first -- keywords are generated from it)
    if report.has_description:
        s.check("Описание", ok=True)
    elif "description" in report.missing_items:
        s.check("Описание", ok=False, detail="не задано")

    # Keywords status
    if report.has_keywords:
        kw_info = f"{report.keyword_count} фраз"
        if report.cluster_count:
            kw_info = f"{report.cluster_count} групп ({report.keyword_count} фраз)"
        s.check("Ключевики", ok=True, detail=kw_info)
    else:
        s.check("Ключевики (обязательно)", ok=False)

    # Prices status (progressive: shown for 2+ pubs)
    if "prices" in report.missing_items:
        s.check("Цены", ok=False, detail="не заданы")
    elif report.has_prices:
        s.check("Цены", ok=True)

    # Images (generated WITH the article, not separately)
    if report.image_count > 0:
        img_cost = report.image_count * COST_PER_IMAGE
        s.check("Медиа", ok=True, detail=f"{report.image_count} шт. ({img_cost} ток.)")
    else:
        s.check("Медиа", ok=False)

    # Cost estimate
    s.separator()
    s.line(f"{E.WALLET} " + S.PIPELINE_COST_NORMAL.format(cost=report.estimated_cost))

    return s.build()


async def show_readiness_check(
    callback: CallbackQuery,
    state: FSMContext,
    user: User,
    db: SupabaseClient,
    redis: RedisClient,
    *,
    force_show: bool = False,
) -> None:
    """Render readiness checklist (step 4) or skip to step 5 if all filled.

    Called from article.py after category selection, and after each sub-flow completes.
    When force_show=True (e.g. "back to checklist" from confirm), always show checklist.
    """
    msg = safe_message(callback)
    if not msg:
        return

    data = await state.get_data()
    category_id = data.get("category_id")
    project_id = data.get("project_id")
    project_name = data.get("project_name", "")
    if not category_id:
        await safe_edit_text(msg, S.PIPELINE_CATEGORY_NOT_SET, reply_markup=menu_kb())
        await state.clear()
        await clear_checkpoint(redis, user.id)
        return

    image_count = data.get("image_count", 0)

    settings = get_settings()
    token_svc = TokenService(db=db, admin_ids=settings.admin_ids)
    balance = await token_svc.get_balance(user.id)

    svc = ReadinessService(db)
    report = await svc.check(user.id, category_id, balance, image_count)

    # Skip to step 5 if all required items filled (unless forced back)
    if not force_show and report.all_filled and not report.missing_items:
        from routers.publishing.pipeline.generation import show_confirm

        await show_confirm(callback, state, user, redis, report, data)
        return

    text = _build_checklist_text(report, data)
    kb = pipeline_readiness_kb(report)
    await safe_edit_text(msg, text, reply_markup=kb)
    await state.set_state(ArticlePipelineFSM.readiness_check)
    await state.update_data(image_count=image_count)
    await save_checkpoint(
        redis,
        user.id,
        current_step="readiness_check",
        project_id=project_id,
        project_name=project_name,
        category_id=category_id,
    )


async def show_readiness_check_msg(
    message: Message,
    state: FSMContext,
    user: User,
    db: SupabaseClient,
    redis: RedisClient,
) -> None:
    """Render readiness checklist via new message (after text/file input sub-flows)."""
    data = await state.get_data()
    category_id = data.get("category_id")
    project_id = data.get("project_id")
    project_name = data.get("project_name", "")
    if not category_id:
        await message.answer(S.PIPELINE_CATEGORY_NOT_SET, reply_markup=menu_kb())
        await state.clear()
        await clear_checkpoint(redis, user.id)
        return

    image_count = data.get("image_count", 0)

    settings = get_settings()
    token_svc = TokenService(db=db, admin_ids=settings.admin_ids)
    balance = await token_svc.get_balance(user.id)

    svc = ReadinessService(db)
    report = await svc.check(user.id, category_id, balance, image_count)

    if report.all_filled and not report.missing_items:
        from routers.publishing.pipeline.generation import show_confirm_msg

        await show_confirm_msg(message, state, user, redis, report, data)
        return

    text = _build_checklist_text(report, data)
    kb = pipeline_readiness_kb(report)
    await message.answer(text, reply_markup=kb)
    await state.set_state(ArticlePipelineFSM.readiness_check)
    await state.update_data(image_count=image_count)
    await save_checkpoint(
        redis,
        user.id,
        current_step="readiness_check",
        project_id=project_id,
        project_name=project_name,
        category_id=category_id,
    )


# ---------------------------------------------------------------------------
# Register shared sub-flows (keywords, description, navigation)
# ---------------------------------------------------------------------------

_article_config = ReadinessConfig(
    fsm_class=ArticlePipelineFSM,
    prefix="pipeline:readiness",
    cancel_cb="pipeline:article:cancel",
    log_prefix="pipeline.readiness",
    show_check=show_readiness_check,
    show_check_msg=show_readiness_check_msg,
    description_hint="статьи",
    charge_suffix="pipeline",
    extra_back_states=[ArticlePipelineFSM.readiness_prices],
)

_handlers = register_readiness_subflows(router, _article_config)

# Re-export handler functions for backward-compatible test imports
readiness_keywords_menu = _handlers["readiness_keywords_menu"]
readiness_keywords_auto = _handlers["readiness_keywords_auto"]
readiness_keywords_configure = _handlers["readiness_keywords_configure"]
readiness_keywords_upload_start = _handlers["readiness_keywords_upload_start"]
readiness_keywords_upload_file = _handlers["readiness_keywords_upload_file"]
readiness_keywords_text_input = _handlers["readiness_keywords_text_input"]
_handle_configure_products = _handlers["_handle_configure_products"]
readiness_keywords_city_select = _handlers["readiness_keywords_city_select"]
readiness_keywords_geo_input = _handlers["readiness_keywords_geo_input"]
readiness_keywords_cancel = _handlers["readiness_keywords_cancel"]
readiness_description_menu = _handlers["readiness_description_menu"]
readiness_description_ai = _handlers["readiness_description_ai"]
readiness_description_manual_start = _handlers["readiness_description_manual_start"]
readiness_description_manual_input = _handlers["readiness_description_manual_input"]
readiness_back_from_input = _handlers["readiness_back_from_input"]
readiness_back = _handlers["readiness_back"]

# Re-export run_keyword_generation as _run_pipeline_keyword_generation
# for backward-compatible test imports (same function, same signature).
_run_pipeline_keyword_generation = run_keyword_generation

# Expose config for test patching (closures capture cfg, not module-level names).
# Tests can use object.__setattr__(_article_readiness_config, "show_check", AsyncMock())
# to intercept cfg.show_check() calls inside closure handlers.
_article_readiness_config = _article_config


# ---------------------------------------------------------------------------
# Prices sub-flow (4c) — article-specific
# ---------------------------------------------------------------------------


@router.callback_query(
    ArticlePipelineFSM.readiness_check,
    F.data == "pipeline:readiness:prices",
)
async def readiness_prices_menu(callback: CallbackQuery) -> None:
    """Show prices input options."""
    msg = safe_message(callback)
    if not msg:
        await callback.answer()
        return

    await safe_edit_text(msg, 
        S.PIPELINE_PRICES_QUESTION,
        reply_markup=pipeline_prices_options_kb(),
    )
    await callback.answer()


@router.callback_query(
    ArticlePipelineFSM.readiness_check,
    F.data == "pipeline:readiness:prices:text",
)
async def readiness_prices_text_start(
    callback: CallbackQuery,
    state: FSMContext,
) -> None:
    """Start prices text input sub-flow."""
    msg = safe_message(callback)
    if not msg:
        await callback.answer()
        return

    await safe_edit_text(msg, 
        S.PIPELINE_PRICES_TEXT_PROMPT,
        reply_markup=pipeline_prices_options_kb(),
    )
    await state.set_state(ArticlePipelineFSM.readiness_prices)
    await callback.answer()


@router.message(ArticlePipelineFSM.readiness_prices, F.text)
async def readiness_prices_text_input(
    message: Message,
    state: FSMContext,
    user: User,
    db: SupabaseClient,
    redis: RedisClient,
    category_service_factory: CategoryServiceFactory,
) -> None:
    """Save prices from text input."""
    text = (message.text or "").strip()
    if not text:
        await message.answer(
            S.PIPELINE_PRICES_TEXT_FALLBACK,
            reply_markup=cancel_kb("pipeline:article:cancel"),
        )
        return

    if len(text) > _MAX_PRICE_TEXT_LEN:
        await message.answer(
            S.PIPELINE_PRICES_TOO_LONG,
            reply_markup=cancel_kb("pipeline:article:cancel"),
        )
        return

    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    if len(lines) > _MAX_PRICE_ROWS:
        await message.answer(
            S.PIPELINE_PRICES_MAX_ROWS.format(max=_MAX_PRICE_ROWS, count=len(lines)),
            reply_markup=cancel_kb("pipeline:article:cancel"),
        )
        return

    data = await state.get_data()
    category_id = data.get("category_id")
    if not category_id:
        await message.answer(S.PIPELINE_PRICES_CAT_MISSING, reply_markup=menu_kb())
        return

    prices_text = "\n".join(lines)
    cat_svc = category_service_factory(db)
    saved = await cat_svc.update_prices(category_id, user.id, prices_text)
    if not saved:
        log.error("pipeline.readiness.prices_save_failed", category_id=category_id, user_id=user.id)
        await message.answer(S.PIPELINE_PRICES_SAVE_ERROR)
        return

    log.info(
        "pipeline.readiness.prices_text",
        user_id=user.id,
        category_id=category_id,
        lines_count=len(lines),
    )

    await message.answer(S.PIPELINE_PRICES_SAVED.format(count=len(lines)))
    await show_readiness_check_msg(message, state, user, db, redis)


@router.callback_query(
    ArticlePipelineFSM.readiness_check,
    F.data == "pipeline:readiness:prices:excel",
)
async def readiness_prices_excel_start(
    callback: CallbackQuery,
    state: FSMContext,
) -> None:
    """Start prices Excel upload sub-flow."""
    msg = safe_message(callback)
    if not msg:
        await callback.answer()
        return

    await safe_edit_text(msg, 
        S.PIPELINE_PRICES_EXCEL_PROMPT,
        reply_markup=pipeline_prices_options_kb(),
    )
    await state.set_state(ArticlePipelineFSM.readiness_prices)
    await callback.answer()


@router.message(ArticlePipelineFSM.readiness_prices, F.document)
async def readiness_prices_excel_file(
    message: Message,
    state: FSMContext,
    user: User,
    db: SupabaseClient,
    redis: RedisClient,
    category_service_factory: CategoryServiceFactory,
) -> None:
    """Process uploaded Excel file with prices."""
    doc = message.document
    if not doc:
        await message.answer(S.PIPELINE_FILE_NOT_FOUND)
        return

    filename = doc.file_name or ""
    if not filename.lower().endswith(".xlsx"):
        await message.answer(
            S.PIPELINE_FILE_WRONG_FORMAT,
            reply_markup=cancel_kb("pipeline:article:cancel"),
        )
        return

    if doc.file_size and doc.file_size > _MAX_EXCEL_FILE_SIZE:
        await message.answer(
            S.PIPELINE_FILE_TOO_BIG,
            reply_markup=cancel_kb("pipeline:article:cancel"),
        )
        return

    file = await message.bot.download(doc)  # type: ignore[union-attr]
    if file is None:
        await message.answer(S.PIPELINE_FILE_DOWNLOAD_ERROR)
        return

    file_bytes = file.read()

    # Reuse Excel parsing from prices module
    from routers.categories.prices import parse_excel_rows

    result = parse_excel_rows(file_bytes)

    if isinstance(result, str):
        error_msgs: dict[str, str] = {
            "empty": S.PIPELINE_FILE_EMPTY,
            "too_many_rows": S.PIPELINE_FILE_TOO_MANY_ROWS.format(max=_MAX_PRICE_ROWS),
        }
        await message.answer(
            error_msgs.get(result, S.PIPELINE_FILE_READ_ERROR),
            reply_markup=cancel_kb("pipeline:article:cancel"),
        )
        return

    if not result:
        await message.answer(
            S.PIPELINE_FILE_EXTRACT_ERROR,
            reply_markup=cancel_kb("pipeline:article:cancel"),
        )
        return

    data = await state.get_data()
    category_id = data.get("category_id")
    if not category_id:
        await message.answer(S.PIPELINE_PRICES_CAT_MISSING, reply_markup=menu_kb())
        return

    prices_text = "\n".join(result)
    cat_svc = category_service_factory(db)
    saved = await cat_svc.update_prices(category_id, user.id, prices_text)
    if not saved:
        log.error("pipeline.readiness.prices_excel_failed", category_id=category_id, user_id=user.id)
        await message.answer(S.PIPELINE_PRICES_SAVE_ERROR)
        return

    log.info(
        "pipeline.readiness.prices_excel",
        user_id=user.id,
        category_id=category_id,
        lines_count=len(result),
    )

    await message.answer(S.PIPELINE_PRICES_EXCEL_UPLOADED.format(count=len(result)))
    await show_readiness_check_msg(message, state, user, db, redis)


# ---------------------------------------------------------------------------
# Images sub-flow (4d) — article-specific
# ---------------------------------------------------------------------------


@router.callback_query(
    ArticlePipelineFSM.readiness_check,
    F.data == "pipeline:readiness:images",
)
async def readiness_images_menu(callback: CallbackQuery, state: FSMContext) -> None:
    """Show image count selection."""
    msg = safe_message(callback)
    if not msg:
        await callback.answer()
        return

    data = await state.get_data()
    current_count = data.get("image_count", 0)

    await safe_edit_text(msg, 
        S.PIPELINE_IMAGES_CURRENT.format(count=current_count),
        reply_markup=pipeline_images_options_kb(current_count),
    )
    await callback.answer()


@router.callback_query(
    ArticlePipelineFSM.readiness_check,
    F.data.regexp(r"^pipeline:readiness:images:(\d+)$"),
)
async def readiness_images_select(
    callback: CallbackQuery,
    state: FSMContext,
    user: User,
    db: SupabaseClient,
    redis: RedisClient,
) -> None:
    """Handle image count selection and return to checklist."""
    if not callback.data:
        await callback.answer()
        return

    try:
        count = int(callback.data.split(":")[-1])
    except ValueError:
        await callback.answer()
        return

    allowed = {0, 1, 2, 3, 4, 6, 8, 10}
    if count not in allowed:
        log.warning("pipeline.readiness.images_invalid_count", user_id=user.id, count=count)
        await callback.answer("Некорректное количество.", show_alert=True)
        return

    await state.update_data(image_count=count)

    log.info("pipeline.readiness.images_count", user_id=user.id, count=count)
    await callback.answer(f"Изображений: {count}")
    await show_readiness_check(callback, state, user, db, redis)


# ---------------------------------------------------------------------------
# Done (step 5 transition) — article-specific
# ---------------------------------------------------------------------------


@router.callback_query(
    ArticlePipelineFSM.readiness_check,
    F.data == "pipeline:readiness:done",
)
async def readiness_done(
    callback: CallbackQuery,
    state: FSMContext,
    user: User,
    db: SupabaseClient,
    redis: RedisClient,
) -> None:
    """Proceed to step 5 (confirmation). Keywords are required blocker."""
    msg = safe_message(callback)
    if not msg:
        await callback.answer()
        return

    data = await state.get_data()
    category_id = data.get("category_id")
    if not category_id:
        await callback.answer("Категория не найдена.", show_alert=True)
        return

    image_count = data.get("image_count", 0)
    settings = get_settings()
    token_svc = TokenService(db=db, admin_ids=settings.admin_ids)
    balance = await token_svc.get_balance(user.id)

    svc = ReadinessService(db)
    report = await svc.check(user.id, category_id, balance, image_count)

    if report.has_blockers:
        await callback.answer(
            "Добавьте ключевые фразы — это обязательный пункт.",
            show_alert=True,
        )
        return

    from routers.publishing.pipeline.generation import show_confirm

    await show_confirm(callback, state, user, redis, report, data)
    await callback.answer()
