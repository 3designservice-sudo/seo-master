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
from integrations.designservice import (
    DesignserviceAPIError,
    DesignserviceAuthError,
    DesignserviceClient,
)
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


async def _build_articles_text() -> str:
    """Articles dashboard: live stats from _bot_api.php + nearest planned.

    PR 2 — read-only view. Generation FSM ('Опубликовать сейчас') добавится в PR 3.
    """
    client = DesignserviceClient()
    screen = Screen(E.GEAR, "Статьи").blank()

    try:
        stats = await client.stats()
    except DesignserviceAuthError:
        screen = screen.line(
            "⚠ Ключ DESIGNSERVICE_BOT_API_KEY не настроен или отклонён сервером. "
            "Добавьте его в Railway → Variables и перезапустите сервис."
        )
        return screen.build()
    except DesignserviceAPIError as exc:
        screen = screen.line(f"⚠ _bot_api.php недоступен: {exc}")
        return screen.build()

    screen = (
        screen.line(f"Всего в roadmap: {stats.total}")
        .line(
            "По статусам: "
            + ", ".join(
                f"{s}={c}"
                for s, c in sorted(stats.by_status.items(), key=lambda kv: -kv[1])
            )
        )
    )
    if stats.overdue_planned:
        screen = screen.line(f"⚠ Просрочено (planned до сегодня): {stats.overdue_planned}")
    screen = screen.blank()

    try:
        today_resp = await client.get_planned(date="today", limit=10, sort="freq")
    except DesignserviceAPIError as exc:
        screen = screen.line(f"⚠ get_planned упал: {exc}")
        return screen.build()

    screen = screen.line(f"📅 На сегодня planned: {today_resp.count}")
    for art in today_resp.articles[:5]:
        h1 = art.h1[:80] + ("…" if len(art.h1) > 80 else "")
        freq = f" [{art.kw_total_freq}/мес]" if art.kw_total_freq else ""
        screen = screen.line(f"  • id={art.id}: {h1}{freq}")
    if today_resp.count > 5:
        screen = screen.line(f"  …ещё {today_resp.count - 5}")

    screen = screen.blank().hint(
        "PR 3 добавит кнопку «Опубликовать сейчас» — рендер HTML + публикация через _receiver.php."
    )
    return screen.build()


@router.callback_query(F.data == "designservice:articles")
async def designservice_articles(callback: CallbackQuery, user: User) -> None:
    """Статьи: дашборд roadmap. Только read-only в PR 2."""
    if not _is_admin(user):
        await callback.answer(S.ADMIN_ACCESS_DENIED, show_alert=True)
        return
    msg = safe_message(callback)
    if not msg:
        await callback.answer()
        return
    text = await _build_articles_text()
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
