"""Inline keyboards for the Designservice admin section (PR 1 skeleton)."""

from __future__ import annotations

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup


def designservice_root_kb() -> InlineKeyboardMarkup:
    """Корневой экран Designservice — три раздела + назад."""
    rows = [
        [InlineKeyboardButton(text="📝 Статьи", callback_data="designservice:articles")],
        [InlineKeyboardButton(text="⚙️ Администрирование", callback_data="designservice:admin")],
        [InlineKeyboardButton(text="📊 Аналитика", callback_data="designservice:analytics")],
        [InlineKeyboardButton(text="К панели", callback_data="admin:panel")],
    ]
    return InlineKeyboardMarkup(inline_keyboard=rows)


def designservice_back_to_root_kb() -> InlineKeyboardMarkup:
    """Подменю-заглушка: одна кнопка «Назад»."""
    rows = [[InlineKeyboardButton(text="Назад", callback_data="designservice:entry")]]
    return InlineKeyboardMarkup(inline_keyboard=rows)
