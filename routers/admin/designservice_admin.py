"""Admin panel — Designservice.group: root-экран + заглушки трёх пунктов (PR 1).

В этом файле — только базовая инфраструктура раздела:
    designservice:entry      → root-экран (Статьи / Администрирование / Аналитика)
    designservice:articles   → заглушка (заполнится в PR 2)
    designservice:admin      → заглушка (PR 2-3)
    designservice:analytics  → заглушка (PR 4)

Подключение _bot_api.php (источник тем), _receiver.php (публикация HTML),
FSM генерации, генерации картинок через Gemini и анти-AI humanizer-pass —
в следующих PR. См. CLAUDE.md в проекте designservice / SEO_PLAN.md.
"""

from __future__ import annotations

import structlog
from aiogram import F, Router
from aiogram.types import CallbackQuery

from bot.helpers import safe_edit_text, safe_message
from bot.texts import designservice as TXT
from bot.texts import strings as S
from bot.texts.emoji import E
from bot.texts.screens import Screen
from db.models import User
from keyboards.designservice import (
    designservice_back_to_root_kb,
    designservice_root_kb,
)

log = structlog.get_logger()
router = Router()


def _is_admin(user: User) -> bool:
    return user.role == "admin"


# ---------------------------------------------------------------------------
# designservice:entry — root
# ---------------------------------------------------------------------------


def _build_root_text() -> str:
    return (
        Screen(E.LEAF, TXT.DESIGNSERVICE_ROOT_TITLE)
        .blank()
        .line(TXT.DESIGNSERVICE_ROOT_SUBTITLE)
        .blank()
        .hint(TXT.DESIGNSERVICE_ROOT_HINT)
        .build()
    )


@router.callback_query(F.data == "designservice:entry")
async def designservice_entry_root(callback: CallbackQuery, user: User) -> None:
    """Корневой экран Designservice — 3 кнопки."""
    if not _is_admin(user):
        await callback.answer(S.ADMIN_ACCESS_DENIED, show_alert=True)
        return
    msg = safe_message(callback)
    if not msg:
        await callback.answer()
        return
    await safe_edit_text(msg, _build_root_text(), reply_markup=designservice_root_kb())
    await callback.answer()


# ---------------------------------------------------------------------------
# Заглушки для трёх пунктов (PR 1)
# ---------------------------------------------------------------------------


def _build_stub_text(title: str, body: str) -> str:
    return Screen(E.GEAR, title).blank().line(body).build()


@router.callback_query(F.data == "designservice:articles")
async def designservice_articles_stub(callback: CallbackQuery, user: User) -> None:
    if not _is_admin(user):
        await callback.answer(S.ADMIN_ACCESS_DENIED, show_alert=True)
        return
    msg = safe_message(callback)
    if not msg:
        await callback.answer()
        return
    text = _build_stub_text("Статьи", TXT.DESIGNSERVICE_STUB_ARTICLES)
    await safe_edit_text(msg, text, reply_markup=designservice_back_to_root_kb())
    await callback.answer()


@router.callback_query(F.data == "designservice:admin")
async def designservice_admin_stub(callback: CallbackQuery, user: User) -> None:
    if not _is_admin(user):
        await callback.answer(S.ADMIN_ACCESS_DENIED, show_alert=True)
        return
    msg = safe_message(callback)
    if not msg:
        await callback.answer()
        return
    text = _build_stub_text("Администрирование", TXT.DESIGNSERVICE_STUB_ADMIN)
    await safe_edit_text(msg, text, reply_markup=designservice_back_to_root_kb())
    await callback.answer()


@router.callback_query(F.data == "designservice:analytics")
async def designservice_analytics_stub(callback: CallbackQuery, user: User) -> None:
    if not _is_admin(user):
        await callback.answer(S.ADMIN_ACCESS_DENIED, show_alert=True)
        return
    msg = safe_message(callback)
    if not msg:
        await callback.answer()
        return
    text = _build_stub_text("Аналитика", TXT.DESIGNSERVICE_STUB_ANALYTICS)
    await safe_edit_text(msg, text, reply_markup=designservice_back_to_root_kb())
    await callback.answer()
