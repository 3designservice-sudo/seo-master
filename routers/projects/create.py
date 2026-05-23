"""ProjectCreateFSM and ProjectEditFSM handlers."""

import html
import time
from collections.abc import Sequence

import structlog
from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message

from bot.fsm_utils import ensure_no_active_fsm
from bot.helpers import safe_callback_data, safe_edit_text, safe_message
from bot.service_factory import ProjectServiceFactory
from bot.texts import strings as S
from bot.texts.emoji import E
from bot.texts.screens import Screen
from bot.validators import URL_RE
from db.client import SupabaseClient
from db.models import Project, ProjectCreate, ProjectUpdate, User
from keyboards.inline import cancel_kb, menu_kb, project_created_kb, project_edit_kb
from services.projects import MAX_PROJECTS_PER_USER

log = structlog.get_logger()
router = Router()


def _project_completed(project: Project) -> dict[str, bool]:
    """Build ``completed`` dict for project_edit_kb checkmarks."""
    return {
        "name": bool(project.name),
        "company_name": bool(project.company_name),
        "specialization": bool(project.specialization),
        "description": bool(project.description),
        "advantages": bool(project.advantages),
        "experience": bool(project.experience),
        "website_url": bool(project.website_url),
        "company_city": bool(project.company_city),
        "company_phone": bool(project.company_phone),
        "company_email": bool(project.company_email),
        "company_address": bool(project.company_address),
    }


# ---------------------------------------------------------------------------
# FSM definitions (FSM_SPEC.md section 1)
# ---------------------------------------------------------------------------


class ProjectCreateFSM(StatesGroup):
    name = State()


class ProjectEditFSM(StatesGroup):
    field_value = State()


# ---------------------------------------------------------------------------
# Field metadata for edit
# ---------------------------------------------------------------------------


def _truncate(text: str, max_len: int) -> str:
    """Truncate text with ellipsis for display."""
    return text[:max_len] + "…" if len(text) > max_len else text


_FIELD_LABELS: dict[str, str] = S.FIELD_LABELS

_FIELD_LIMITS: dict[str, tuple[int, int]] = {
    "name": (2, 100),
    "company_name": (2, 255),
    "specialization": (2, 500),
    "website_url": (0, 500),
    "description": (10, 2000),
    "advantages": (5, 1000),
    "experience": (2, 500),
    "company_city": (2, 100),
    "company_address": (2, 255),
    "company_phone": (5, 30),
    "company_email": (5, 100),
}

# Display label capitalized for edit prompt header
_FIELD_DISPLAY: dict[str, str] = S.FIELD_DISPLAY


def _build_field_edit_prompt(field: str, current_value: str) -> str:
    """Build structured edit prompt with header, current value, and hint."""
    display_label = _FIELD_DISPLAY.get(field, field)
    s = Screen(E.PEN, S.PROJECT_EDIT_TITLE)
    s.blank()
    s.line(f"Поле: <b>{display_label}</b>")
    s.blank()
    if current_value:
        display_value = html.escape(_truncate(current_value, 100))
        s.line(f"{S.PROJECT_EDIT_CURRENT}")
        s.line(f"<i>{display_value}</i>")
    else:
        s.line(f"{S.PROJECT_EDIT_CURRENT} <i>{S.PROJECT_EDIT_EMPTY}</i>")
    s.hint(S.PROJECT_EDIT_PROMPT)
    return s.build()


# ---------------------------------------------------------------------------
# ProjectCreateFSM
# ---------------------------------------------------------------------------


@router.callback_query(F.data == "project:create")
async def start_create(
    callback: CallbackQuery,
    state: FSMContext,
    user: User,
    db: SupabaseClient,
    project_service_factory: ProjectServiceFactory,
) -> None:
    """Start project creation flow."""
    msg = safe_message(callback)
    if not msg:
        await callback.answer()
        return

    # H17: enforce project limit per user
    proj_svc = project_service_factory(db)
    under_limit = await proj_svc.check_project_limit(user.id)
    if not under_limit:
        await callback.answer(
            S.PROJECT_LIMIT_REACHED.format(limit=MAX_PROJECTS_PER_USER),
            show_alert=True,
        )
        return

    interrupted = await ensure_no_active_fsm(state)
    if interrupted:
        await msg.answer(f"Предыдущий процесс ({interrupted}) прерван.")

    await state.set_state(ProjectCreateFSM.name)
    await state.update_data(last_update_time=time.time())

    text = (
        Screen(E.FOLDER, S.PROJECT_CREATE_TITLE)
        .progress(1, 5, "Проект")
        .blank()
        .line(S.PROJECT_CREATE_QUESTION)
        .hint(S.PROJECT_CREATE_HINT)
        .build()
    )
    await msg.answer(text, reply_markup=cancel_kb("project:create:cancel"))
    await callback.answer()


@router.message(ProjectCreateFSM.name, F.text)
async def process_name(
    message: Message,
    state: FSMContext,
    user: User,
    db: SupabaseClient,
    project_service_factory: ProjectServiceFactory,
) -> None:
    """Single step: project name (2-100 chars) -> create project."""
    text = (message.text or "").strip()

    if text == "Отмена":
        await state.clear()
        await message.answer(S.PROJECT_CREATE_CANCELLED, reply_markup=menu_kb())
        return

    if len(text) < 2 or len(text) > 100:
        await message.answer(S.VALIDATION_NAME_LENGTH)
        return

    await state.clear()

    proj_svc = project_service_factory(db)
    project = await proj_svc.create_project(
        ProjectCreate(user_id=user.id, name=text, company_name=text)
    )

    if not project:
        await message.answer(S.PROJECT_LIMIT_REACHED.format(limit=MAX_PROJECTS_PER_USER), reply_markup=menu_kb())
        return

    safe_name = html.escape(project.name)
    await message.answer(
        S.PROJECT_CREATED.format(name=safe_name),
        reply_markup=project_created_kb(project.id),
    )

    log.info("project_created", project_id=project.id, user_id=user.id)


# ---------------------------------------------------------------------------
# ProjectEditFSM
# ---------------------------------------------------------------------------


@router.callback_query(F.data.regexp(r"^project:\d+:edit$"))
async def show_edit_screen(
    callback: CallbackQuery,
    user: User,
    db: SupabaseClient,
    project_service_factory: ProjectServiceFactory,
) -> None:
    """Show project edit screen with field buttons."""
    msg = safe_message(callback)
    if not msg:
        await callback.answer()
        return

    cb_data = safe_callback_data(callback)
    project_id = int(cb_data.split(":")[1])

    proj_svc = project_service_factory(db)
    project = await proj_svc.get_owned_project(project_id, user.id)
    if not project:
        await callback.answer(S.PROJECT_NOT_FOUND, show_alert=True)
        return

    text = _build_edit_text(project)
    await safe_edit_text(msg, text, reply_markup=project_edit_kb(project_id, _project_completed(project)))
    await callback.answer()


def _build_edit_text(project: Project) -> str:
    """Build edit screen text with current field values grouped in sections.

    Empty fields (None or empty string) are hidden.
    Long values are truncated to 60 chars.
    Sections with all empty fields are hidden entirely.
    """
    safe_name = html.escape(project.name)
    s = Screen(E.PEN, f"{safe_name} \u2014 РЕДАКТИРОВАНИЕ")

    # Section 1: Basic info
    basic_fields = [
        ("Название", project.name),
        ("Компания", project.company_name),
        ("Специализация", project.specialization),
    ]
    basic_lines = _render_section_fields(basic_fields)
    if basic_lines:
        s.section(E.FOLDER, S.SECTION_BASIC)
        for ln in basic_lines:
            s.line(ln)

    # Section 2: About company
    about_fields = [
        ("Описание", project.description),
        ("Преимущества", project.advantages),
        ("Опыт", project.experience),
    ]
    about_lines = _render_section_fields(about_fields)
    if about_lines:
        s.section(E.DOC, S.SECTION_ABOUT)
        for ln in about_lines:
            s.line(ln)

    # Section 3: Contacts
    contact_fields = [
        ("Сайт", project.website_url),
        ("Город", project.company_city),
        ("Адрес", project.company_address),
        ("Телефон", project.company_phone),
        ("Email", project.company_email),
    ]
    contact_lines = _render_section_fields(contact_fields)
    if contact_lines:
        s.section(E.LINK, S.SECTION_CONTACTS)
        for ln in contact_lines:
            s.line(ln)

    s.hint(S.PROJECT_EDIT_HINT)
    return s.build()


def _render_section_fields(fields: Sequence[tuple[str, str | None]]) -> list[str]:
    """Render non-empty fields, truncating long values to 60 chars."""
    result: list[str] = []
    for label, value in fields:
        if not value:
            continue
        display = _truncate(value, 60)
        result.append(f"{label}: {html.escape(display)}")
    return result


@router.callback_query(F.data.regexp(r"^project:\d+:edit:\w+$"))
async def start_field_edit(
    callback: CallbackQuery,
    state: FSMContext,
    user: User,
    db: SupabaseClient,
    project_service_factory: ProjectServiceFactory,
) -> None:
    """Start editing a specific field — enter ProjectEditFSM."""
    msg = safe_message(callback)
    if not msg:
        await callback.answer()
        return

    cb_data = safe_callback_data(callback)
    parts = cb_data.split(":")
    project_id = int(parts[1])
    field = parts[3]

    if field not in _FIELD_LABELS:
        await callback.answer(S.UNKNOWN_FIELD, show_alert=True)
        return

    proj_svc = project_service_factory(db)
    project = await proj_svc.get_owned_project(project_id, user.id)
    if not project:
        await callback.answer(S.PROJECT_NOT_FOUND, show_alert=True)
        return

    interrupted = await ensure_no_active_fsm(state)
    if interrupted:
        await msg.answer(f"Предыдущий процесс ({interrupted}) прерван.")

    await state.set_state(ProjectEditFSM.field_value)
    await state.update_data(
        last_update_time=time.time(),
        edit_project_id=project_id,
        edit_field=field,
    )

    current_raw = getattr(project, field, None) or ""
    await msg.answer(
        _build_field_edit_prompt(field, current_raw),
        reply_markup=cancel_kb("project:edit:cancel"),
    )
    await callback.answer()


@router.message(ProjectEditFSM.field_value, F.text)
async def process_field_value(
    message: Message,
    state: FSMContext,
    user: User,
    db: SupabaseClient,
    project_service_factory: ProjectServiceFactory,
) -> None:
    """Process new field value and save."""
    text = (message.text or "").strip()

    proj_svc = project_service_factory(db)

    if text == "Отмена":
        data = await state.get_data()
        project_id = data.get("edit_project_id")
        await state.clear()
        if project_id:
            project = await proj_svc.get_owned_project(int(project_id), user.id)
            if project:
                edit_text = _build_edit_text(project)
                await message.answer(
                    edit_text,
                    reply_markup=project_edit_kb(int(project_id), _project_completed(project)),
                )
                return
        await message.answer(S.PROJECT_EDIT_CANCELLED, reply_markup=menu_kb())
        return

    data = await state.get_data()
    project_id = int(data["edit_project_id"])
    field = str(data["edit_field"])

    # Validate length
    min_len, max_len = _FIELD_LIMITS.get(field, (1, 500))

    # URL validation for website_url
    if field == "website_url":
        if text.lower() in ("нет", "-", ""):
            text = ""
        elif not URL_RE.match(text):
            await message.answer(S.VALIDATION_URL_INVALID)
            return
        else:
            if not text.startswith("http"):
                text = f"https://{text}"
            if len(text) > max_len:
                await message.answer(S.VALIDATION_URL_TOO_LONG.format(max=max_len))
                return
    elif len(text) < min_len or len(text) > max_len:
        await message.answer(S.VALIDATION_FIELD_LENGTH.format(min=min_len, max=max_len))
        return

    await state.clear()

    # Ownership-verified update
    update_data = ProjectUpdate(**{field: text or None})  # type: ignore[arg-type]
    project = await proj_svc.update_project(project_id, user.id, update_data)

    if project:
        label = _FIELD_LABELS.get(field, field)
        edit_text = S.PROJECT_EDIT_UPDATED.format(label=label) + "\n\n" + _build_edit_text(project)
        await message.answer(edit_text, reply_markup=project_edit_kb(project_id, _project_completed(project)))
    else:
        error_text = Screen(E.WARNING, "ОШИБКА").blank().line(S.ERROR_UPDATE).build()
        await message.answer(error_text, reply_markup=menu_kb())

    log.info("project_field_updated", project_id=project_id, field=field, user_id=user.id)


# ---------------------------------------------------------------------------
# Cancel handlers (inline button)
# ---------------------------------------------------------------------------


@router.callback_query(F.data == "project:create:cancel")
async def cancel_create(
    callback: CallbackQuery,
    state: FSMContext,
) -> None:
    """Cancel project creation via inline button."""
    msg = safe_message(callback)
    if not msg:
        await callback.answer()
        return

    await state.clear()
    await safe_edit_text(msg, S.PROJECT_CREATE_CANCELLED, reply_markup=menu_kb())
    await callback.answer()


@router.callback_query(F.data == "project:edit:cancel")
async def cancel_edit(
    callback: CallbackQuery,
    state: FSMContext,
    user: User,
    db: SupabaseClient,
    project_service_factory: ProjectServiceFactory,
) -> None:
    """Cancel project field edit via inline button — return to edit screen."""
    msg = safe_message(callback)
    if not msg:
        await callback.answer()
        return

    data = await state.get_data()
    project_id = data.get("edit_project_id")
    await state.clear()

    if project_id:
        proj_svc = project_service_factory(db)
        project = await proj_svc.get_owned_project(int(project_id), user.id)
        if project:
            edit_text = _build_edit_text(project)
            await safe_edit_text(
                msg,
                edit_text,
                reply_markup=project_edit_kb(int(project_id), _project_completed(project)),
            )
            await callback.answer()
            return

    await safe_edit_text(msg, S.PROJECT_EDIT_CANCELLED, reply_markup=menu_kb())
    await callback.answer()
