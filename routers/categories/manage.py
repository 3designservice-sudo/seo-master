"""Category CRUD: list, create (FSM), card, delete with E24/E42 cleanup."""

import html
import time

import structlog
from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from bot.assets import edit_screen
from bot.fsm_utils import ensure_no_active_fsm
from bot.helpers import get_owned_project, safe_callback_data, safe_edit_text, safe_message
from bot.service_factory import CategoryServiceFactory, TokenServiceFactory
from bot.texts import strings as S
from bot.texts.emoji import E
from bot.texts.screens import Screen
from db.client import SupabaseClient
from db.models import User
from db.repositories.projects import ProjectsRepository
from db.repositories.publications import PublicationsRepository
from keyboards.inline import (
    category_card_kb,
    category_created_kb,
    category_delete_confirm_kb,
    category_list_empty_kb,
    category_list_kb,
    menu_kb,
)
from services.scheduler import SchedulerService

log = structlog.get_logger()
router = Router()


# ---------------------------------------------------------------------------
# FSM definition (FSM_SPEC.md section 1)
# ---------------------------------------------------------------------------


class CategoryCreateFSM(StatesGroup):
    name = State()


# ---------------------------------------------------------------------------
# Category list
# ---------------------------------------------------------------------------


@router.callback_query(F.data.regexp(r"^project:\d+:categories$"))
async def show_category_list(
    callback: CallbackQuery,
    user: User,
    db: SupabaseClient,
    category_service_factory: CategoryServiceFactory,
) -> None:
    """Show category list for a project (UX_TOOLBOX.md section 7.1)."""
    msg = safe_message(callback)
    if not msg:
        await callback.answer()
        return

    cb_data = safe_callback_data(callback)
    project_id = int(cb_data.split(":")[1])
    project = await get_owned_project(db, project_id, user.id)
    if not project:
        await callback.answer(S.PROJECT_NOT_FOUND, show_alert=True)
        return

    cat_svc = category_service_factory(db)
    categories = await cat_svc.list_by_project(project_id, user.id)

    if not categories:
        safe_name = html.escape(project.name)
        empty_text = (
            Screen(E.HASHTAG, f"КАТЕГОРИИ \u2014 {safe_name}")
            .blank()
            .line(S.CATEGORY_EMPTY)
            .hint(S.CATEGORY_EMPTY_HINT)
            .build()
        )
        await edit_screen(
            msg,
            "empty_categories.png",
            empty_text,
            reply_markup=category_list_empty_kb(project_id),
        )
    else:
        safe_name = html.escape(project.name)
        text = (
            Screen(E.HASHTAG, f"КАТЕГОРИИ \u2014 {safe_name} ({len(categories)})")
            .hint(S.CATEGORIES_LIST_HINT)
            .build()
        )
        await edit_screen(
            msg,
            "empty_categories.png",
            text,
            reply_markup=category_list_kb(categories, project_id),
        )
    await callback.answer()


@router.callback_query(F.data.regexp(r"^page:categories:\d+:\d+$"))
async def paginate_categories(
    callback: CallbackQuery,
    user: User,
    db: SupabaseClient,
    category_service_factory: CategoryServiceFactory,
) -> None:
    """Handle category list pagination."""
    msg = safe_message(callback)
    if not msg:
        await callback.answer()
        return

    cb_data = safe_callback_data(callback)
    parts = cb_data.split(":")
    project_id = int(parts[2])
    page = int(parts[3])

    project = await get_owned_project(db, project_id, user.id)
    if not project:
        await callback.answer(S.PROJECT_NOT_FOUND, show_alert=True)
        return

    cat_svc = category_service_factory(db)
    categories = await cat_svc.list_by_project(project_id, user.id)
    if categories is None:
        await callback.answer(S.PROJECT_NOT_FOUND, show_alert=True)
        return

    safe_name = html.escape(project.name)
    text = (
        Screen(E.HASHTAG, f"КАТЕГОРИИ \u2014 {safe_name} ({len(categories)})")
        .hint(S.CATEGORIES_LIST_HINT)
        .build()
    )
    await edit_screen(
        msg,
        "empty_categories.png",
        text,
        reply_markup=category_list_kb(categories, project_id, page),
    )
    await callback.answer()


# ---------------------------------------------------------------------------
# CategoryCreateFSM
# ---------------------------------------------------------------------------


@router.callback_query(F.data.regexp(r"^category:\d+:create$"))
async def start_category_create(
    callback: CallbackQuery,
    state: FSMContext,
    user: User,
    db: SupabaseClient,
    category_service_factory: CategoryServiceFactory,
) -> None:
    """Start category creation flow."""
    msg = safe_message(callback)
    if not msg:
        await callback.answer()
        return

    cb_data = safe_callback_data(callback)
    project_id = int(cb_data.split(":")[1])
    project = await get_owned_project(db, project_id, user.id)
    if not project:
        await callback.answer(S.PROJECT_NOT_FOUND, show_alert=True)
        return

    # U14: block category creation if project website_url is not set
    if not project.website_url:
        await safe_edit_text(
            msg,
            S.CATEGORY_NEEDS_WEBSITE,
            reply_markup=InlineKeyboardMarkup(
                inline_keyboard=[
                    [InlineKeyboardButton(text="К проекту", callback_data=f"project:{project_id}:edit")],
                ]
            ),
        )
        await callback.answer()
        return

    # H17: enforce category limit per project
    cat_svc = category_service_factory(db)
    has_room = await cat_svc.check_category_limit(project_id, user.id)
    if not has_room:
        from services.categories import MAX_CATEGORIES_PER_PROJECT

        await callback.answer(
            S.CATEGORY_LIMIT_REACHED.format(limit=MAX_CATEGORIES_PER_PROJECT),
            show_alert=True,
        )
        return

    interrupted = await ensure_no_active_fsm(state)
    if interrupted:
        await msg.answer(f"Предыдущий процесс ({interrupted}) прерван.")

    await state.set_state(CategoryCreateFSM.name)
    await state.update_data(last_update_time=time.time(), create_project_id=project_id)

    text = (
        Screen(E.FOLDER, S.CATEGORY_CREATE_TITLE)
        .progress(2, 5, "Категория")
        .blank()
        .line(S.CATEGORY_CREATE_PROMPT)
        .hint(S.CATEGORY_CREATE_EXAMPLE)
        .build()
    )
    await msg.answer(text)
    await callback.answer()


@router.message(CategoryCreateFSM.name, F.text)
async def process_category_name(
    message: Message,
    state: FSMContext,
    user: User,
    db: SupabaseClient,
    category_service_factory: CategoryServiceFactory,
) -> None:
    """Process category name (2-100 chars)."""
    text = (message.text or "").strip()

    if text == "Отмена":
        await state.clear()
        await message.answer(S.CATEGORY_CREATE_CANCELLED, reply_markup=menu_kb())
        return

    if len(text) < 2 or len(text) > 100:
        await message.answer(S.CATEGORY_NAME_LENGTH)
        return

    data = await state.get_data()
    project_id = int(data["create_project_id"])
    await state.clear()

    cat_svc = category_service_factory(db)
    category = await cat_svc.create_category(project_id, user.id, text)

    if not category:
        await message.answer(S.PROJECT_NOT_FOUND, reply_markup=menu_kb())
        return

    safe_name = html.escape(category.name)
    await message.answer(
        S.CATEGORY_CREATED.format(name=safe_name),
        reply_markup=category_created_kb(category.id),
    )
    log.info("category_created", category_id=category.id, project_id=project_id, user_id=user.id)


# ---------------------------------------------------------------------------
# Category card
# ---------------------------------------------------------------------------


@router.callback_query(F.data.regexp(r"^category:\d+:card$"))
async def show_category_card(
    callback: CallbackQuery,
    user: User,
    db: SupabaseClient,
    category_service_factory: CategoryServiceFactory,
) -> None:
    """Show category card (UX_TOOLBOX.md section 8)."""
    msg = safe_message(callback)
    if not msg:
        await callback.answer()
        return

    cb_data = safe_callback_data(callback)
    category_id = int(cb_data.split(":")[1])
    cat_svc = category_service_factory(db)
    category = await cat_svc.get_owned_category(category_id, user.id)
    if not category:
        await callback.answer(S.CATEGORY_NOT_FOUND, show_alert=True)
        return

    # Build card text via Screen builder with checklist
    safe_name = html.escape(category.name)
    s = Screen(E.FOLDER, safe_name)
    s.blank()

    # Keyword clusters
    keyword_count = len(category.keywords)
    if keyword_count > 0:
        cluster_count = sum(1 for k in category.keywords if k.get("cluster_name"))
        total_phrases = sum(len(k.get("phrases", [])) for k in category.keywords)
        if cluster_count > 0:
            s.check("Ключевики", ok=True, detail=f"{cluster_count} групп ({total_phrases} фраз)")
        else:
            s.check("Ключевики", ok=True, detail=f"{keyword_count} фраз")
    else:
        s.check("Ключевики", ok=False, detail="не заданы")

    # Description
    s.check("Описание", ok=bool(category.description), detail="" if category.description else "не задано")

    # Prices
    if category.prices:
        price_lines = [ln for ln in category.prices.splitlines() if ln.strip()]
        s.check("Цены", ok=True, detail=f"{len(price_lines)} позиций")
    else:
        s.check("Цены", ok=False, detail="не заданы")

    # Image settings (project fallback -> category)
    proj = await ProjectsRepository(db).get_by_id(category.project_id)
    eff_image_settings = (proj.image_settings if proj else None) or category.image_settings or {}
    img_count = eff_image_settings.get("count")
    if img_count is not None:
        s.check("Медиа", ok=True, detail=f"{img_count} изобр.")
    else:
        s.check("Медиа", ok=False, detail="нет файлов")

    # Publication count
    pub_repo = PublicationsRepository(db)
    pub_count = await pub_repo.get_count_by_category(category_id)
    s.field(E.ANALYTICS, "Публикаций", pub_count)
    s.hint(S.CATEGORY_HINT)

    text = s.build()
    await safe_edit_text(msg, text, reply_markup=category_card_kb(category_id, category.project_id))
    await callback.answer()


# ---------------------------------------------------------------------------
# Category delete
# ---------------------------------------------------------------------------


@router.callback_query(F.data.regexp(r"^category:\d+:delete$"))
async def confirm_category_delete(
    callback: CallbackQuery,
    user: User,
    db: SupabaseClient,
    category_service_factory: CategoryServiceFactory,
) -> None:
    """Show category delete confirmation."""
    msg = safe_message(callback)
    if not msg:
        await callback.answer()
        return

    cb_data = safe_callback_data(callback)
    category_id = int(cb_data.split(":")[1])
    cat_svc = category_service_factory(db)
    result = await cat_svc.get_delete_impact(category_id, user.id)
    if not result:
        await callback.answer(S.CATEGORY_NOT_FOUND, show_alert=True)
        return

    category, active_count = result

    safe_name = html.escape(category.name)
    s = Screen(E.WARNING, S.CATEGORY_DELETE_QUESTION.format(name=safe_name))
    s.blank()
    if active_count > 0:
        s.line(S.CATEGORY_DELETE_SCHEDULES.format(count=active_count))
    s.hint(S.CATEGORY_DELETE_WARNING)

    await safe_edit_text(
        msg,
        s.build(),
        reply_markup=category_delete_confirm_kb(category_id, category.project_id),
    )
    await callback.answer()


@router.callback_query(F.data.regexp(r"^category:\d+:delete:confirm$"))
async def execute_category_delete(
    callback: CallbackQuery,
    user: User,
    db: SupabaseClient,
    category_service_factory: CategoryServiceFactory,
    scheduler_service: SchedulerService,
    token_service_factory: TokenServiceFactory,
) -> None:
    """Delete category with E24 + E42 cleanup."""
    msg = safe_message(callback)
    if not msg:
        await callback.answer()
        return

    cb_data = safe_callback_data(callback)
    category_id = int(cb_data.split(":")[1])
    cat_svc = category_service_factory(db)
    token_svc = token_service_factory(db)

    deleted, category, remaining = await cat_svc.delete_category(category_id, user.id, scheduler_service, token_svc)

    if deleted and category:
        safe_name = html.escape(category.name)
        kb = (
            category_list_kb(remaining, category.project_id)
            if remaining
            else category_list_empty_kb(category.project_id)
        )
        success_text = (
            Screen(E.CHECK, "КАТЕГОРИЯ УДАЛЕНА")
            .blank()
            .line(S.CATEGORY_DELETED.format(name=safe_name))
            .build()
        )
        await safe_edit_text(msg, success_text, reply_markup=kb)
    else:
        error_text = Screen(E.WARNING, "ОШИБКА").blank().line(S.CATEGORY_DELETE_ERROR).build()
        await safe_edit_text(msg, error_text, reply_markup=menu_kb())

    await callback.answer()
