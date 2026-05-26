"""Unified keyword generation/upload wizard shared between Pipeline and Toolbox.

Extracted from routers/publishing/pipeline/_readiness_common.py (keyword sub-flow)
and routers/categories/keywords.py (sections 3-7) to eliminate duplication.

Keywords are FREE everywhere -- no token charging.
"""

from __future__ import annotations

import asyncio
import contextlib
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

import structlog
from aiogram import F, Router
from aiogram.filters import StateFilter
from aiogram.fsm.state import State
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, Message

from bot.custom_emoji import EMOJI_DONE, EMOJI_PROGRESS
from bot.helpers import safe_edit_text, safe_message
from bot.texts import strings as S
from bot.texts.emoji import E
from db.models import User
from keyboards.inline import cancel_kb, menu_kb
from services.categories import CategoryService
from services.keywords import KeywordService

if TYPE_CHECKING:
    from aiogram.fsm.context import FSMContext

    from bot.service_factory import CategoryServiceFactory, ProjectServiceFactory
    from cache.client import RedisClient
    from db.client import SupabaseClient
    from services.ai.orchestrator import AIOrchestrator
    from services.external.dataforseo import DataForSEOClient

log = structlog.get_logger()

# Limits for keyword upload (shared between all callers)
MAX_KEYWORD_PHRASES = 500
MAX_KEYWORD_FILE_SIZE = 1 * 1024 * 1024  # 1 MB


# ---------------------------------------------------------------------------
# Type aliases (re-exported from _readiness_common for compatibility)
# ---------------------------------------------------------------------------

ShowCheckCb = Callable[
    [CallbackQuery, "FSMContext", User, "SupabaseClient", "RedisClient"],
    Awaitable[None],
]
ShowCheckMsgCb = Callable[
    [Message, "FSMContext", User, "SupabaseClient", "RedisClient"],
    Awaitable[None],
]


# ---------------------------------------------------------------------------
# KeywordWizardConfig
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class KeywordWizardConfig:
    """Configuration for the unified keyword wizard.

    Parameterizes differences between Pipeline readiness sub-flow
    and Toolbox keyword generation/upload.
    """

    state_products: State
    """FSM state for waiting for products text / upload file."""

    state_geo: State
    """FSM state for waiting for geography text."""

    state_generating: State
    """FSM state during generation progress."""

    prefix: str
    """Callback prefix: 'pipeline:readiness' or 'kw'."""

    log_prefix: str
    """Structlog event prefix."""

    cancel_cb_fn: Callable[[dict[str, Any]], str]
    """Dynamic cancel callback_data builder -- reads cat_id from FSM data."""

    on_done: ShowCheckCb
    """Return callback after wizard completes (callback sub-flows)."""

    on_done_msg: ShowCheckMsgCb
    """Return callback after wizard completes (message sub-flows)."""

    cancel_nav_kb_fn: Callable[[dict[str, Any]], InlineKeyboardMarkup]
    """Cancel/back navigation keyboard for text input prompts."""

    error_state: State | None = None
    """FSM state to set on generation failure (None -> clear state)."""

    error_kb_fn: Callable[[dict[str, Any]], InlineKeyboardMarkup] | None = None
    """Error keyboard builder (receives FSM data dict). None -> no keyboard."""

    auto_mode: bool = False
    """Pipeline-only: auto-generate from category.name (enables city select)."""

    upload_enriches: bool = True
    """Toolbox: cluster+enrich uploaded phrases. Pipeline: save as flat keywords."""


# ---------------------------------------------------------------------------
# Error messages (specific + actionable)
# ---------------------------------------------------------------------------


def _keyword_error_message(exc: Exception) -> str:
    """Map exception to user-friendly keyword-specific error message."""
    import math

    from bot.exceptions import AIGenerationError, RateLimitError

    if isinstance(exc, RateLimitError):
        secs = exc.retry_after_seconds
        if secs < 60:
            return f"Слишком много запросов на подбор фраз. Подождите {max(secs, 1)} сек."
        minutes = math.ceil(secs / 60)
        return f"Слишком много запросов на подбор фраз. Подождите {minutes} мин."

    if isinstance(exc, AIGenerationError):
        msg_lower = exc.message.lower()
        if "content" in msg_lower and "filter" in msg_lower:
            return (
                "ИИ не смог обработать запрос — возможно, тема или география "
                "слишком чувствительные.\n\nПопробуйте изменить географию "
                "или переформулировать описание категории."
            )
        if "truncated" in msg_lower:
            return "Ответ ИИ оказался слишком длинным. Попробуйте ещё раз."
        if "timeout" in msg_lower or "timed out" in msg_lower:
            return "ИИ не успел ответить вовремя. Попробуйте ещё раз через минуту."
        return "ИИ не смог подобрать фразы. Попробуйте ещё раз или измените описание категории."

    if isinstance(exc, RuntimeError) and "ownership" in str(exc):
        return "Категория не найдена или была удалена."

    return "Не удалось подобрать фразы. Попробуйте позже."


# ---------------------------------------------------------------------------
# Progress display (cumulative loader, matches article/social style)
# ---------------------------------------------------------------------------

# Steps: (active_label, done_label)
_KW_STEPS: list[tuple[str, str]] = [
    (S.KW_STEP_FETCH, S.KW_STEP_FETCH_DONE),
    (S.KW_STEP_CLUSTER, S.KW_STEP_CLUSTER_DONE),
    (S.KW_STEP_ENRICH, S.KW_STEP_ENRICH_DONE),
]

# Upload pipeline steps
_KW_UPLOAD_STEPS: list[tuple[str, str]] = [
    (S.KW_STEP_CLUSTER, S.KW_STEP_CLUSTER_DONE),
    (S.KW_STEP_ENRICH, S.KW_STEP_ENRICH_DONE),
]


def _kw_progress_text(steps: list[tuple[str, str]], current: int, extra: str = "") -> str:
    """Build cumulative progress text for keyword generation."""
    lines = [f"{E.HASHTAG} <b>{S.KW_PROGRESS_TITLE}</b>", ""]
    for i, (active_label, done_label) in enumerate(steps):
        if i < current:
            label = done_label
            if i == current - 1 and extra:
                label = f"{done_label} ({extra})"
            lines.append(f"{EMOJI_DONE} {label}")
        elif i == current:
            lines.append(f"{EMOJI_PROGRESS} {active_label}...")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Core generation pipeline
# ---------------------------------------------------------------------------


async def run_keyword_generation(
    progress_msg: Message,
    state: FSMContext,
    user: User,
    db: SupabaseClient,
    redis: RedisClient,
    *,
    cfg: KeywordWizardConfig,
    category_id: int,
    project_id: int,
    products: str,
    geography: str,
    ai_orchestrator: AIOrchestrator,
    dataforseo_client: DataForSEOClient,
    target_phrases: int = 100,
) -> None:
    """Run keyword pipeline: fetch -> cluster -> enrich -> save.

    AI clustering (DeepSeek) can take 60-90 seconds. Progress updates use
    _safe_edit (tolerates Telegram errors) and the final result is sent
    as a NEW message.

    Args:
        progress_msg: Message to edit for progress updates (caller sends it).
    """
    log_prefix = cfg.log_prefix

    async def _safe_edit(text: str) -> None:
        """Edit message, silently ignoring Telegram errors."""
        try:
            await safe_edit_text(progress_msg, text)
        except Exception:
            log.debug(f"{log_prefix}.edit_failed", text=text[:50])

    try:
        kw_service = KeywordService(
            orchestrator=ai_orchestrator,
            dataforseo=dataforseo_client,
            db=db,
        )

        # Step 1: Fetch raw phrases from DataForSEO (~1-3s)
        await _safe_edit(_kw_progress_text(_KW_STEPS, 0))
        raw_phrases = await kw_service.fetch_raw_phrases(
            products=products,
            geography=geography,
            project_id=project_id,
            user_id=user.id,
        )

        if raw_phrases:
            # Step 2a: DataForSEO had data -> AI clustering (~60-90s)
            await _safe_edit(
                _kw_progress_text(_KW_STEPS, 1, f"{len(raw_phrases)} фраз")
            )
            clusters = await kw_service.cluster_phrases(
                raw_phrases=raw_phrases,
                products=products,
                geography=geography,
                project_id=project_id,
                user_id=user.id,
                target_phrases=target_phrases,
            )
        else:
            # Step 2b: DataForSEO empty -> AI generates clusters directly
            await _safe_edit(
                _kw_progress_text(_KW_STEPS, 1)
            )
            clusters = await kw_service.generate_clusters_direct(
                products=products,
                geography=geography,
                project_id=project_id,
                user_id=user.id,
                target_phrases=target_phrases,
            )

        # Step 3: Enrich with metrics (~3s)
        await _safe_edit(
            _kw_progress_text(_KW_STEPS, 2, f"{len(clusters)} групп")
        )
        enriched = await kw_service.enrich_clusters(clusters)

        # Filter AI-invented zero-volume junk
        enriched = kw_service.filter_low_quality(enriched, min_keep=max(60, int(target_phrases * 0.6)))

        # Save (MERGE with existing) via CategoryService
        cat_svc = CategoryService(db=db)
        category = await cat_svc.get_owned_category(category_id, user.id)
        if not category:
            raise RuntimeError("keywords_save_failed.ownership")
        existing: list[dict[str, Any]] = category.keywords or []
        merged = existing + enriched
        saved = await cat_svc.update_keywords(category_id, user.id, merged)
        if not saved:
            raise RuntimeError("keywords_save_failed.update")

        total_phrases = sum(len(c.get("phrases", [])) for c in enriched)
        total_volume = sum(c.get("total_volume", 0) for c in enriched)

        log.info(
            f"{log_prefix}.keywords_generated",
            user_id=user.id,
            category_id=category_id,
            clusters=len(enriched),
            phrases=total_phrases,
        )

    except Exception as exc:
        log.exception(
            f"{log_prefix}.keywords_failed",
            user_id=user.id,
            category_id=category_id,
        )
        # Send error as new message (original may be expired)
        with contextlib.suppress(Exception):
            await progress_msg.delete()
        bot = progress_msg.bot
        if not bot:
            return

        error_text = _keyword_error_message(exc)

        fsm_data = await state.get_data()
        error_kb = cfg.error_kb_fn(fsm_data) if cfg.error_kb_fn else None
        await bot.send_message(
            chat_id=progress_msg.chat.id,
            text=error_text,
            reply_markup=error_kb,
        )
        if cfg.error_state is not None:
            await state.set_state(cfg.error_state)
        else:
            await state.clear()
        return

    # Post-success UI — save chat_id before deleting message
    chat_id = progress_msg.chat.id
    bot = progress_msg.bot
    try:
        await progress_msg.delete()
    except Exception:
        log.debug(f"{log_prefix}.delete_progress_failed")

    volume_line = f"\nОбщий объём: {total_volume:,}/мес" if total_volume > 0 else ""
    if not bot:
        return
    with contextlib.suppress(Exception):
        done_text = (
            f"{E.HASHTAG} <b>{S.KW_PROGRESS_TITLE}</b>\n\n"
            f"{EMOJI_DONE} {S.KW_STEP_FETCH_DONE}\n"
            f"{EMOJI_DONE} {S.KW_STEP_CLUSTER_DONE}\n"
            f"{EMOJI_DONE} {S.KW_STEP_ENRICH_DONE}\n\n"
            f"{E.CHECK} {S.KW_RESULT_ADDED.format(clusters=len(enriched), phrases=total_phrases)}"
            f"{volume_line}"
        )
        await bot.send_message(chat_id=chat_id, text=done_text)
    await asyncio.sleep(1)
    with contextlib.suppress(Exception):
        await cfg.on_done_msg(progress_msg, state, user, db, redis)


# ---------------------------------------------------------------------------
# Upload pipeline (cluster + enrich) for toolbox mode
# ---------------------------------------------------------------------------


async def _run_upload_enrich_pipeline(
    msg: Message,
    state: FSMContext,
    user: User,
    db: SupabaseClient,
    *,
    cfg: KeywordWizardConfig,
    cat_id: int,
    project_id: int,
    raw_phrases: list[dict[str, Any]],
    ai_orchestrator: AIOrchestrator,
    dataforseo_client: DataForSEOClient,
) -> None:
    """Upload pipeline: cluster -> enrich -> save (no charge -- upload is free)."""
    log_prefix = cfg.log_prefix

    try:
        kw_service = KeywordService(
            orchestrator=ai_orchestrator,
            dataforseo=dataforseo_client,
            db=db,
        )

        # Step 1: Cluster
        progress_msg = await msg.answer(
            _kw_progress_text(_KW_UPLOAD_STEPS, 0, f"{len(raw_phrases)} фраз загружено")
        )

        clusters = await kw_service.cluster_phrases(
            raw_phrases=raw_phrases,
            products="",
            geography="",
            project_id=project_id,
            user_id=user.id,
        )

        # Step 2: Enrich
        await safe_edit_text(
            progress_msg,
            _kw_progress_text(_KW_UPLOAD_STEPS, 1, f"{len(clusters)} групп"),
        )

        enriched = await kw_service.enrich_clusters(clusters)

        # Save (MERGE with existing)
        cat_svc = CategoryService(db=db)
        category = await cat_svc.get_owned_category(cat_id, user.id)
        if not category:
            raise RuntimeError(f"Category {cat_id} not found or not owned by user {user.id}")
        existing: list[dict[str, Any]] = category.keywords or []
        merged = existing + enriched
        await cat_svc.update_keywords(cat_id, user.id, merged)

        total_phrases = sum(len(c.get("phrases", [])) for c in enriched)
        total_volume = sum(c.get("total_volume", 0) for c in enriched)

        volume_line = f"\nОбщий объём: {total_volume:,}/мес" if total_volume > 0 else ""
        done_text = (
            f"{E.HASHTAG} <b>{S.KW_PROGRESS_TITLE}</b>\n\n"
            f"{EMOJI_DONE} {S.KW_STEP_CLUSTER_DONE}\n"
            f"{EMOJI_DONE} {S.KW_STEP_ENRICH_DONE}\n\n"
            f"{E.CHECK} {S.KW_RESULT_UPLOADED.format(clusters=len(enriched), phrases=total_phrases)}"
            f"{volume_line}"
        )
        await safe_edit_text(progress_msg, done_text)
        await state.clear()

        log.info(
            f"{log_prefix}.keywords_uploaded",
            cat_id=cat_id,
            user_id=user.id,
            clusters=len(enriched),
        )

    except Exception as exc:
        log.exception(f"{log_prefix}.upload_pipeline_failed", cat_id=cat_id, user_id=user.id)
        fsm_data = await state.get_data()
        await state.clear()

        error_text = _keyword_error_message(exc)
        error_kb = cfg.error_kb_fn(fsm_data) if cfg.error_kb_fn else None
        await msg.answer(error_text, reply_markup=error_kb)


# ---------------------------------------------------------------------------
# Handler factory
# ---------------------------------------------------------------------------


def register_keyword_wizard(router: Router, cfg: KeywordWizardConfig) -> dict[str, Any]:  # noqa: C901
    """Register keyword wizard handlers on the given router.

    Handles: products text/upload, geo text/city select, qty select,
    confirm, cancel. Entry points (menu, auto, configure, upload_start)
    are NOT registered here -- they stay in the caller module.

    Returns a dict mapping handler names to functions (for test imports).

    Note: C901 complexity is expected -- factory registering 7+ handlers.
    """
    prefix = cfg.prefix
    log_prefix = cfg.log_prefix

    # -----------------------------------------------------------------------
    # Products text input (routes by kw_mode: "configure" or "upload")
    # -----------------------------------------------------------------------

    async def wizard_products_text(
        message: Message,
        state: FSMContext,
        user: User,
        db: SupabaseClient,
        redis: RedisClient,
        category_service_factory: CategoryServiceFactory,
    ) -> None:
        """Handle text input -- route by kw_mode (configure=products, upload=phrases)."""
        data = await state.get_data()
        kw_mode = data.get("kw_mode", "upload")

        if kw_mode == "configure":
            # Validate products (3-1000 chars)
            text = (message.text or "").strip()
            if len(text) < 3 or len(text) > 1000:
                cancel_cb = cfg.cancel_cb_fn(data)
                await message.answer(
                    "Введите от 3 до 1000 символов.",
                    reply_markup=cfg.cancel_nav_kb_fn(data),
                )
                return

            await state.set_state(cfg.state_geo)
            await state.update_data(kw_products=text)

            await message.answer(
                "Укажите географию продвижения:\n<i>Например: Москва, Россия, СНГ</i>\n\nОт 2 до 200 символов.",
                reply_markup=cfg.cancel_nav_kb_fn(data),
            )
            return

        # Upload mode: process text as keyword phrases (one per line)
        text = (message.text or "").strip()
        cancel_cb = cfg.cancel_cb_fn(data)
        if not text:
            await message.answer(
                "Отправьте TXT-файл или введите фразы текстом (каждая с новой строки).",
                reply_markup=cancel_kb(cancel_cb),
            )
            return

        phrases = [line.strip() for line in text.splitlines() if line.strip()]
        if not phrases:
            await message.answer(
                "Не удалось распознать фразы. Каждая фраза -- с новой строки.",
                reply_markup=cancel_kb(cancel_cb),
            )
            return

        if len(phrases) > MAX_KEYWORD_PHRASES:
            await message.answer(
                f"Максимум {MAX_KEYWORD_PHRASES} фраз. Сейчас: {len(phrases)}.",
                reply_markup=cancel_kb(cancel_cb),
            )
            return

        category_id = data.get("category_id") or data.get("kw_cat_id")
        if not category_id:
            await message.answer("Категория не найдена. Начните заново.", reply_markup=menu_kb())
            return

        keywords = [{"phrase": p, "volume": 0, "cpc": 0.0} for p in phrases]
        cat_svc = category_service_factory(db)
        saved = await cat_svc.update_keywords(int(category_id), user.id, keywords)
        if not saved:
            log.error(f"{log_prefix}.keywords_text_failed", category_id=category_id, user_id=user.id)
            await message.answer("Не удалось сохранить фразы. Попробуйте снова.")
            return

        log.info(
            f"{log_prefix}.keywords_text",
            user_id=user.id,
            category_id=category_id,
            phrase_count=len(phrases),
        )

        await message.answer(f"Сохранено {len(phrases)} фраз.")
        await cfg.on_done_msg(message, state, user, db, redis)

    router.message.register(
        wizard_products_text,
        cfg.state_products,
        F.text,
    )

    # -----------------------------------------------------------------------
    # Upload file handler
    # -----------------------------------------------------------------------

    async def wizard_upload_file(
        message: Message,
        state: FSMContext,
        user: User,
        db: SupabaseClient,
        redis: RedisClient,
        category_service_factory: CategoryServiceFactory,
        ai_orchestrator: AIOrchestrator | None = None,
        dataforseo_client: DataForSEOClient | None = None,
    ) -> None:
        """Process uploaded TXT file with keyword phrases."""
        doc = message.document
        if not doc:
            await message.answer("Файл не найден.")
            return

        data = await state.get_data()
        cancel_cb = cfg.cancel_cb_fn(data)

        filename = doc.file_name or ""
        if not filename.lower().endswith(".txt"):
            await message.answer(
                "Нужен .txt файл (UTF-8), одна фраза на строку.",
                reply_markup=cancel_kb(cancel_cb),
            )
            return

        if doc.file_size and doc.file_size > MAX_KEYWORD_FILE_SIZE:
            await message.answer(
                "Файл слишком большой (макс. 1 МБ).",
                reply_markup=cancel_kb(cancel_cb),
            )
            return

        file = await message.bot.download(doc)  # type: ignore[union-attr]
        if file is None:
            await message.answer("Не удалось загрузить файл.")
            return

        content = file.read().decode("utf-8", errors="replace")
        phrases = [line.strip() for line in content.splitlines() if line.strip()]

        if not phrases:
            await message.answer(
                "Файл пустой. Добавьте фразы (одна на строку).",
                reply_markup=cancel_kb(cancel_cb),
            )
            return

        if len(phrases) > MAX_KEYWORD_PHRASES:
            await message.answer(
                f"Максимум {MAX_KEYWORD_PHRASES} фраз. Сейчас: {len(phrases)}.",
                reply_markup=cancel_kb(cancel_cb),
            )
            return

        category_id = data.get("category_id") or data.get("kw_cat_id")
        if not category_id:
            await message.answer("Категория не найдена. Начните заново.", reply_markup=menu_kb())
            return

        cat_id = int(category_id)
        project_id = int(data.get("project_id") or data.get("kw_project_id") or 0)

        if cfg.upload_enriches:
            # Toolbox mode: cluster + enrich pipeline
            if not ai_orchestrator or not dataforseo_client:
                log.error(f"{log_prefix}.upload_missing_deps", category_id=cat_id)
                await message.answer("Внутренняя ошибка. Попробуйте позже.")
                return
            raw = [{"phrase": p, "volume": 0, "cpc": 0.0, "ai_suggested": False} for p in phrases]
            await _run_upload_enrich_pipeline(
                message,
                state,
                user,
                db,
                cfg=cfg,
                cat_id=cat_id,
                project_id=project_id,
                raw_phrases=raw,
                ai_orchestrator=ai_orchestrator,
                dataforseo_client=dataforseo_client,
            )
        else:
            # Pipeline mode: save as flat keywords
            keywords = [{"phrase": p, "volume": 0, "cpc": 0.0} for p in phrases]
            cat_svc = category_service_factory(db)
            saved = await cat_svc.update_keywords(cat_id, user.id, keywords)
            if not saved:
                log.error(f"{log_prefix}.keywords_upload_failed", category_id=cat_id, user_id=user.id)
                await message.answer("Не удалось сохранить фразы. Попробуйте снова.")
                return

            log.info(
                f"{log_prefix}.keywords_uploaded",
                user_id=user.id,
                category_id=cat_id,
                phrase_count=len(phrases),
            )

            await message.answer(f"Загружено {len(phrases)} фраз.")
            await cfg.on_done_msg(message, state, user, db, redis)

    router.message.register(
        wizard_upload_file,
        cfg.state_products,
        F.document,
    )

    # -----------------------------------------------------------------------
    # Geography text input
    # -----------------------------------------------------------------------

    async def wizard_geo_input(
        message: Message,
        state: FSMContext,
        user: User,
        db: SupabaseClient,
        redis: RedisClient,
        ai_orchestrator: AIOrchestrator,
        dataforseo_client: DataForSEOClient,
        project_service_factory: ProjectServiceFactory,
    ) -> None:
        """Geography text input (2-200 chars) -> run generation immediately."""
        text = (message.text or "").strip()
        data = await state.get_data()
        if len(text) < 2 or len(text) > 200:
            await message.answer(
                "Введите от 2 до 200 символов.",
                reply_markup=cfg.cancel_nav_kb_fn(data),
            )
            return

        await state.update_data(kw_geography=text)
        await state.set_state(cfg.state_generating)

        # Save city to project for future auto-fill
        project_id = data.get("project_id") or data.get("kw_project_id")
        if project_id:
            from db.models import ProjectUpdate

            proj_svc = project_service_factory(db)
            with contextlib.suppress(Exception):
                await proj_svc.update_project(int(project_id), user.id, ProjectUpdate(company_city=text))

        products = data.get("kw_products", "")
        category_id = data.get("category_id") or data.get("kw_cat_id")
        if not category_id or not project_id:
            await message.answer("Данные не найдены. Начните заново.", reply_markup=menu_kb())
            await state.clear()
            return

        progress_msg = await message.answer(
            _kw_progress_text(_KW_STEPS, 0),
        )

        await run_keyword_generation(
            progress_msg=progress_msg,
            state=state,
            user=user,
            db=db,
            redis=redis,
            cfg=cfg,
            category_id=int(category_id),
            project_id=int(project_id),
            products=products,
            geography=text,
            ai_orchestrator=ai_orchestrator,
            dataforseo_client=dataforseo_client,
        )

    router.message.register(
        wizard_geo_input,
        cfg.state_geo,
        F.text,
    )

    # -----------------------------------------------------------------------
    # City select (pipeline auto_mode only)
    # -----------------------------------------------------------------------

    if cfg.auto_mode:
        async def wizard_city_select(
            callback: CallbackQuery,
            state: FSMContext,
            user: User,
            db: SupabaseClient,
            redis: RedisClient,
            project_service_factory: ProjectServiceFactory,
            ai_orchestrator: AIOrchestrator,
            dataforseo_client: DataForSEOClient,
        ) -> None:
            """Quick city selection -> run generation immediately (UX_PIPELINE SS4a)."""
            msg = safe_message(callback)
            if not msg:
                await callback.answer()
                return

            if not callback.data:
                await callback.answer()
                return
            city = callback.data.split(":")[-1]
            data = await state.get_data()
            products = data.get("kw_products", "")
            project_id = data.get("project_id") or data.get("kw_project_id")
            category_id = data.get("category_id") or data.get("kw_cat_id")
            kw_target = int(data.get("kw_target") or 100)

            # Save city to project for future use
            if project_id:
                from db.models import ProjectUpdate

                proj_svc = project_service_factory(db)
                with contextlib.suppress(Exception):
                    await proj_svc.update_project(int(project_id), user.id, ProjectUpdate(company_city=city))

            if not category_id or not project_id:
                await callback.answer("Данные не найдены. Начните заново.", show_alert=True)
                return

            await state.set_state(cfg.state_generating)
            await state.update_data(kw_geography=city)

            await safe_edit_text(msg, _kw_progress_text(_KW_STEPS, 0))
            await callback.answer()

            await run_keyword_generation(
                progress_msg=msg,
                state=state,
                user=user,
                db=db,
                redis=redis,
                cfg=cfg,
                category_id=int(category_id),
                project_id=int(project_id),
                products=products,
                geography=city,
                ai_orchestrator=ai_orchestrator,
                dataforseo_client=dataforseo_client,
                target_phrases=kw_target,
            )

        router.callback_query.register(
            wizard_city_select,
            cfg.state_geo,
            F.data.startswith(f"{prefix}:keywords:city:"),
        )

    # -----------------------------------------------------------------------
    # Cancel (from geo state only — state_generating has active coroutine, M4)
    # -----------------------------------------------------------------------

    prefix_escaped = re.escape(prefix)
    cancel_pattern = rf"^{prefix_escaped}.*(?:cancel|gen_cancel)$"

    async def wizard_cancel(
        callback: CallbackQuery,
        state: FSMContext,
        user: User,
        db: SupabaseClient,
        redis: RedisClient,
    ) -> None:
        """Cancel keyword wizard -- return to caller screen."""
        await cfg.on_done(callback, state, user, db, redis)
        await callback.answer()

    router.callback_query.register(
        wizard_cancel,
        StateFilter(cfg.state_geo),
        F.data.regexp(cancel_pattern),
    )

    # Build and return handlers dict
    handlers: dict[str, Any] = {
        "wizard_products_text": wizard_products_text,
        "wizard_upload_file": wizard_upload_file,
        "wizard_geo_input": wizard_geo_input,
        "wizard_cancel": wizard_cancel,
    }
    if cfg.auto_mode:
        handlers["wizard_city_select"] = wizard_city_select  # type: ignore[possibly-undefined]

    return handlers
