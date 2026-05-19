"""Profile, notifications, referral, tariffs, payments, scheduler, and admin keyboards."""

from typing import Any

from aiogram.enums import ButtonStyle
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from bot.texts.emoji import TOGGLE_ON
from bot.texts.legal import PRIVACY_POLICY_URL, TERMS_OF_SERVICE_URL
from keyboards.common import format_connection_display

__all__ = [
    "admin_panel_kb",
    "admin_portals_kb",
    "broadcast_audience_kb",
    "broadcast_confirm_kb",
    "delete_account_cancelled_kb",
    "delete_account_confirm_kb",
    "detect_active_preset",
    "notifications_kb",
    "payment_method_kb",
    "profile_kb",
    "referral_kb",
    "schedule_count_kb",
    "schedule_days_kb",
    "schedule_times_kb",
    "scheduler_cat_list_kb",
    "scheduler_config_kb",
    "scheduler_conn_list_kb",
    "scheduler_crosspost_kb",
    "scheduler_social_cat_list_kb",
    "scheduler_social_config_kb",
    "scheduler_social_conn_list_kb",
    "scheduler_type_kb",
    "tariffs_kb",
    "user_actions_kb",
    "yookassa_link_kb",
]


# ---------------------------------------------------------------------------
# Profile (UX_TOOLBOX section 14)
# ---------------------------------------------------------------------------


def profile_kb() -> InlineKeyboardMarkup:
    """Profile main screen keyboard."""
    rows = [
        [InlineKeyboardButton(text="Пополнить баланс", callback_data="nav:tokens", style=ButtonStyle.PRIMARY)],
        [InlineKeyboardButton(text="Уведомления", callback_data="profile:notifications")],
        [InlineKeyboardButton(text="Реферальная программа", callback_data="profile:referral")],
        [
            InlineKeyboardButton(text="Политика", url=PRIVACY_POLICY_URL),
            InlineKeyboardButton(text="Оферта", url=TERMS_OF_SERVICE_URL),
        ],
        [InlineKeyboardButton(text="Главное меню", callback_data="nav:dashboard")],
    ]
    return InlineKeyboardMarkup(inline_keyboard=rows)


def notifications_kb(
    notify_publications: bool,
    notify_balance: bool,
    notify_news: bool,
) -> InlineKeyboardMarkup:
    """Notification toggle keyboard."""

    def _toggle(label: str, enabled: bool, key: str) -> InlineKeyboardButton:
        mark = "\u2713" if enabled else "\u2717"
        return InlineKeyboardButton(text=f"{mark} {label}", callback_data=f"profile:notify:{key}")

    rows = [
        [_toggle("Публикации", notify_publications, "publications")],
        [_toggle("Баланс", notify_balance, "balance")],
        [_toggle("Новости", notify_news, "news")],
        [InlineKeyboardButton(text="К профилю", callback_data="nav:profile")],
    ]
    return InlineKeyboardMarkup(inline_keyboard=rows)


def referral_kb() -> InlineKeyboardMarkup:
    """Referral program keyboard (link shown inline in message text)."""
    rows = [
        [InlineKeyboardButton(text="К профилю", callback_data="nav:profile")],
    ]
    return InlineKeyboardMarkup(inline_keyboard=rows)


def delete_account_confirm_kb() -> InlineKeyboardMarkup:
    """Confirmation dialog for account deletion (152-FZ compliance)."""
    rows = [
        [
            InlineKeyboardButton(
                text="Да, удалить аккаунт",
                callback_data="account:delete:confirm",
                style=ButtonStyle.DANGER,
            ),
        ],
        [InlineKeyboardButton(text="Отмена", callback_data="account:delete:cancel")],
    ]
    return InlineKeyboardMarkup(inline_keyboard=rows)


def delete_account_cancelled_kb() -> InlineKeyboardMarkup:
    """Keyboard shown after account deletion is cancelled."""
    rows = [
        [InlineKeyboardButton(text="К профилю", callback_data="nav:profile")],
    ]
    return InlineKeyboardMarkup(inline_keyboard=rows)


# ---------------------------------------------------------------------------
# Tariffs & Payments (UX_TOOLBOX section 15)
# ---------------------------------------------------------------------------


def tariffs_kb() -> InlineKeyboardMarkup:
    """Package selection keyboard. Profi is PRIMARY (best value)."""
    from services.payments.packages import PACKAGES

    rows: list[list[InlineKeyboardButton]] = []
    for name, pkg in PACKAGES.items():
        style = ButtonStyle.PRIMARY if name == "profi" else None
        btn = InlineKeyboardButton(
            text=pkg.label,
            callback_data=f"tariff:{name}:buy",
        )
        if style:
            btn.style = style
        rows.append([btn])
    rows.append([InlineKeyboardButton(text="Главное меню", callback_data="nav:dashboard")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def payment_method_kb(package_name: str) -> InlineKeyboardMarkup:
    """Payment method selection: Stars or YooKassa."""
    rows = [
        [InlineKeyboardButton(text="Telegram Stars", callback_data=f"tariff:{package_name}:stars")],
        [InlineKeyboardButton(text="ЮKassa (карта)", callback_data=f"tariff:{package_name}:yookassa")],
        [InlineKeyboardButton(text="Назад", callback_data="nav:tokens")],
    ]
    return InlineKeyboardMarkup(inline_keyboard=rows)


def yookassa_link_kb(url: str, package_name: str) -> InlineKeyboardMarkup:
    """YooKassa payment link + back button."""
    rows = [
        [InlineKeyboardButton(text="Перейти к оплате", url=url)],
        [InlineKeyboardButton(text="Назад", callback_data=f"tariff:{package_name}:buy")],
    ]
    return InlineKeyboardMarkup(inline_keyboard=rows)


# ---------------------------------------------------------------------------
# Scheduler (UX_TOOLBOX section 13)
# ---------------------------------------------------------------------------

_PRESETS: dict[str, tuple[str, list[str], list[str], int]] = {
    "1w": ("1 раз/нед", ["wed"], ["10:00"], 1),
    "3w": ("3 раза/нед", ["mon", "wed", "fri"], ["10:00"], 1),
    "daily": ("Каждый день", ["mon", "tue", "wed", "thu", "fri", "sat", "sun"], ["10:00"], 1),
}


def detect_active_preset(schedule_days: list[str], posts_per_day: int) -> str | None:
    """Match current schedule parameters to a preset key.

    Returns preset key ("1w", "3w", "daily") or "manual" if no preset matches.
    Returns None if schedule_days is empty or posts_per_day <= 0 (no schedule).
    """
    if not schedule_days or posts_per_day <= 0:
        return None

    day_set = set(schedule_days)
    for key, (_label, days, _times, ppd) in _PRESETS.items():
        if day_set == set(days) and posts_per_day == ppd:
            return key
    return "manual"


_DAY_LABELS: dict[str, str] = {
    "mon": "Пн",
    "tue": "Вт",
    "wed": "Ср",
    "thu": "Чт",
    "fri": "Пт",
    "sat": "Сб",
    "sun": "Вс",
}


def scheduler_type_kb(project_id: int) -> InlineKeyboardMarkup:
    """Scheduler type selection: articles or social posts."""
    pid = project_id
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="Статьи на сайт",
                    callback_data=f"project:{pid}:sched_articles",
                ),
                InlineKeyboardButton(
                    text="Посты в соцсети",
                    callback_data=f"project:{pid}:sched_social",
                ),
            ],
            [InlineKeyboardButton(text="К проекту", callback_data=f"project:{pid}:card")],
        ]
    )


def scheduler_cat_list_kb(categories: list[Any], project_id: int) -> InlineKeyboardMarkup:
    """Category list for scheduler entry."""
    rows: list[list[InlineKeyboardButton]] = []
    for cat in categories:
        rows.append(
            [
                InlineKeyboardButton(
                    text=cat.name,
                    callback_data=f"scheduler:{project_id}:cat:{cat.id}",
                )
            ]
        )
    rows.append([InlineKeyboardButton(text="К проекту", callback_data=f"project:{project_id}:card")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def scheduler_social_cat_list_kb(categories: list[Any], project_id: int) -> InlineKeyboardMarkup:
    """Category list for social scheduler entry."""
    rows: list[list[InlineKeyboardButton]] = []
    for cat in categories:
        rows.append(
            [
                InlineKeyboardButton(
                    text=cat.name,
                    callback_data=f"sched_social:{project_id}:cat:{cat.id}",
                )
            ]
        )
    rows.append([InlineKeyboardButton(text="К проекту", callback_data=f"project:{project_id}:card")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def scheduler_conn_list_kb(
    connections: list[Any],
    schedules: dict[int, Any],
    cat_id: int,
    project_id: int,
) -> InlineKeyboardMarkup:
    """Article (WordPress) connection list with schedule summaries."""
    rows: list[list[InlineKeyboardButton]] = []
    for conn in connections:
        sched = schedules.get(conn.id)
        display = format_connection_display(conn)
        if sched and sched.enabled:
            days_str = ", ".join(_DAY_LABELS.get(d) or d for d in sched.schedule_days)
            label = f"{display} ({days_str})"
        else:
            label = f"{display} (нет расписания)"
        rows.append(
            [
                InlineKeyboardButton(
                    text=label,
                    callback_data=f"scheduler:{cat_id}:conn:{conn.id}",
                )
            ]
        )
    rows.append(
        [
            InlineKeyboardButton(
                text="Назад",
                callback_data=f"project:{project_id}:sched_articles",
            )
        ]
    )
    return InlineKeyboardMarkup(inline_keyboard=rows)


def scheduler_config_kb(
    cat_id: int,
    conn_id: int,
    has_schedule: bool,
    schedule_days: list[str] | None = None,
    posts_per_day: int = 0,
) -> InlineKeyboardMarkup:
    """Schedule config: presets + manual + disable.

    Active preset is auto-detected from schedule_days/posts_per_day.
    If no schedule, "3w" gets PRIMARY as recommendation.
    """
    active_preset = detect_active_preset(schedule_days or [], posts_per_day)
    presets = [("3 раза/неделю", "3w"), ("1 раз/неделю", "1w"), ("Каждый день", "daily")]
    rows: list[list[InlineKeyboardButton]] = []
    for label, key in presets:
        if active_preset and active_preset == key:
            style = ButtonStyle.SUCCESS
            text = f"{TOGGLE_ON}{label}"
        elif not active_preset and key == "3w":
            style = ButtonStyle.PRIMARY
            text = label
        else:
            style = None
            text = label
        rows.append(
            [
                InlineKeyboardButton(
                    text=text,
                    callback_data=f"sched:{cat_id}:{conn_id}:preset:{key}",
                    style=style,
                )
            ]
        )
    manual_text = f"{TOGGLE_ON}Настроить вручную" if active_preset == "manual" else "Настроить вручную"
    manual_style = ButtonStyle.SUCCESS if active_preset == "manual" else None
    rows.append(
        [
            InlineKeyboardButton(
                text=manual_text,
                callback_data=f"sched:{cat_id}:{conn_id}:manual",
                style=manual_style,
            )
        ]
    )
    if has_schedule:
        rows.append(
            [
                InlineKeyboardButton(
                    text="Отключить расписание",
                    callback_data=f"sched:{cat_id}:{conn_id}:disable",
                    style=ButtonStyle.DANGER,
                )
            ]
        )
    rows.append([InlineKeyboardButton(text="Назад", callback_data=f"scheduler:{cat_id}:conn_list")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def schedule_days_kb(selected: set[str]) -> InlineKeyboardMarkup:
    """Day selection grid for manual schedule setup."""
    rows: list[list[InlineKeyboardButton]] = []
    row: list[InlineKeyboardButton] = []
    for key, label in _DAY_LABELS.items():
        mark = TOGGLE_ON if key in selected else ""
        row.append(InlineKeyboardButton(text=f"{mark}{label}", callback_data=f"sched:day:{key}"))
        if len(row) == 4:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    rows.append(
        [
            InlineKeyboardButton(text="Готово", callback_data="sched:days:done"),
            InlineKeyboardButton(text="Отмена", callback_data="sched:cancel"),
        ]
    )
    return InlineKeyboardMarkup(inline_keyboard=rows)


def schedule_count_kb() -> InlineKeyboardMarkup:
    """Posts per day selection (1-5)."""
    row = [InlineKeyboardButton(text=str(i), callback_data=f"sched:count:{i}") for i in range(1, 6)]
    cancel_row = [InlineKeyboardButton(text="Отмена", callback_data="sched:cancel")]
    return InlineKeyboardMarkup(inline_keyboard=[row, cancel_row])


_SOCIAL_TYPES = {"telegram", "vk", "pinterest"}


def scheduler_social_conn_list_kb(
    connections: list[Any],
    schedules: dict[int, Any],
    cat_id: int,
    project_id: int,
) -> InlineKeyboardMarkup:
    """Social connection list with cross-post count badges."""
    rows: list[list[InlineKeyboardButton]] = []
    for conn in connections:
        if conn.platform_type not in _SOCIAL_TYPES:
            continue
        sched = schedules.get(conn.id)
        display = format_connection_display(conn)
        if sched and sched.enabled:
            days_str = ", ".join(_DAY_LABELS.get(d) or d for d in sched.schedule_days)
            cross_count = len(sched.cross_post_connection_ids) if sched.cross_post_connection_ids else 0
            cross_badge = f" +{cross_count} кросс" if cross_count else ""
            label = f"{display} ({days_str}{cross_badge})"
        else:
            label = f"{display} (нет расписания)"
        rows.append(
            [
                InlineKeyboardButton(
                    text=label,
                    callback_data=f"sched_social:{cat_id}:conn:{conn.id}",
                )
            ]
        )
    rows.append(
        [
            InlineKeyboardButton(
                text="Назад",
                callback_data=f"project:{project_id}:sched_social",
            )
        ]
    )
    return InlineKeyboardMarkup(inline_keyboard=rows)


def scheduler_crosspost_kb(
    cat_id: int,
    conn_id: int,
    social_connections: list[Any],
    selected_ids: list[int],
) -> InlineKeyboardMarkup:
    """Cross-post toggle checkboxes for dependent platforms."""
    rows: list[list[InlineKeyboardButton]] = []
    for conn in social_connections:
        if conn.id == conn_id:
            continue  # skip lead connection
        mark = TOGGLE_ON if conn.id in selected_ids else ""
        display = format_connection_display(conn)
        rows.append(
            [
                InlineKeyboardButton(
                    text=f"{mark}{display}",
                    callback_data=f"sched_xp:{cat_id}:{conn_id}:{conn.id}:toggle",
                )
            ]
        )
    rows.append(
        [
            InlineKeyboardButton(
                text="Сохранить",
                callback_data=f"sched_xp:{cat_id}:{conn_id}:save",
                style=ButtonStyle.SUCCESS,
            ),
            InlineKeyboardButton(
                text="Отмена",
                callback_data=f"sched_social:{cat_id}:conn:{conn_id}",
            ),
        ]
    )
    return InlineKeyboardMarkup(inline_keyboard=rows)


def scheduler_social_config_kb(
    cat_id: int,
    conn_id: int,
    has_schedule: bool,
    has_other_social: bool = False,
    schedule_days: list[str] | None = None,
    posts_per_day: int = 0,
) -> InlineKeyboardMarkup:
    """Schedule config for social connections: presets + manual + cross-post + disable.

    Active preset is auto-detected from schedule_days/posts_per_day.
    If no schedule, "3w" gets PRIMARY as recommendation.
    """
    active_preset = detect_active_preset(schedule_days or [], posts_per_day)
    presets = [("3 раза/неделю", "3w"), ("1 раз/неделю", "1w"), ("Каждый день", "daily")]
    rows: list[list[InlineKeyboardButton]] = []
    for label, key in presets:
        if active_preset and active_preset == key:
            style = ButtonStyle.SUCCESS
            text = f"{TOGGLE_ON}{label}"
        elif not active_preset and key == "3w":
            style = ButtonStyle.PRIMARY
            text = label
        else:
            style = None
            text = label
        rows.append(
            [
                InlineKeyboardButton(
                    text=text,
                    callback_data=f"sched:{cat_id}:{conn_id}:preset:{key}",
                    style=style,
                )
            ]
        )
    manual_text = f"{TOGGLE_ON}Настроить вручную" if active_preset == "manual" else "Настроить вручную"
    manual_style = ButtonStyle.SUCCESS if active_preset == "manual" else None
    rows.append(
        [
            InlineKeyboardButton(
                text=manual_text,
                callback_data=f"sched:{cat_id}:{conn_id}:manual",
                style=manual_style,
            )
        ]
    )
    if has_schedule and has_other_social:
        rows.append(
            [
                InlineKeyboardButton(
                    text="Кросс-постинг",
                    callback_data=f"sched_xp:{cat_id}:{conn_id}:config",
                )
            ]
        )
    if has_schedule:
        rows.append(
            [
                InlineKeyboardButton(
                    text="Отключить расписание",
                    callback_data=f"sched:{cat_id}:{conn_id}:disable",
                    style=ButtonStyle.DANGER,
                )
            ]
        )
    rows.append([InlineKeyboardButton(text="Назад", callback_data=f"scheduler:{cat_id}:social_conn_list")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def schedule_times_kb(selected: set[str], required: int) -> InlineKeyboardMarkup:
    """Time slot grid (06:00-23:00). Shows selected count vs required."""
    rows: list[list[InlineKeyboardButton]] = []
    row: list[InlineKeyboardButton] = []
    for hour in range(6, 24):
        time_str = f"{hour:02d}:00"
        mark = TOGGLE_ON if time_str in selected else ""
        row.append(InlineKeyboardButton(text=f"{mark}{time_str}", callback_data=f"sched:time:{time_str}"))
        if len(row) == 4:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    done_text = f"Готово ({len(selected)}/{required})"
    rows.append(
        [
            InlineKeyboardButton(text=done_text, callback_data="sched:times:done"),
            InlineKeyboardButton(text="Отмена", callback_data="sched:cancel"),
        ]
    )
    return InlineKeyboardMarkup(inline_keyboard=rows)


# ---------------------------------------------------------------------------
# Admin (UX_TOOLBOX section 16)
# ---------------------------------------------------------------------------


def admin_panel_kb() -> InlineKeyboardMarkup:
    """Admin panel main keyboard."""
    from bot.config import get_settings

    rows = [
        [InlineKeyboardButton(text="Затраты API (детально)", callback_data="admin:api_costs")],
        [InlineKeyboardButton(text="Просмотр пользователя", callback_data="admin:user_lookup")],
        [InlineKeyboardButton(text="Рассылка", callback_data="admin:broadcast")],
        [InlineKeyboardButton(text="Порталы и сервисы", callback_data="admin:portals")],
    ]
    if get_settings().bamboodom_enabled:
        rows.append([InlineKeyboardButton(text="Bamboodom.ru", callback_data="bamboodom:entry")])
    if get_settings().designservice_enabled:
        rows.append([InlineKeyboardButton(text="Designservice.group", callback_data="designservice:entry")])
    rows.append([InlineKeyboardButton(text="Главное меню", callback_data="nav:dashboard")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def admin_portals_kb() -> InlineKeyboardMarkup:
    """Admin portals & services keyboard with direct links to all external dashboards."""
    rows = [
        # --- Инфраструктура ---
        [InlineKeyboardButton(text="Railway — хостинг", url="https://railway.com/dashboard")],
        [InlineKeyboardButton(text="Supabase — база данных", url="https://supabase.com/dashboard")],
        [InlineKeyboardButton(text="Upstash — Redis / QStash", url="https://console.upstash.com")],
        [InlineKeyboardButton(text="Sentry — мониторинг", url="https://sentry.io")],
        # --- AI-провайдеры ---
        [InlineKeyboardButton(text="OpenRouter — AI модели", url="https://openrouter.ai/settings/credits")],
        [InlineKeyboardButton(text="Anthropic — Claude (BYOK)", url="https://console.anthropic.com/settings/billing")],
        [InlineKeyboardButton(text="Google AI Studio — Gemini (BYOK)", url="https://aistudio.google.com/apikey")],
        # --- SEO и данные ---
        [InlineKeyboardButton(text="DataForSEO — SEO-данные", url="https://app.dataforseo.com/api-dashboard")],
        [InlineKeyboardButton(text="Serper — поиск Google", url="https://serper.dev/dashboard")],
        [InlineKeyboardButton(text="Firecrawl — парсинг", url="https://www.firecrawl.dev/app")],
        # --- Платформы ---
        [InlineKeyboardButton(text="BotFather — Telegram-бот", url="https://t.me/BotFather")],
        [InlineKeyboardButton(text="Pinterest — разработчик", url="https://developers.pinterest.com/apps/")],
        # --- Платежи ---
        [InlineKeyboardButton(text="YooKassa — платежи", url="https://yookassa.ru/my")],
        # --- Назад ---
        [InlineKeyboardButton(text="К панели", callback_data="admin:panel")],
    ]
    return InlineKeyboardMarkup(inline_keyboard=rows)


def user_actions_kb(user_id: int, *, is_blocked: bool) -> InlineKeyboardMarkup:
    """User management action buttons for admin user card."""
    rows = [
        [
            InlineKeyboardButton(text="Начислить", callback_data=f"admin:user:{user_id}:credit"),
            InlineKeyboardButton(text="Списать", callback_data=f"admin:user:{user_id}:debit"),
        ],
    ]
    if is_blocked:
        rows.append(
            [InlineKeyboardButton(text="Разблокировать", callback_data=f"admin:user:{user_id}:unblock")],
        )
    else:
        rows.append(
            [InlineKeyboardButton(text="Заблокировать", callback_data=f"admin:user:{user_id}:block")],
        )
    rows.append(
        [InlineKeyboardButton(text="Активность", callback_data=f"admin:user:{user_id}:activity")],
    )
    rows.append(
        [InlineKeyboardButton(text="К панели", callback_data="admin:panel")],
    )
    return InlineKeyboardMarkup(inline_keyboard=rows)


def broadcast_audience_kb() -> InlineKeyboardMarkup:
    """Broadcast audience selection."""
    rows = [
        [InlineKeyboardButton(text="Все пользователи", callback_data="broadcast:audience:all")],
        [InlineKeyboardButton(text="Активные 7 дней", callback_data="broadcast:audience:active_7d")],
        [InlineKeyboardButton(text="Активные 30 дней", callback_data="broadcast:audience:active_30d")],
        [InlineKeyboardButton(text="Оплатившие", callback_data="broadcast:audience:paid")],
        [InlineKeyboardButton(text="Отмена", callback_data="admin:panel")],
    ]
    return InlineKeyboardMarkup(inline_keyboard=rows)


def broadcast_confirm_kb() -> InlineKeyboardMarkup:
    """Broadcast confirm/cancel."""
    rows = [
        [InlineKeyboardButton(text="Отправить", callback_data="broadcast:send", style=ButtonStyle.DANGER)],
        [InlineKeyboardButton(text="Отмена", callback_data="admin:panel")],
    ]
    return InlineKeyboardMarkup(inline_keyboard=rows)
