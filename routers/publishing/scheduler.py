"""Scheduler: presets, manual FSM (ScheduleSetupFSM), disable (UX_TOOLBOX section 13)."""

import html as html_mod

import structlog
from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardMarkup

from bot.fsm_utils import ensure_no_active_fsm
from bot.helpers import safe_callback_data, safe_edit_text, safe_message
from bot.texts import strings as S
from bot.texts.emoji import E
from bot.texts.screens import Screen
from db.models import User
from keyboards.inline import (
    _DAY_LABELS,
    _PRESETS,
    format_connection_display,
    schedule_count_kb,
    schedule_days_kb,
    schedule_times_kb,
    scheduler_cat_list_kb,
    scheduler_config_kb,
    scheduler_conn_list_kb,
    scheduler_crosspost_kb,
    scheduler_social_cat_list_kb,
    scheduler_social_config_kb,
    scheduler_social_conn_list_kb,
    scheduler_type_kb,
)
from services.scheduler import SchedulerService

log = structlog.get_logger()
router = Router()


def _extract_selected_from_keyboard(
    markup: InlineKeyboardMarkup | None,
) -> list[int]:
    """Extract selected connection IDs from cross-post keyboard checkmarks."""
    if not markup:
        return []
    selected: list[int] = []
    for row in markup.inline_keyboard:
        for btn in row:
            if btn.callback_data and btn.callback_data.endswith(":toggle") and btn.text.startswith("\u2713"):
                parts = btn.callback_data.split(":")
                if len(parts) >= 4:
                    selected.append(int(parts[3]))
    return selected


class ScheduleSetupFSM(StatesGroup):
    select_days = State()
    select_count = State()
    select_times = State()


# ---------------------------------------------------------------------------
# Entry: from project card
# ---------------------------------------------------------------------------


@router.callback_query(F.data.regexp(r"^project:\d+:scheduler$"))
async def scheduler_entry(
    callback: CallbackQuery,
) -> None:
    """Scheduler type selection — articles or social."""
    msg = safe_message(callback)
    if not msg:
        await callback.answer()
        return

    cb_data = safe_callback_data(callback)
    project_id = int(cb_data.split(":")[1])
    text = (
        Screen(E.SCHEDULE, S.SCHEDULER_TITLE)
        .progress(5, 5, "Автопубликация")
        .blank()
        .line(S.SCHEDULER_TYPE_PROMPT)
        .hint(S.SCHEDULER_TYPE_HINT)
        .build()
    )
    await safe_edit_text(msg, text, reply_markup=scheduler_type_kb(project_id))
    await callback.answer()


@router.callback_query(F.data.regexp(r"^project:\d+:sched_articles$"))
async def scheduler_articles_entry(
    callback: CallbackQuery,
    user: User,
    scheduler_service: SchedulerService,
) -> None:
    """Articles scheduler entry — filters WP-only connections downstream."""
    msg = safe_message(callback)
    if not msg:
        await callback.answer()
        return

    cb_data = safe_callback_data(callback)
    project_id = int(cb_data.split(":")[1])
    cats = await scheduler_service.get_project_categories(project_id, user.id)
    if cats is None:
        await callback.answer(S.PROJECT_NOT_FOUND, show_alert=True)
        return
    if not cats:
        await callback.answer(S.SCHEDULE_NO_CATEGORIES, show_alert=True)
        return

    text = (
        Screen(E.SCHEDULE, S.SCHEDULER_ARTICLES_TITLE)
        .blank()
        .line(S.SCHEDULER_SELECT_CATEGORY)
        .hint(S.SCHEDULER_CAT_HINT)
        .build()
    )
    await safe_edit_text(msg, text, reply_markup=scheduler_cat_list_kb(cats, project_id))
    await callback.answer()


@router.callback_query(F.data.regexp(r"^project:\d+:sched_social$"))
async def scheduler_social_entry(
    callback: CallbackQuery,
    user: User,
    scheduler_service: SchedulerService,
) -> None:
    """Social scheduler entry — filters social connections."""
    msg = safe_message(callback)
    if not msg:
        await callback.answer()
        return

    cb_data = safe_callback_data(callback)
    project_id = int(cb_data.split(":")[1])
    social_conns = await scheduler_service.get_social_connections(project_id, user.id)
    if social_conns is None:
        await callback.answer(S.PROJECT_NOT_FOUND, show_alert=True)
        return
    if not social_conns:
        await callback.answer(S.SCHEDULE_NO_SOCIAL, show_alert=True)
        return

    cats = await scheduler_service.get_project_categories(project_id, user.id)
    if not cats:
        await callback.answer(S.SCHEDULE_NO_CATEGORIES, show_alert=True)
        return

    text = (
        Screen(E.SCHEDULE, S.SCHEDULER_SOCIAL_TITLE)
        .blank()
        .line(S.SCHEDULER_SELECT_CATEGORY)
        .hint(S.SCHEDULER_CAT_HINT)
        .build()
    )
    await safe_edit_text(msg, text, reply_markup=scheduler_social_cat_list_kb(cats, project_id))
    await callback.answer()


# ---------------------------------------------------------------------------
# Category -> connections list
# ---------------------------------------------------------------------------


@router.callback_query(F.data.regexp(r"^scheduler:\d+:cat:\d+$"))
async def scheduler_category(
    callback: CallbackQuery,
    user: User,
    scheduler_service: SchedulerService,
) -> None:
    """Show connections with schedule summaries."""
    msg = safe_message(callback)
    if not msg:
        await callback.answer()
        return

    cb_data = safe_callback_data(callback)
    parts = cb_data.split(":")
    project_id = int(parts[1])
    cat_id = int(parts[3])

    wp_connections = await scheduler_service.get_wp_connections(project_id, user.id)
    if wp_connections is None:
        await callback.answer(S.PROJECT_NOT_FOUND, show_alert=True)
        return
    if not wp_connections:
        await callback.answer(S.SCHEDULE_NO_WP, show_alert=True)
        return

    schedules_map = await scheduler_service.get_category_schedules_map(cat_id)

    conn_text = (
        Screen(E.SCHEDULE, S.SCHEDULE_ARTICLES_CONN_TITLE)
        .blank().line(S.SCHEDULER_SELECT_CONNECTION)
        .hint(S.SCHEDULER_CONN_HINT).build()
    )
    await safe_edit_text(
        msg, conn_text,
        reply_markup=scheduler_conn_list_kb(wp_connections, schedules_map, cat_id, project_id),
    )
    await callback.answer()


# ---------------------------------------------------------------------------
# Back to connection list from config
# ---------------------------------------------------------------------------


@router.callback_query(F.data.regexp(r"^scheduler:\d+:conn_list$"))
async def scheduler_conn_list_back(
    callback: CallbackQuery,
    user: User,
    scheduler_service: SchedulerService,
) -> None:
    """Navigate back to article (WordPress) connection list."""
    msg = safe_message(callback)
    if not msg:
        await callback.answer()
        return

    cb_data = safe_callback_data(callback)
    cat_id = int(cb_data.split(":")[1])
    ctx = await scheduler_service.verify_category_ownership(cat_id, user.id)
    if not ctx:
        await callback.answer(S.CATEGORY_NOT_FOUND, show_alert=True)
        return

    wp_connections = await scheduler_service.get_wp_connections(ctx.project.id, user.id)
    schedules_map = await scheduler_service.get_category_schedules_map(cat_id)

    conn_text = (
        Screen(E.SCHEDULE, S.SCHEDULE_ARTICLES_CONN_TITLE)
        .blank().line(S.SCHEDULER_SELECT_CONNECTION)
        .hint(S.SCHEDULER_CONN_HINT).build()
    )
    await safe_edit_text(
        msg, conn_text,
        reply_markup=scheduler_conn_list_kb(wp_connections or [], schedules_map, cat_id, ctx.project.id),
    )
    await callback.answer()


# ---------------------------------------------------------------------------
# Connection -> config screen
# ---------------------------------------------------------------------------


@router.callback_query(F.data.regexp(r"^scheduler:\d+:conn:\d+$"))
async def scheduler_connection(
    callback: CallbackQuery,
    user: User,
    scheduler_service: SchedulerService,
) -> None:
    """Show schedule config for a connection."""
    msg = safe_message(callback)
    if not msg:
        await callback.answer()
        return

    cb_data = safe_callback_data(callback)
    parts = cb_data.split(":")
    cat_id = int(parts[1])
    conn_id = int(parts[3])

    ctx = await scheduler_service.verify_category_ownership(cat_id, user.id)
    if not ctx:
        await callback.answer(S.CATEGORY_NOT_FOUND, show_alert=True)
        return

    schedules_map = await scheduler_service.get_category_schedules_map(cat_id)
    existing = schedules_map.get(conn_id)

    s = Screen(E.SCHEDULE, S.SCHEDULE_TITLE)
    s.blank()
    if existing and existing.enabled:
        days_str = ", ".join(_DAY_LABELS.get(d, d) for d in existing.schedule_days)
        times_str = ", ".join(existing.schedule_times)
        s.line(f"{S.SCHEDULE_CURRENT}")
        s.line(f"  Дни: {days_str}")
        s.line(f"  Время: {times_str}")
        s.line(f"  Постов/день: {existing.posts_per_day}")
        s.blank()
    s.line(f"{S.SCHEDULE_SELECT_OPTION}")
    s.hint(S.SCHEDULE_HINT)
    text = s.build()

    await safe_edit_text(msg,
        text,
        reply_markup=scheduler_config_kb(
            cat_id,
            conn_id,
            existing is not None and existing.enabled,
            schedule_days=existing.schedule_days if existing and existing.enabled else None,
            posts_per_day=existing.posts_per_day if existing and existing.enabled else 0,
        ),
    )
    await callback.answer()


# ---------------------------------------------------------------------------
# Preset schedule
# ---------------------------------------------------------------------------


@router.callback_query(F.data.regexp(r"^sched:\d+:\d+:preset:(1w|3w|daily)$"))
async def scheduler_preset(
    callback: CallbackQuery,
    user: User,
    scheduler_service: SchedulerService,
) -> None:
    """Apply preset schedule."""
    msg = safe_message(callback)
    if not msg:
        await callback.answer()
        return

    cb_data = safe_callback_data(callback)
    parts = cb_data.split(":")
    cat_id = int(parts[1])
    conn_id = int(parts[2])
    preset_key = parts[4]

    preset = _PRESETS[preset_key]
    days, times, posts_per_day = preset[1], preset[2], preset[3]

    try:
        result = await scheduler_service.apply_schedule(
            cat_id,
            conn_id,
            user.id,
            days,
            times,
            posts_per_day,
        )
    except Exception:
        log.exception("preset_schedule_creation_failed", cat_id=cat_id, conn_id=conn_id, preset=preset_key)
        await callback.answer(S.SCHEDULE_ERROR_CREATE, show_alert=True)
        return

    if not result:
        await callback.answer(S.SCHEDULE_CONN_NOT_FOUND, show_alert=True)
        return

    display = html_mod.escape(format_connection_display(result.connection))
    is_social = result.connection.platform_type != "wordpress"

    if is_social:
        social_conns = await scheduler_service.get_social_connections_by_category(cat_id, user.id)
        has_other = len(social_conns or []) > 1
        reply_markup = scheduler_social_config_kb(
            cat_id, conn_id, has_schedule=True, has_other_social=has_other,
            schedule_days=days, posts_per_day=posts_per_day,
        )
    else:
        reply_markup = scheduler_config_kb(
            cat_id, conn_id, has_schedule=True,
            schedule_days=days, posts_per_day=posts_per_day,
        )

    set_text = (
        Screen(E.CHECK, S.SCHEDULE_SET_TITLE)
        .blank()
        .line(f"Подключение: {display}")
        .line(f"Режим: {preset[0]}")
        .line(S.SCHEDULE_COST_ESTIMATE.format(cost=result.weekly_cost))
        .hint(S.SCHEDULE_SET_HINT)
        .build()
    )
    await safe_edit_text(msg, set_text, reply_markup=reply_markup)
    await callback.answer()


# ---------------------------------------------------------------------------
# Disable schedule
# ---------------------------------------------------------------------------


@router.callback_query(F.data.regexp(r"^sched:\d+:\d+:disable$"))
async def scheduler_disable(
    callback: CallbackQuery,
    user: User,
    scheduler_service: SchedulerService,
) -> None:
    """Disable and delete schedule."""
    msg = safe_message(callback)
    if not msg:
        await callback.answer()
        return

    cb_data = safe_callback_data(callback)
    parts = cb_data.split(":")
    cat_id = int(parts[1])
    conn_id = int(parts[2])

    ok = await scheduler_service.disable_connection_schedule(cat_id, conn_id, user.id)
    if not ok:
        await callback.answer(S.CATEGORY_NOT_FOUND, show_alert=True)
        return

    conn = await scheduler_service.get_connection(conn_id, user.id)
    is_social = conn is not None and conn.platform_type != "wordpress"

    if is_social:
        social_conns = await scheduler_service.get_social_connections_by_category(cat_id, user.id)
        has_other = len(social_conns or []) > 1
        reply_markup = scheduler_social_config_kb(
            cat_id, conn_id, has_schedule=False, has_other_social=has_other,
        )
    else:
        reply_markup = scheduler_config_kb(cat_id, conn_id, has_schedule=False)

    await safe_edit_text(msg,
        S.SCHEDULE_DISABLED,
        reply_markup=reply_markup,
    )
    await callback.answer()


# ---------------------------------------------------------------------------
# Manual setup entry -> FSM
# ---------------------------------------------------------------------------


@router.callback_query(F.data.regexp(r"^sched:\d+:\d+:manual$"))
async def scheduler_manual(
    callback: CallbackQuery,
    user: User,
    scheduler_service: SchedulerService,
    state: FSMContext,
) -> None:
    """Enter manual schedule setup FSM."""
    msg = safe_message(callback)
    if not msg:
        await callback.answer()
        return

    cb_data = safe_callback_data(callback)
    parts = cb_data.split(":")
    cat_id = int(parts[1])
    conn_id = int(parts[2])

    # Verify ownership (callback_data tampering protection)
    ctx = await scheduler_service.verify_category_ownership(cat_id, user.id)
    if not ctx:
        await callback.answer(S.CATEGORY_NOT_FOUND, show_alert=True)
        return

    interrupted = await ensure_no_active_fsm(state)
    if interrupted:
        await msg.answer(f"Предыдущий процесс ({interrupted}) прерван.")

    # Check if schedule already exists (to restore button state on cancel)
    sched_has_schedule = await scheduler_service.has_active_schedule(cat_id, conn_id)

    await state.update_data(
        sched_cat_id=cat_id,
        sched_conn_id=conn_id,
        sched_days=[],
        sched_has_schedule=sched_has_schedule,
    )
    await state.set_state(ScheduleSetupFSM.select_days)

    text = (
        Screen(E.SCHEDULE, S.SCHEDULE_TITLE)
        .blank()
        .line(S.SCHEDULE_SELECT_DAYS)
        .hint(S.SCHEDULE_DAYS_HINT)
        .build()
    )
    await safe_edit_text(msg, text, reply_markup=schedule_days_kb(set()))
    await callback.answer()


# ---------------------------------------------------------------------------
# FSM: toggle day
# ---------------------------------------------------------------------------


@router.callback_query(
    ScheduleSetupFSM.select_days,
    F.data.regexp(r"^sched:day:(mon|tue|wed|thu|fri|sat|sun)$"),
)
async def schedule_day_toggle(callback: CallbackQuery, state: FSMContext) -> None:
    """Toggle day selection in FSM."""
    msg = safe_message(callback)
    if not msg:
        await callback.answer()
        return

    cb_data = safe_callback_data(callback)
    day = cb_data.split(":")[-1]
    data = await state.get_data()
    selected: set[str] = set(data.get("sched_days", []))

    if day in selected:
        selected.discard(day)
    else:
        selected.add(day)

    await state.update_data(sched_days=sorted(selected))
    await msg.edit_reply_markup(reply_markup=schedule_days_kb(selected))
    await callback.answer()


# ---------------------------------------------------------------------------
# FSM: days done
# ---------------------------------------------------------------------------


@router.callback_query(ScheduleSetupFSM.select_days, F.data == "sched:days:done")
async def schedule_days_done(callback: CallbackQuery, state: FSMContext) -> None:
    """Validate at least 1 day selected, move to count."""
    msg = safe_message(callback)
    if not msg:
        await callback.answer()
        return

    data = await state.get_data()
    selected = data.get("sched_days", [])

    if not selected:
        await callback.answer(S.SCHEDULE_SELECT_DAY, show_alert=True)
        return

    await state.set_state(ScheduleSetupFSM.select_count)
    text = (
        Screen(E.SCHEDULE, S.SCHEDULE_TITLE)
        .blank()
        .line(S.SCHEDULE_SELECT_COUNT)
        .hint(S.SCHEDULE_COUNT_HINT)
        .build()
    )
    await safe_edit_text(msg, text, reply_markup=schedule_count_kb())
    await callback.answer()


# ---------------------------------------------------------------------------
# FSM: count select
# ---------------------------------------------------------------------------


@router.callback_query(ScheduleSetupFSM.select_count, F.data.regexp(r"^sched:count:[1-5]$"))
async def schedule_count_select(callback: CallbackQuery, state: FSMContext) -> None:
    """Store count, move to time selection."""
    msg = safe_message(callback)
    if not msg:
        await callback.answer()
        return

    cb_data = safe_callback_data(callback)
    count = int(cb_data.split(":")[-1])
    await state.update_data(sched_count=count, sched_times=[])
    await state.set_state(ScheduleSetupFSM.select_times)

    text = (
        Screen(E.SCHEDULE, S.SCHEDULE_TITLE)
        .blank()
        .line(S.SCHEDULE_SELECT_TIMES.format(count=count))
        .hint(S.SCHEDULE_TIMES_HINT)
        .build()
    )
    await safe_edit_text(msg, text, reply_markup=schedule_times_kb(set(), count))
    await callback.answer()


# ---------------------------------------------------------------------------
# FSM: toggle time
# ---------------------------------------------------------------------------


@router.callback_query(ScheduleSetupFSM.select_times, F.data.regexp(r"^sched:time:\d{2}:00$"))
async def schedule_time_toggle(callback: CallbackQuery, state: FSMContext) -> None:
    """Toggle time slot selection."""
    msg = safe_message(callback)
    if not msg:
        await callback.answer()
        return

    # callback_data = "sched:time:10:00" -> extract "10:00"
    cb_data = safe_callback_data(callback)
    parts = cb_data.split(":")
    time_str = f"{parts[2]}:{parts[3]}"

    data = await state.get_data()
    selected: set[str] = set(data.get("sched_times", []))
    required: int = data.get("sched_count", 1)

    if time_str in selected:
        selected.discard(time_str)
    elif len(selected) < required:
        selected.add(time_str)
    else:
        await callback.answer(f"Максимум {required} слотов", show_alert=True)
        return

    await state.update_data(sched_times=sorted(selected))
    await msg.edit_reply_markup(reply_markup=schedule_times_kb(selected, required))
    await callback.answer()


# ---------------------------------------------------------------------------
# FSM: times done -> save
# ---------------------------------------------------------------------------


@router.callback_query(ScheduleSetupFSM.select_times, F.data == "sched:times:done")
async def schedule_times_done(
    callback: CallbackQuery,
    user: User,
    state: FSMContext,
    scheduler_service: SchedulerService,
) -> None:
    """Validate time count, create schedule."""
    msg = safe_message(callback)
    if not msg:
        await callback.answer()
        return

    data = await state.get_data()
    selected_times: list[str] = sorted(data.get("sched_times", []))
    required: int = data.get("sched_count", 1)

    if len(selected_times) != required:
        await callback.answer(f"Выберите ровно {required} слотов", show_alert=True)
        return

    cat_id: int = data["sched_cat_id"]
    conn_id: int = data["sched_conn_id"]
    selected_days: list[str] = sorted(data.get("sched_days", []))

    try:
        result = await scheduler_service.apply_schedule(
            cat_id,
            conn_id,
            user.id,
            selected_days,
            selected_times,
            required,
        )
    except Exception:
        log.exception("manual_schedule_creation_failed", cat_id=cat_id, conn_id=conn_id)
        await callback.answer(S.SCHEDULE_ERROR_CREATE, show_alert=True)
        await state.clear()
        return

    if not result:
        await callback.answer(S.SCHEDULE_CONN_NOT_FOUND, show_alert=True)
        await state.clear()
        return

    await state.clear()

    days_str = ", ".join(_DAY_LABELS.get(d, d) for d in selected_days)
    times_str = ", ".join(selected_times)

    is_social = result.connection.platform_type != "wordpress"
    if is_social:
        social_conns = await scheduler_service.get_social_connections_by_category(cat_id, user.id)
        has_other = len(social_conns or []) > 1
        reply_markup = scheduler_social_config_kb(
            cat_id, conn_id, has_schedule=True, has_other_social=has_other,
            schedule_days=list(selected_days), posts_per_day=required,
        )
    else:
        reply_markup = scheduler_config_kb(
            cat_id, conn_id, has_schedule=True,
            schedule_days=list(selected_days), posts_per_day=required,
        )

    set_text = (
        Screen(E.CHECK, S.SCHEDULE_SET_TITLE)
        .blank()
        .line(f"  Дни: {days_str}")
        .line(f"  Время: {times_str}")
        .line(f"  Постов/день: {required}")
        .line(S.SCHEDULE_COST_ESTIMATE.format(cost=result.weekly_cost))
        .hint(S.SCHEDULE_SET_HINT)
        .build()
    )
    await safe_edit_text(msg, set_text, reply_markup=reply_markup)
    await callback.answer()


# ---------------------------------------------------------------------------
# Social: connection list for category
# ---------------------------------------------------------------------------


@router.callback_query(F.data.regexp(r"^sched_social:\d+:cat:\d+$"))
async def scheduler_social_category(
    callback: CallbackQuery,
    user: User,
    scheduler_service: SchedulerService,
) -> None:
    """Show social connections with schedule summaries for a category."""
    msg = safe_message(callback)
    if not msg:
        await callback.answer()
        return

    cb_data = safe_callback_data(callback)
    parts = cb_data.split(":")
    project_id = int(parts[1])
    cat_id = int(parts[3])

    social_conns = await scheduler_service.get_social_connections(project_id, user.id)
    if social_conns is None:
        await callback.answer(S.PROJECT_NOT_FOUND, show_alert=True)
        return
    if not social_conns:
        await callback.answer(S.SCHEDULE_NO_SOCIAL, show_alert=True)
        return

    schedules_map = await scheduler_service.get_category_schedules_map(cat_id)

    social_text = (
        Screen(E.SCHEDULE, S.SCHEDULE_SOCIAL_CONN_TITLE)
        .blank().line(S.SCHEDULER_SELECT_CONNECTION)
        .hint(S.SCHEDULER_CONN_HINT).build()
    )
    await safe_edit_text(
        msg, social_text,
        reply_markup=scheduler_social_conn_list_kb(social_conns, schedules_map, cat_id, project_id),
    )
    await callback.answer()


@router.callback_query(F.data.regexp(r"^scheduler:\d+:social_conn_list$"))
async def scheduler_social_conn_list_back(
    callback: CallbackQuery,
    user: User,
    scheduler_service: SchedulerService,
) -> None:
    """Navigate back to social connection list."""
    msg = safe_message(callback)
    if not msg:
        await callback.answer()
        return

    cb_data = safe_callback_data(callback)
    cat_id = int(cb_data.split(":")[1])
    ctx = await scheduler_service.verify_category_ownership(cat_id, user.id)
    if not ctx:
        await callback.answer(S.CATEGORY_NOT_FOUND, show_alert=True)
        return

    social_conns = await scheduler_service.get_social_connections(ctx.project.id, user.id)
    schedules_map = await scheduler_service.get_category_schedules_map(cat_id)

    social_back_text = (
        Screen(E.SCHEDULE, S.SCHEDULE_SOCIAL_CONN_TITLE)
        .blank().line(S.SCHEDULER_SELECT_CONNECTION)
        .hint(S.SCHEDULER_CONN_HINT).build()
    )
    await safe_edit_text(
        msg, social_back_text,
        reply_markup=scheduler_social_conn_list_kb(social_conns or [], schedules_map, cat_id, ctx.project.id),
    )
    await callback.answer()


# ---------------------------------------------------------------------------
# Social: connection config
# ---------------------------------------------------------------------------


@router.callback_query(F.data.regexp(r"^sched_social:\d+:conn:\d+$"))
async def scheduler_social_connection(
    callback: CallbackQuery,
    user: User,
    scheduler_service: SchedulerService,
) -> None:
    """Show social schedule config with cross-post option."""
    msg = safe_message(callback)
    if not msg:
        await callback.answer()
        return

    cb_data = safe_callback_data(callback)
    parts = cb_data.split(":")
    cat_id = int(parts[1])
    conn_id = int(parts[3])

    ctx = await scheduler_service.verify_category_ownership(cat_id, user.id)
    if not ctx:
        await callback.answer(S.CATEGORY_NOT_FOUND, show_alert=True)
        return

    schedules_map = await scheduler_service.get_category_schedules_map(cat_id)
    existing = schedules_map.get(conn_id)

    social_conns = await scheduler_service.get_social_connections(ctx.project.id, user.id)
    has_other_social = len(social_conns or []) > 1

    s = Screen(E.SCHEDULE, S.SCHEDULE_SOCIAL_TITLE)
    s.blank()
    if existing and existing.enabled:
        days_str = ", ".join(_DAY_LABELS.get(d, d) for d in existing.schedule_days)
        times_str = ", ".join(existing.schedule_times)
        s.line(f"{S.SCHEDULE_CURRENT}")
        s.line(f"  Дни: {days_str}")
        s.line(f"  Время: {times_str}")
        s.line(f"  Постов/день: {existing.posts_per_day}")
        if existing.cross_post_connection_ids:
            s.line(f"  {S.CROSSPOST_TITLE}: {len(existing.cross_post_connection_ids)} платформ")
        s.blank()
    s.line(f"{S.SCHEDULE_SELECT_OPTION}")
    s.hint(S.SCHEDULE_SOCIAL_HINT)
    text = s.build()

    await safe_edit_text(msg, 
        text,
        reply_markup=scheduler_social_config_kb(
            cat_id,
            conn_id,
            existing is not None and existing.enabled,
            has_other_social,
            schedule_days=existing.schedule_days if existing and existing.enabled else None,
            posts_per_day=existing.posts_per_day if existing and existing.enabled else 0,
        ),
    )
    await callback.answer()


# ---------------------------------------------------------------------------
# Cross-post config
# ---------------------------------------------------------------------------


@router.callback_query(F.data.regexp(r"^sched_xp:\d+:\d+:config$"))
async def scheduler_crosspost_config(
    callback: CallbackQuery,
    user: User,
    scheduler_service: SchedulerService,
) -> None:
    """Show cross-post toggle screen."""
    msg = safe_message(callback)
    if not msg:
        await callback.answer()
        return

    cb_data = safe_callback_data(callback)
    parts = cb_data.split(":")
    cat_id = int(parts[1])
    conn_id = int(parts[2])

    config = await scheduler_service.get_crosspost_config(cat_id, conn_id, user.id)
    if not config:
        await callback.answer(S.SCHEDULE_CONN_NOT_FOUND, show_alert=True)
        return

    lead_display = html_mod.escape(format_connection_display(config.lead_connection))
    text = (
        Screen(E.SYNC, S.CROSSPOST_TITLE)
        .blank()
        .line(f"Ведущая платформа: {lead_display}")
        .blank()
        .line(S.CROSSPOST_PROMPT)
        .line(S.CROSSPOST_COST)
        .hint(S.CROSSPOST_CONFIG_HINT)
        .build()
    )

    await safe_edit_text(msg,
        text,
        reply_markup=scheduler_crosspost_kb(
            cat_id,
            conn_id,
            config.social_connections,
            config.selected_ids,
        ),
    )
    await callback.answer()


@router.callback_query(F.data.regexp(r"^sched_xp:\d+:\d+:\d+:toggle$"))
async def scheduler_crosspost_toggle(
    callback: CallbackQuery,
    user: User,
    scheduler_service: SchedulerService,
) -> None:
    """Toggle a cross-post target connection."""
    msg = safe_message(callback)
    if not msg:
        await callback.answer()
        return

    cb_data = safe_callback_data(callback)
    parts = cb_data.split(":")
    cat_id = int(parts[1])
    conn_id = int(parts[2])
    target_conn_id = int(parts[3])

    social_conns = await scheduler_service.get_social_connections_by_category(cat_id, user.id)
    if social_conns is None:
        await callback.answer(S.CATEGORY_NOT_FOUND, show_alert=True)
        return

    # P0-3: verify target_conn_id belongs to this project's social connections
    project_conn_ids = {c.id for c in social_conns}
    if target_conn_id not in project_conn_ids:
        await callback.answer("Подключение не найдено", show_alert=True)
        return

    # P0-2: read current selection from keyboard markup, not DB (avoids losing intermediate toggles)
    selected_ids: list[int] = _extract_selected_from_keyboard(msg.reply_markup)

    if target_conn_id in selected_ids:
        selected_ids.remove(target_conn_id)
    else:
        selected_ids.append(target_conn_id)

    await msg.edit_reply_markup(
        reply_markup=scheduler_crosspost_kb(cat_id, conn_id, social_conns, selected_ids),
    )
    await callback.answer()


@router.callback_query(F.data.regexp(r"^sched_xp:\d+:\d+:save$"))
async def scheduler_crosspost_save(
    callback: CallbackQuery,
    user: User,
    scheduler_service: SchedulerService,
) -> None:
    """Save cross_post_connection_ids to schedule."""
    msg = safe_message(callback)
    if not msg:
        await callback.answer()
        return

    cb_data = safe_callback_data(callback)
    parts = cb_data.split(":")
    cat_id = int(parts[1])
    conn_id = int(parts[2])

    selected_ids = _extract_selected_from_keyboard(msg.reply_markup)

    result = await scheduler_service.update_crosspost(cat_id, conn_id, user.id, selected_ids)
    if not result:
        await callback.answer("Расписание не найдено", show_alert=True)
        return

    if result.count:
        result_text = (
            Screen(E.CHECK, S.CROSSPOST_TITLE)
            .blank()
            .line(S.CROSSPOST_SAVED.format(count=result.count))
            .build()
        )
    else:
        result_text = (
            Screen(E.SYNC, S.CROSSPOST_TITLE)
            .blank()
            .line(S.CROSSPOST_DISABLED)
            .build()
        )

    await safe_edit_text(msg,
        result_text,
        reply_markup=scheduler_social_config_kb(
            cat_id,
            conn_id,
            has_schedule=True,
            has_other_social=result.has_other_social,
            schedule_days=result.schedule.schedule_days,
            posts_per_day=result.schedule.posts_per_day,
        ),
    )
    await callback.answer()


# ---------------------------------------------------------------------------
# FSM: cancel
# ---------------------------------------------------------------------------


@router.callback_query(F.data == "sched:cancel", ScheduleSetupFSM.select_days)
@router.callback_query(F.data == "sched:cancel", ScheduleSetupFSM.select_count)
@router.callback_query(F.data == "sched:cancel", ScheduleSetupFSM.select_times)
async def schedule_cancel(
    callback: CallbackQuery,
    state: FSMContext,
    user: User,
    scheduler_service: SchedulerService,
) -> None:
    """Cancel manual schedule setup, return to connection config."""
    msg = safe_message(callback)
    if not msg:
        await callback.answer()
        return

    data = await state.get_data()
    cat_id = data.get("sched_cat_id")
    conn_id = data.get("sched_conn_id")
    await state.clear()

    if cat_id and conn_id:
        cat_id_int, conn_id_int = int(cat_id), int(conn_id)
        conn = await scheduler_service.get_connection(conn_id_int, user.id)
        is_social = conn is not None and conn.platform_type != "wordpress"

        # Re-read current schedule to restore active preset display
        schedules_map = await scheduler_service.get_category_schedules_map(cat_id_int)
        existing = schedules_map.get(conn_id_int)
        sched_days = existing.schedule_days if existing and existing.enabled else None
        sched_ppd = existing.posts_per_day if existing and existing.enabled else 0
        has_sched = existing is not None and existing.enabled

        if is_social:
            social_conns = await scheduler_service.get_social_connections_by_category(cat_id_int, user.id)
            has_other = len(social_conns or []) > 1
            reply_markup = scheduler_social_config_kb(
                cat_id_int, conn_id_int,
                has_schedule=has_sched, has_other_social=has_other,
                schedule_days=sched_days, posts_per_day=sched_ppd,
            )
        else:
            reply_markup = scheduler_config_kb(
                cat_id_int, conn_id_int, has_schedule=has_sched,
                schedule_days=sched_days, posts_per_day=sched_ppd,
            )

        await safe_edit_text(msg,
            S.SCHEDULE_CANCELLED,
            reply_markup=reply_markup,
        )
    else:
        await safe_edit_text(msg, S.SCHEDULE_CANCELLED_FALLBACK)
    await callback.answer()
