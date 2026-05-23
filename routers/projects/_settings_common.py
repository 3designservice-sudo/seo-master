"""Shared helpers and main-screen handlers for content settings.

Private module: imported by text_settings.py and image_settings.py.
"""

from typing import Any

import structlog
from aiogram import F, Router
from aiogram.types import CallbackQuery

from bot.helpers import safe_callback_data, safe_edit_text, safe_message
from bot.service_factory import ProjectServiceFactory
from bot.texts import strings as S
from bot.texts.emoji import E
from bot.texts.screens import Screen
from db.client import SupabaseClient
from db.credential_manager import CredentialManager
from db.models import Project, User
from db.repositories.connections import ConnectionsRepository
from keyboards.inline import (
    project_content_settings_kb,
    project_platform_card_kb,
)

log = structlog.get_logger()
router = Router()

_PLAT_ICONS: dict[str, str] = {
    "wordpress": E.WORDPRESS,
    "telegram": E.TELEGRAM,
    "vk": E.VK,
    "pinterest": E.PINTEREST,
}

_PLAT_NAMES = {k: v.upper() for k, v in S.PLATFORM_DISPLAY.items()}

# Regex for target: "d" or platform names
_PT_RE = r"(d|wordpress|telegram|vk|pinterest)"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _fmt(items: list[str], fb: str = "не выбран") -> str:
    return ", ".join(items) if items else fb


def _settings_text(ts: dict[str, Any], is_: dict[str, Any]) -> str:
    """Build settings display text from ts/is dicts."""
    na = "—"

    wc = ts.get("word_count")
    wc_line = f"{wc} слов" if wc else na
    styles_line = _fmt(ts.get("styles", []), na)
    html_line = ts.get("html_style") or na

    pf_line = is_.get("preview_format") or na
    af_line = _fmt(is_.get("article_formats", []), na)
    is_line = _fmt(is_.get("styles", []), na)
    cnt = is_.get("count")
    cnt_line = str(cnt) if cnt is not None else na

    return (
        f"{E.PEN} <b>Текст</b>\n"
        f"  Стиль: {styles_line}\n"
        f"  HTML: {html_line}\n"
        f"  Длина: {wc_line}\n"
        "\n"
        f"{E.IMAGE} <b>Изображения</b>\n"
        f"  Превью: {pf_line}\n"
        f"  Форматы: {af_line}\n"
        f"  Стиль: {is_line}\n"
        f"  Количество: {cnt_line}"
    )


def _main_screen_text(*, has_platforms: bool = False) -> str:
    desc = S.CONTENT_SETTINGS_DESC if has_platforms else S.CONTENT_SETTINGS_NO_PLATFORMS
    return (
        Screen(E.SLIDERS, S.CONTENT_SETTINGS_TITLE)
        .progress(3, 5, "Текст и фото")
        .blank()
        .line(desc)
        .hint(S.CONTENT_SETTINGS_HINT)
        .build()
    )


def _platform_card_text(
    pt: str, ts: dict[str, Any], is_: dict[str, Any],
) -> str:
    icon = _PLAT_ICONS.get(pt, "")
    name = _PLAT_NAMES.get(pt, pt.upper())
    body = _settings_text(ts, is_)
    return (
        Screen(icon, name)
        .blank()
        .line(body)
        .hint(S.CONTENT_PLATFORM_HINT)
        .build()
    )


async def _load_project(
    callback: CallbackQuery,
    pid: int,
    user: User,
    db: SupabaseClient,
    psf: ProjectServiceFactory,
) -> Project | None:
    proj_svc = psf(db)
    project = await proj_svc.get_owned_project(pid, user.id)
    if not project:
        await callback.answer(S.PROJECT_NOT_FOUND, show_alert=True)
    return project


async def _get_platforms(
    db: SupabaseClient, key: str, pid: int,
) -> list[str]:
    cm = CredentialManager(key)
    return await ConnectionsRepository(db, cm).get_platform_types_by_project(pid)


async def _load_settings(
    db: SupabaseClient,
    project: Project,
    target: str,
    psf: ProjectServiceFactory,
) -> tuple[dict[str, Any], dict[str, Any]]:
    if target == "d":
        ts = dict(project.text_settings) if project.text_settings else {}
        is_ = dict(project.image_settings) if project.image_settings else {}
        return ts, is_
    return await psf(db).resolve_effective_settings(project.id, target)


async def _save_ts(
    db: SupabaseClient, pid: int, uid: int, target: str,
    ts: dict[str, Any], psf: ProjectServiceFactory,
) -> None:
    svc = psf(db)
    if target == "d":
        await svc.update_text_settings(pid, uid, ts)
    else:
        await svc.upsert_platform_settings(pid, uid, target, text_settings=ts)


async def _save_is(
    db: SupabaseClient, pid: int, uid: int, target: str,
    is_: dict[str, Any], psf: ProjectServiceFactory,
) -> None:
    svc = psf(db)
    if target == "d":
        await svc.update_image_settings(pid, uid, is_)
    else:
        await svc.upsert_platform_settings(
            pid, uid, target, image_settings=is_,
        )


async def _load_ts(
    db: SupabaseClient, p: Project, t: str, psf: ProjectServiceFactory,
) -> dict[str, Any]:
    ts, _ = await _load_settings(db, p, t, psf)
    return ts


async def _load_is(
    db: SupabaseClient, p: Project, t: str, psf: ProjectServiceFactory,
) -> dict[str, Any]:
    _, is_ = await _load_settings(db, p, t, psf)
    return is_


# ---------------------------------------------------------------------------
# Shared render helper (DRY for show_settings, back_to_settings, d:card)
# ---------------------------------------------------------------------------


async def _render_main_screen(
    msg: Any,
    pid: int,
    db: SupabaseClient,
    psf: ProjectServiceFactory,
) -> None:
    """Render main content settings screen with platform list."""
    svc = psf(db)
    platforms = await _get_platforms(db, svc.encryption_key, pid)
    await safe_edit_text(
        msg, _main_screen_text(has_platforms=bool(platforms)),
        reply_markup=project_content_settings_kb(pid, platforms),
    )


# ---------------------------------------------------------------------------
# 1. Main settings screen
# ---------------------------------------------------------------------------


@router.callback_query(F.data.regexp(r"^project:\d+:content_settings$"))
async def show_settings(
    callback: CallbackQuery,
    user: User,
    db: SupabaseClient,
    project_service_factory: ProjectServiceFactory,
) -> None:
    msg = safe_message(callback)
    if not msg:
        await callback.answer()
        return
    cb_data = safe_callback_data(callback)
    pid = int(cb_data.split(":")[1])
    project = await _load_project(
        callback, pid, user, db, project_service_factory,
    )
    if not project:
        return
    await _render_main_screen(msg, pid, db, project_service_factory)
    await callback.answer()


@router.callback_query(F.data.regexp(r"^psettings:\d+:back$"))
async def back_to_settings(
    callback: CallbackQuery,
    user: User,
    db: SupabaseClient,
    project_service_factory: ProjectServiceFactory,
) -> None:
    msg = safe_message(callback)
    if not msg:
        await callback.answer()
        return
    cb_data = safe_callback_data(callback)
    pid = int(cb_data.split(":")[1])
    project = await _load_project(
        callback, pid, user, db, project_service_factory,
    )
    if not project:
        return
    await _render_main_screen(msg, pid, db, project_service_factory)
    await callback.answer()


# ---------------------------------------------------------------------------
# 2. Platform / Default card
# ---------------------------------------------------------------------------


@router.callback_query(F.data.regexp(rf"^psettings:\d+:{_PT_RE}:card$"))
async def show_platform_card(
    callback: CallbackQuery,
    user: User,
    db: SupabaseClient,
    project_service_factory: ProjectServiceFactory,
) -> None:
    msg = safe_message(callback)
    if not msg:
        await callback.answer()
        return
    cb_data = safe_callback_data(callback)
    parts = cb_data.split(":")
    pid, target = int(parts[1]), parts[2]
    project = await _load_project(
        callback, pid, user, db, project_service_factory,
    )
    if not project:
        return
    # target="d" → redirect to main settings screen
    if target == "d":
        await _render_main_screen(msg, pid, db, project_service_factory)
        await callback.answer()
        return
    ts, is_ = await _load_settings(
        db, project, target, project_service_factory,
    )
    await safe_edit_text(
        msg, _platform_card_text(target, ts, is_),
        reply_markup=project_platform_card_kb(pid, target),
    )
    await callback.answer()


# ---------------------------------------------------------------------------
# 3. Reset platform override
# ---------------------------------------------------------------------------


@router.callback_query(F.data.regexp(rf"^psettings:\d+:{_PT_RE}:reset$"))
async def reset_platform(
    callback: CallbackQuery,
    user: User,
    db: SupabaseClient,
    project_service_factory: ProjectServiceFactory,
) -> None:
    msg = safe_message(callback)
    if not msg:
        await callback.answer()
        return
    cb_data = safe_callback_data(callback)
    parts = cb_data.split(":")
    pid, target = int(parts[1]), parts[2]
    if target == "d":
        await callback.answer()
        return
    svc = project_service_factory(db)
    project = await svc.get_owned_project(pid, user.id)
    if not project:
        await callback.answer(S.PROJECT_NOT_FOUND, show_alert=True)
        return
    await svc.delete_platform_settings(pid, user.id, target)
    log.info("platform_settings_reset", project_id=pid, platform=target)
    await safe_edit_text(
        msg, _platform_card_text(target, {}, {}),
        reply_markup=project_platform_card_kb(pid, target),
    )
    await callback.answer(S.CONTENT_RESET_DONE)
