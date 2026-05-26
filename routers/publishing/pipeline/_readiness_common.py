"""Common readiness sub-flow logic shared between article and social pipelines (S1b).

Extracts the keyword generation pipeline, description generation logic,
and readiness sub-flow handler registration factory that were duplicated
between readiness.py and social/readiness.py.
Differences are passed via ReadinessConfig and parameters.

Keyword wizard handlers (products/geo/qty/confirm/cancel/city/upload) are
now delegated to routers.shared.keyword_wizard -- only entry points
(menu, auto, configure, upload_start) remain here.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

import structlog
from aiogram import F, Router
from aiogram.filters import StateFilter
from aiogram.fsm.state import State
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, Message

from bot.helpers import safe_edit_text, safe_message
from db.models import User
from keyboards.inline import cancel_kb, menu_kb
from keyboards.pipeline import (
    pipeline_back_to_checklist_kb,
    pipeline_description_options_kb,
    pipeline_keywords_city_kb,
    pipeline_keywords_options_kb,
)
from routers.shared.keyword_wizard import (
    KeywordWizardConfig,
    register_keyword_wizard,
)
from routers.shared.keyword_wizard import (
    run_keyword_generation as _wizard_run_keyword_generation,
)
from services.ai.description import DescriptionService
from services.categories import CategoryService

if TYPE_CHECKING:
    from aiogram.fsm.context import FSMContext

    from bot.service_factory import CategoryServiceFactory, ProjectServiceFactory
    from cache.client import RedisClient
    from db.client import SupabaseClient
    from services.ai.orchestrator import AIOrchestrator
    from services.external.dataforseo import DataForSEOClient

log = structlog.get_logger()


# ---------------------------------------------------------------------------
# Backward-compatible run_keyword_generation wrapper
# ---------------------------------------------------------------------------


async def run_keyword_generation(
    progress_msg: Message,
    state: FSMContext,
    user: User,
    db: SupabaseClient,
    redis: RedisClient,
    *,
    category_id: int,
    project_id: int,
    products: str,
    geography: str,
    ai_orchestrator: AIOrchestrator,
    dataforseo_client: DataForSEOClient,
    log_prefix: str,
    readiness_state: State,
    back_kb_prefix: str = "pipeline:readiness",
    on_success: Callable[[Message, FSMContext, User, SupabaseClient, RedisClient], Awaitable[None]],
) -> None:
    """Backward-compatible wrapper around the unified keyword wizard's run_keyword_generation.

    Constructs a minimal KeywordWizardConfig from the old-style parameters and
    delegates to the shared implementation.
    """
    compat_cfg = KeywordWizardConfig(
        state_products=State(),  # unused by run_keyword_generation
        state_geo=State(),
        state_generating=State(),
        prefix=back_kb_prefix,
        log_prefix=log_prefix,
        cancel_cb_fn=lambda _data: "",
        on_done=lambda cb, st, u, d, r: (  # type: ignore[arg-type]
            on_success(cb.message, st, u, d, r)
            if cb.message and isinstance(cb.message, Message)
            else asyncio.sleep(0)
        ),
        on_done_msg=on_success,
        cancel_nav_kb_fn=lambda _data: InlineKeyboardMarkup(inline_keyboard=[]),
        error_state=readiness_state,
        error_kb_fn=lambda _data: pipeline_back_to_checklist_kb(prefix=back_kb_prefix),
    )

    await _wizard_run_keyword_generation(
        progress_msg=progress_msg,
        state=state,
        user=user,
        db=db,
        redis=redis,
        cfg=compat_cfg,
        category_id=category_id,
        project_id=project_id,
        products=products,
        geography=geography,
        ai_orchestrator=ai_orchestrator,
        dataforseo_client=dataforseo_client,
    )


# ---------------------------------------------------------------------------
# ReadinessConfig -- parameterizes differences between article/social
# ---------------------------------------------------------------------------


# Type alias for the show_check callback (called after callback_query sub-flows)
ShowCheckCb = Callable[
    [CallbackQuery, "FSMContext", User, "SupabaseClient", "RedisClient"],
    Awaitable[None],
]
# Type alias for the show_check_msg callback (called after message sub-flows)
ShowCheckMsgCb = Callable[
    [Message, "FSMContext", User, "SupabaseClient", "RedisClient"],
    Awaitable[None],
]


@dataclass(frozen=True)
class ReadinessConfig:
    """Configuration that parameterizes readiness sub-flow differences.

    Used by register_readiness_subflows() to create handlers with
    the correct FSM states, callback prefixes, and return functions.
    """

    fsm_class: Any
    """FSM StatesGroup: ArticlePipelineFSM or SocialPipelineFSM.

    Typed as Any because both FSM classes are StatesGroup subclasses with
    matching readiness_* State attributes, but mypy can't express this
    structural subtyping for StatesGroup classes.
    """

    prefix: str
    """Callback prefix: 'pipeline:readiness' or 'pipeline:social:readiness'."""

    cancel_cb: str
    """Cancel callback_data: 'pipeline:article:cancel' or 'pipeline:social:cancel'."""

    log_prefix: str
    """Structlog prefix: 'pipeline.readiness' or 'pipeline.social.readiness'."""

    show_check: ShowCheckCb
    """Callback to show readiness checklist after callback_query sub-flows."""

    show_check_msg: ShowCheckMsgCb
    """Callback to show readiness checklist after message sub-flows."""

    description_hint: str = "статьи"
    """Hint text for description prompt: 'статьи' or 'посты'."""

    charge_suffix: str = "pipeline"
    """Suffix for token charge description: 'pipeline' or 'social pipeline'."""

    extra_back_states: list[Any] = field(default_factory=list)
    """Extra FSM states for the back-from-input handler (e.g. readiness_prices)."""


# ---------------------------------------------------------------------------
# Description AI generation (shared between article and social readiness)
# ---------------------------------------------------------------------------


async def generate_description_ai(
    callback: CallbackQuery,
    state: FSMContext,
    user: User,
    db: SupabaseClient,
    redis: RedisClient,
    ai_orchestrator: AIOrchestrator,
    *,
    log_prefix: str,
    on_success: Callable[[CallbackQuery, FSMContext, User, SupabaseClient, RedisClient], Awaitable[None]],
) -> None:
    """Generate category description via AI (free for user) and return to checklist."""
    msg = safe_message(callback)
    if not msg:
        await callback.answer()
        return

    data = await state.get_data()
    category_id = data.get("category_id")
    project_id = data.get("project_id")
    if not category_id or not project_id:
        await callback.answer("Категория не найдена.", show_alert=True)
        return

    # Answer callback immediately so the button stops "loading"
    await callback.answer()
    await safe_edit_text(msg, "Генерирую описание...")

    # Generate + save
    desc_svc = DescriptionService(orchestrator=ai_orchestrator, db=db)
    try:
        result = await desc_svc.generate(
            user_id=user.id,
            project_id=project_id,
            category_id=category_id,
        )
        generated = result.content if isinstance(result.content, str) else str(result.content)

        cat_svc = CategoryService(db=db)
        save_result = await cat_svc.update_description(category_id, user.id, generated)
        if not save_result:
            raise RuntimeError("description_save_failed")
    except Exception:
        log.exception(
            f"{log_prefix}.description_ai_failed",
            user_id=user.id,
            category_id=category_id,
        )
        await safe_edit_text(msg, "Ошибка генерации описания. Попробуйте позже.", reply_markup=menu_kb())
        return

    log.info(
        f"{log_prefix}.description_generated",
        user_id=user.id,
        category_id=category_id,
    )

    await on_success(callback, state, user, db, redis)


# ---------------------------------------------------------------------------
# Readiness sub-flow handler factory
# ---------------------------------------------------------------------------


def register_readiness_subflows(router: Router, cfg: ReadinessConfig) -> dict[str, Any]:  # noqa: C901
    """Register keyword/description/navigation handlers on the given router.

    Uses programmatic registration (router.callback_query / router.message)
    instead of decorators so the same logic can serve both article and social pipelines.

    Keyword wizard handlers (products text, upload file, geo, qty, confirm, cancel,
    city select) are delegated to register_keyword_wizard(). Only entry points
    (menu, auto, configure, upload_start) remain here.

    Returns a dict mapping handler names to handler functions (for test imports).
    """
    fsm = cfg.fsm_class
    prefix = cfg.prefix
    cancel_cb = cfg.cancel_cb
    log_prefix = cfg.log_prefix

    # -----------------------------------------------------------------------
    # Construct KeywordWizardConfig and register wizard handlers
    # -----------------------------------------------------------------------

    def _pipeline_cancel_nav_kb(_data: dict[str, Any]) -> InlineKeyboardMarkup:
        return pipeline_back_to_checklist_kb(prefix=prefix)

    def _pipeline_error_kb(_data: dict[str, Any]) -> InlineKeyboardMarkup:
        return pipeline_back_to_checklist_kb(prefix=prefix)

    kw_cfg = KeywordWizardConfig(
        state_products=fsm.readiness_keywords_products,
        state_geo=fsm.readiness_keywords_geo,
        state_generating=fsm.readiness_keywords_generating,
        prefix=prefix,
        log_prefix=log_prefix,
        cancel_cb_fn=lambda _data: cancel_cb,
        # Use lambdas to enable test patching of cfg.show_check/show_check_msg
        # (tests mutate _article_readiness_config attrs via object.__setattr__)
        on_done=lambda cb, st, u, d, r: cfg.show_check(cb, st, u, d, r),
        on_done_msg=lambda msg, st, u, d, r: cfg.show_check_msg(msg, st, u, d, r),
        cancel_nav_kb_fn=_pipeline_cancel_nav_kb,
        error_state=fsm.readiness_check,
        error_kb_fn=_pipeline_error_kb,
        auto_mode=True,
        upload_enriches=False,
    )

    wizard_handlers = register_keyword_wizard(router, kw_cfg)

    # -----------------------------------------------------------------------
    # Keywords entry points (stay here -- they set FSM state and kw_mode)
    # -----------------------------------------------------------------------

    async def readiness_keywords_menu(callback: CallbackQuery) -> None:
        """Show keyword generation options."""
        msg = safe_message(callback)
        if not msg:
            await callback.answer()
            return

        await safe_edit_text(
            msg,
            "Ключевые фразы\n\nВыберите способ добавления:",
            reply_markup=pipeline_keywords_options_kb(prefix=prefix),
        )
        await callback.answer()

    router.callback_query.register(
        readiness_keywords_menu,
        fsm.readiness_check,
        F.data == f"{prefix}:keywords",
    )

    async def readiness_keywords_auto(
        callback: CallbackQuery,
        state: FSMContext,
        user: User,
        db: SupabaseClient,
        redis: RedisClient,
        category_service_factory: CategoryServiceFactory,
        project_service_factory: ProjectServiceFactory,
        ai_orchestrator: AIOrchestrator,
        dataforseo_client: DataForSEOClient,
    ) -> None:
        """Auto keyword generation -- quick path (defaults from DB, no confirmation)."""
        msg = safe_message(callback)
        if not msg:
            await callback.answer()
            return

        # Parse target volume (100/200/300) from callback.data; default 100.
        target = 100
        if callback.data:
            tail = callback.data.rsplit(":", 1)[-1]
            if tail.isdigit():
                t = int(tail)
                if t in (100, 200, 300):
                    target = t

        data = await state.get_data()
        category_id = data.get("category_id")
        project_id = data.get("project_id")
        if not category_id or not project_id:
            await callback.answer("Категория не найдена.", show_alert=True)
            return

        cat_svc = category_service_factory(db)
        category = await cat_svc.get_owned_category(category_id, user.id)
        if not category:
            await callback.answer("Категория не найдена.", show_alert=True)
            return

        proj_svc = project_service_factory(db)
        project = await proj_svc.get_owned_project(project_id, user.id)

        products = category.name
        geography = project.company_city if project and project.company_city else None

        # UX_PIPELINE SS4a: if no company_city -- ask city first
        if not geography:
            await state.set_state(fsm.readiness_keywords_geo)
            await state.update_data(kw_products=products, kw_mode="auto", kw_target=target)

            await safe_edit_text(
                msg,
                "В каком городе ваш бизнес?\n<i>Для точных SEO-фраз</i>",
                reply_markup=pipeline_keywords_city_kb(prefix=prefix),
            )
            await callback.answer()
            return

        # City already set — run generation immediately
        await state.set_state(fsm.readiness_keywords_generating)
        await state.update_data(kw_products=products, kw_geography=geography, kw_target=target)

        await safe_edit_text(msg, "Получаю реальные фразы из DataForSEO...")
        await callback.answer()

        await _wizard_run_keyword_generation(
            progress_msg=msg,
            state=state,
            user=user,
            db=db,
            redis=redis,
            cfg=kw_cfg,
            category_id=int(category_id),
            project_id=int(project_id),
            products=products,
            geography=geography,
            ai_orchestrator=ai_orchestrator,
            dataforseo_client=dataforseo_client,
            target_phrases=target,
        )

    router.callback_query.register(
        readiness_keywords_auto,
        fsm.readiness_check,
        F.data.in_({
            f"{prefix}:keywords:auto",
            f"{prefix}:keywords:auto:100",
            f"{prefix}:keywords:auto:200",
            f"{prefix}:keywords:auto:300",
        }),
    )

    async def readiness_keywords_configure(
        callback: CallbackQuery,
        state: FSMContext,
    ) -> None:
        """Configure keyword generation -- full path (products -> geo -> qty -> confirm)."""
        msg = safe_message(callback)
        if not msg:
            await callback.answer()
            return

        await state.set_state(fsm.readiness_keywords_products)
        await state.update_data(kw_mode="configure")

        await safe_edit_text(
            msg,
            "Какие товары или услуги продвигаете?\n"
            "<i>Например: кухни на заказ, шкафы-купе, корпусная мебель</i>\n\n"
            "От 3 до 1000 символов.",
            reply_markup=pipeline_back_to_checklist_kb(prefix=prefix),
        )
        await callback.answer()

    router.callback_query.register(
        readiness_keywords_configure,
        fsm.readiness_check,
        F.data == f"{prefix}:keywords:configure",
    )

    async def readiness_keywords_upload_start(
        callback: CallbackQuery,
        state: FSMContext,
    ) -> None:
        """Start keyword upload sub-flow -- prompt for TXT file."""
        msg = safe_message(callback)
        if not msg:
            await callback.answer()
            return

        await safe_edit_text(
            msg,
            "Загрузите TXT-файл с ключевыми фразами.\n"
            "Одна фраза на строку, максимум 500 фраз, до 1 МБ.\n\n"
            "Или отправьте фразы текстом (каждая с новой строки).",
            reply_markup=pipeline_back_to_checklist_kb(prefix=prefix),
        )
        await state.set_state(fsm.readiness_keywords_products)
        await state.update_data(kw_mode="upload")
        await callback.answer()

    router.callback_query.register(
        readiness_keywords_upload_start,
        fsm.readiness_check,
        F.data == f"{prefix}:keywords:upload",
    )

    # -----------------------------------------------------------------------
    # Description sub-flow
    # -----------------------------------------------------------------------

    async def readiness_description_menu(callback: CallbackQuery) -> None:
        """Show description options."""
        msg = safe_message(callback)
        if not msg:
            await callback.answer()
            return

        await safe_edit_text(
            msg,
            "Описание компании/категории\n\nAI напишет точнее с контекстом о вашей компании.",
            reply_markup=pipeline_description_options_kb(prefix=prefix),
        )
        await callback.answer()

    router.callback_query.register(
        readiness_description_menu,
        fsm.readiness_check,
        F.data == f"{prefix}:description",
    )

    async def readiness_description_ai(
        callback: CallbackQuery,
        state: FSMContext,
        user: User,
        db: SupabaseClient,
        redis: RedisClient,
        ai_orchestrator: AIOrchestrator,
    ) -> None:
        """Generate category description via AI (delegates to generate_description_ai)."""
        await generate_description_ai(
            callback=callback,
            state=state,
            user=user,
            db=db,
            redis=redis,
            ai_orchestrator=ai_orchestrator,
            log_prefix=log_prefix,
            on_success=cfg.show_check,
        )

    router.callback_query.register(
        readiness_description_ai,
        fsm.readiness_check,
        F.data == f"{prefix}:description:ai",
    )

    async def readiness_description_manual_start(
        callback: CallbackQuery,
        state: FSMContext,
    ) -> None:
        """Start manual description input."""
        msg = safe_message(callback)
        if not msg:
            await callback.answer()
            return

        await safe_edit_text(
            msg,
            f"Введите описание компании/категории (10-2000 символов).\n\n"
            f"Чем подробнее -- тем точнее будут {cfg.description_hint}.",
            reply_markup=pipeline_back_to_checklist_kb(prefix=prefix),
        )
        await state.set_state(fsm.readiness_description)
        await callback.answer()

    router.callback_query.register(
        readiness_description_manual_start,
        fsm.readiness_check,
        F.data == f"{prefix}:description:manual",
    )

    async def readiness_description_manual_input(
        message: Message,
        state: FSMContext,
        user: User,
        db: SupabaseClient,
        redis: RedisClient,
        category_service_factory: CategoryServiceFactory,
    ) -> None:
        """Save manually entered description."""
        text = (message.text or "").strip()
        if len(text) < 10 or len(text) > 2000:
            await message.answer(
                "Описание: от 10 до 2000 символов.",
                reply_markup=cancel_kb(cancel_cb),
            )
            return

        data = await state.get_data()
        category_id = data.get("category_id")
        if not category_id:
            await message.answer("Категория не найдена. Начните заново.", reply_markup=menu_kb())
            return

        cat_svc = category_service_factory(db)
        saved = await cat_svc.update_description(category_id, user.id, text)
        if not saved:
            log.error(f"{log_prefix}.description_save_failed", category_id=category_id, user_id=user.id)
            await message.answer("Не удалось сохранить описание. Попробуйте снова.")
            return

        log.info(
            f"{log_prefix}.description_manual",
            user_id=user.id,
            category_id=category_id,
        )

        await message.answer("Описание сохранено.")
        await cfg.show_check_msg(message, state, user, db, redis)

    router.message.register(
        readiness_description_manual_input,
        fsm.readiness_description,
        F.text,
    )

    # -----------------------------------------------------------------------
    # Navigation: back to checklist, done
    # -----------------------------------------------------------------------

    # Collect all states that need "back to checklist" from text-input sub-flows
    back_input_states = [
        fsm.readiness_keywords_products,
        fsm.readiness_keywords_geo,
        fsm.readiness_description,
        *cfg.extra_back_states,
    ]

    async def readiness_back_from_input(
        callback: CallbackQuery,
        state: FSMContext,
        user: User,
        db: SupabaseClient,
        redis: RedisClient,
    ) -> None:
        """Return to readiness checklist from text-input sub-flows (M5)."""
        await cfg.show_check(callback, state, user, db, redis)
        await callback.answer()

    router.callback_query.register(
        readiness_back_from_input,
        StateFilter(*back_input_states),
        F.data == f"{prefix}:back",
    )

    async def readiness_back(
        callback: CallbackQuery,
        state: FSMContext,
        user: User,
        db: SupabaseClient,
        redis: RedisClient,
    ) -> None:
        """Return to readiness checklist from any sub-flow options screen."""
        await cfg.show_check(callback, state, user, db, redis)
        await callback.answer()

    router.callback_query.register(
        readiness_back,
        fsm.readiness_check,
        F.data == f"{prefix}:back",
    )

    # Note: readiness_done is NOT registered here because it differs between
    # article and social pipelines (article checks image_count, social passes
    # pipeline_type="social"). Each pipeline registers its own done handler.

    # -----------------------------------------------------------------------
    # Backward-compatible _handle_configure_products (old 2-arg signature)
    # -----------------------------------------------------------------------

    async def _handle_configure_products(message: Message, state: FSMContext) -> None:
        """Validate products input for configure keyword path (3-1000 chars).

        Backward-compatible thin wrapper -- old tests call this with (message, state).
        """
        text = (message.text or "").strip()
        if len(text) < 3 or len(text) > 1000:
            await message.answer(
                "Введите от 3 до 1000 символов.",
                reply_markup=pipeline_back_to_checklist_kb(prefix=prefix),
            )
            return

        await state.set_state(fsm.readiness_keywords_geo)
        await state.update_data(kw_products=text)

        await message.answer(
            "Укажите географию продвижения:\n<i>Например: Москва, Россия, СНГ</i>\n\nОт 2 до 200 символов.",
            reply_markup=pipeline_back_to_checklist_kb(prefix=prefix),
        )

    # Return handler functions for test imports (include wizard handlers)
    result: dict[str, Any] = {
        "readiness_keywords_menu": readiness_keywords_menu,
        "readiness_keywords_auto": readiness_keywords_auto,
        "readiness_keywords_configure": readiness_keywords_configure,
        "readiness_keywords_upload_start": readiness_keywords_upload_start,
        "_handle_configure_products": _handle_configure_products,
        "readiness_description_menu": readiness_description_menu,
        "readiness_description_ai": readiness_description_ai,
        "readiness_description_manual_start": readiness_description_manual_start,
        "readiness_description_manual_input": readiness_description_manual_input,
        "readiness_back_from_input": readiness_back_from_input,
        "readiness_back": readiness_back,
    }
    # Include wizard handlers for backward-compatible test imports
    for old_name, new_name in [
        ("readiness_keywords_upload_file", "wizard_upload_file"),
        ("readiness_keywords_text_input", "wizard_products_text"),
        ("readiness_keywords_city_select", "wizard_city_select"),
        ("readiness_keywords_geo_input", "wizard_geo_input"),
        ("readiness_keywords_cancel", "wizard_cancel"),
    ]:
        if new_name in wizard_handlers:
            result[old_name] = wizard_handlers[new_name]
    return result
