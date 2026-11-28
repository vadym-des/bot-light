import asyncio
import html
import logging
import os
import signal
from datetime import datetime, timedelta
from typing import Optional
from zoneinfo import ZoneInfo
from aiogram import Router, F, BaseMiddleware
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import Message, CallbackQuery, TelegramObject
from aiogram.filters import CommandStart, Command, or_f

from config import config
from api_client import ToeApiClient
from schedule_parser import ScheduleParser
from database import db, BOOLEAN_SETTING_KEYS
from keyboards import (
    get_main_reply_keyboard,
    get_status_inline_keyboard,
    get_schedule_inline_keyboard,
    get_settings_inline_keyboard,
)

logger = logging.getLogger(__name__)
router = Router()

PINNED_HEADER = "📌 <b>ЖИВИЙ СТАТУС (ОНОВЛЮЄТЬСЯ АВТОМАТИЧНО)</b>\n\n"


class AutoSubscribeMiddleware(BaseMiddleware):
    async def __call__(self, handler, event: TelegramObject, data: dict):
        user = data.get("event_from_user")
        if user and getattr(user, "is_bot", False):
            return await handler(event, data)

        chat = data.get("event_chat")
        if chat is None:
            chat = getattr(event, "chat", None)
            if chat is None and getattr(event, "message", None) is not None:
                chat = getattr(event.message, "chat", None)

        if chat is not None:
            try:
                chat_type = getattr(chat, "type", "private")
                if chat_type == "private":
                    await db.ensure_subscriber(
                        chat_id=chat.id,
                        username=getattr(user, "username", None) if user else None,
                        first_name=getattr(user, "first_name", None) if user else None,
                    )
                else:
                    await db.ensure_subscriber(
                        chat_id=chat.id,
                        username=getattr(chat, "username", None),
                        first_name=getattr(chat, "title", None),
                    )
            except Exception as e:
                logger.error(f"Error in AutoSubscribeMiddleware: {e}")

        return await handler(event, data)


router.message.middleware(AutoSubscribeMiddleware())
router.callback_query.middleware(AutoSubscribeMiddleware())


def _api_unavailable_message() -> str:
    return (
        "⚠️ <b>Не вдалося отримати дані від сервера «Тернопільобленерго».</b>\n"
        f"📍 {html.escape(config.short_address)} (Черга {html.escape(config.CHER_GPV)})\n\n"
        "Сервер обленерго тимчасово не відповідає або перевантажений.\n"
        "<i>Спробуйте оновити інформацію через хвилину.</i>"
    )


async def build_status_message(
    api_client: ToeApiClient,
    max_cache_age: Optional[float] = None,
    station_monitor=None,
) -> str:
    """
    Constructs the real-time status message based on current power schedule and time.
    """
    tz = ZoneInfo(config.TIMEZONE)
    now = datetime.now(tz)
    today_str = now.strftime("%Y-%m-%d")
    tomorrow_str = (now + timedelta(days=1)).strftime("%Y-%m-%d")

    schedule_members = await api_client.get_schedule(max_cache_age=max_cache_age)
    if schedule_members is None:
        return _api_unavailable_message()

    today_graph = ScheduleParser.select_day_graph(schedule_members, today_str)
    tomorrow_graph = ScheduleParser.select_day_graph(schedule_members, tomorrow_str)

    today_sched = (
        ScheduleParser.extract_day_schedule(today_graph, config.CHER_GPV) if today_graph else None
    )
    tomorrow_sched = (
        ScheduleParser.extract_day_schedule(tomorrow_graph, config.CHER_GPV) if tomorrow_graph else None
    )

    physical_grid = station_monitor.get_live_grid() if station_monitor else None
    return ScheduleParser.format_status_message(
        today_sched, tomorrow_sched, now, physical_grid=physical_grid
    )


async def build_today_message(
    api_client: ToeApiClient,
    max_cache_age: Optional[float] = None,
) -> str:
    tz = ZoneInfo(config.TIMEZONE)
    now = datetime.now(tz)
    today_str = now.strftime("%Y-%m-%d")

    schedule_members = await api_client.get_schedule(max_cache_age=max_cache_age)
    if schedule_members is None:
        return _api_unavailable_message()

    today_graph = ScheduleParser.select_day_graph(schedule_members, today_str)
    today_sched = (
        ScheduleParser.extract_day_schedule(today_graph, config.CHER_GPV) if today_graph else None
    )
    if not today_sched:
        formatted_today = now.strftime("%d.%m.%Y")
        return (
            f"ℹ️ <b>Офіційний графік на сьогодні ({formatted_today}) ще не оприлюднено обленерго.</b>\n"
            f"📍 {html.escape(config.short_address)} (Черга {html.escape(config.CHER_GPV)})\n\n"
            "⚠️ Якщо світло вимкнули прямо зараз, це може бути:\n"
            "• <b>Аварійне відключення (ГАВ/СГАВ)</b> за командою диспетчера «Укренерго»\n"
            "• <b>Позапланові ремонтні роботи</b> на лініях електромереж\n"
            "• Затримка завантаження погодинного графіка на офіційному сервері\n\n"
            "<i>Бот щохвилини перевіряє базу обленерго і сповістить, щойно графік з'явиться!</i>"
        )
    return ScheduleParser.format_schedule_text(today_sched, "сьогодні", dt=now)


async def build_tomorrow_message(
    api_client: ToeApiClient,
    max_cache_age: Optional[float] = None,
) -> str:
    tz = ZoneInfo(config.TIMEZONE)
    now = datetime.now(tz)
    tomorrow_dt = now + timedelta(days=1)
    tomorrow_str = tomorrow_dt.strftime("%Y-%m-%d")
    tomorrow_display = tomorrow_dt.strftime("%d.%m.%Y")

    schedule_members = await api_client.get_schedule(max_cache_age=max_cache_age)
    if schedule_members is None:
        return _api_unavailable_message()

    tomorrow_graph = ScheduleParser.select_day_graph(schedule_members, tomorrow_str)
    tomorrow_sched = (
        ScheduleParser.extract_day_schedule(tomorrow_graph, config.CHER_GPV) if tomorrow_graph else None
    )

    if not tomorrow_sched:
        if now.hour < 17:
            note = "Графік на завтра обленерго зазвичай оприлюднює ввечері (після 18:00 - 20:00)."
        elif 17 <= now.hour <= 22:
            note = "Обленерго ще готує графік на завтра. Як тільки він з'явиться — бот одразу надішле сповіщення!"
        else:
            note = "Графік на завтра наразі ще не оприлюднено обленерго. Бот надішле його одразу після публікації."

        return (
            f"📆 <b>Графік на завтра ({tomorrow_display})</b>\n"
            f"📍 {html.escape(config.short_address)} (Черга {html.escape(config.CHER_GPV)})\n\n"
            f"ℹ️ {note}"
        )
    return ScheduleParser.format_schedule_text(tomorrow_sched, "завтра", dt=now)


async def _safe_edit_message(
    call: CallbackQuery,
    text: str,
    reply_markup,
) -> None:
    try:
        await call.message.edit_text(text, reply_markup=reply_markup, parse_mode="HTML")
        await call.answer("✅ Дані оновлено!")
    except TelegramBadRequest as e:
        if "message is not modified" in str(e).lower():
            await call.answer("ℹ️ Дані вже актуальні.")
        else:
            logger.warning(f"TelegramBadRequest while editing message: {e}")
            await call.answer("⚠️ Не вдалося оновити повідомлення.")
    except Exception as e:
        logger.error(f"Unexpected error editing message: {e}")
        await call.answer("⚠️ Помилка при оновленні.")


def _is_owner(chat_id: int) -> bool:
    return bool(config.DEFAULT_CHAT_ID and chat_id == config.DEFAULT_CHAT_ID)


async def _trigger_restart(api_client: ToeApiClient) -> tuple[bool, str]:
    hook_url = config.RENDER_DEPLOY_HOOK_URL
    if hook_url:
        try:
            session = await api_client.get_session()
            async with session.post(hook_url) as resp:
                if 200 <= resp.status < 300:
                    return (
                        True,
                        "🔄 <b>Запущено оновлення та перезапуск бота на Render.</b>\n"
                        "<i>Нова версія запрацює за 1–2 хвилини.</i>",
                    )
                body = await resp.text()
                logger.warning(f"Render deploy hook returned HTTP {resp.status}: {body[:200]}")
                return (
                    False,
                    f"⚠️ <b>Не вдалося викликати Render Deploy Hook (HTTP {resp.status}).</b>",
                )
        except Exception as e:
            logger.warning(f"Failed to trigger Render deploy hook: {e}")
            return False, "⚠️ <b>Помилка при виклику Render Deploy Hook.</b>"

    asyncio.get_running_loop().call_later(1.0, os.kill, os.getpid(), signal.SIGTERM)
    return (
        True,
        "🔄 <b>Перезапускаю процес бота...</b>\n"
        "<i>Для автооновлення коду після push увімкніть Auto-Deploy на Render або задайте RENDER_DEPLOY_HOOK_URL.</i>",
    )


def _format_subscription_summary(sub: Optional[dict]) -> str:
    sub = sub or {}
    outage = "✅" if sub.get("notify_outage", 1) else "❌"
    restore = "✅" if sub.get("notify_restore", 1) else "❌"
    change = "✅" if sub.get("notify_schedule_change", 1) else "❌"
    warn_mins = sub.get("notify_warning", 15)
    warn_text = f"за {warn_mins} хв" if warn_mins and warn_mins > 0 else "вимкнено"
    quiet = "✅ (23:00–07:00)" if sub.get("quiet_night", 1) else "❌ (зі звуком)"
    digest = "✅" if sub.get("daily_digest", 0) else "❌"
    active_note = (
        "🔔 <b>Ваші активні сповіщення:</b>"
        if sub.get("is_active", 1)
        else "🔔 <b>Сповіщення відновлено з такими налаштуваннями:</b>"
    )
    return (
        f"{active_note}\n"
        f"• 🔴 Початок вимкнення: {outage}\n"
        f"• 🟢 Включення світла: {restore}\n"
        f"• 🔄 Зміни графіка: {change}\n"
        f"• ⚠️ Попередження: {warn_text}\n"
        f"• 🌙 Тихий режим уночі: {quiet}\n"
        f"• ☕️ Ранковий дайджест (08:00): {digest}"
    )


async def _send_fresh_status(
    chat_id: int,
    bot,
    send_fn,
    api_client: ToeApiClient,
    sub: Optional[dict] = None,
    station_monitor=None,
) -> None:
    """Deletes the previous status card (if any), sends a fresh one, and persists its message_id."""
    if sub is None:
        sub = await db.get_subscriber(chat_id)

    if sub and sub.get("last_status_message_id"):
        try:
            await bot.delete_message(chat_id, sub["last_status_message_id"])
        except Exception:
            pass

    status_text = await build_status_message(api_client, station_monitor=station_monitor)
    sent_msg = await send_fn(
        status_text,
        reply_markup=get_status_inline_keyboard(is_pinned=False),
        parse_mode="HTML",
    )
    await db.update_setting(chat_id, "last_status_message_id", sent_msg.message_id)


@router.message(CommandStart())
async def cmd_start(message: Message, api_client: ToeApiClient, station_monitor=None):
    chat_id = message.chat.id
    user = message.from_user
    if message.chat.type == "private":
        username = user.username if user else None
        raw_name = (user.first_name if user else None) or "Користувач"
    else:
        username = message.chat.username
        raw_name = message.chat.title or (user.first_name if user else None) or "Чат"

    await db.add_subscriber(chat_id, username, raw_name, reactivate=True)
    sub = await db.get_subscriber(chat_id)

    safe_name = html.escape(raw_name)
    summary = _format_subscription_summary(sub)

    greeting = (
        f"👋 <b>Вітаю, {safe_name}!</b>\n\n"
        f"Бот моніторить відключення та включення світла для:\n"
        f"📍 <b>{html.escape(config.full_address)}</b>\n"
        f"⚡️ <b>Черга ГПВ:</b> <b>{html.escape(config.CHER_GPV)}</b>\n\n"
        f"{summary}\n\n"
        f"<i>(Усі параметри можна змінити в /settings, а поставити на паузу — через /stop)</i>\n\n"
        f"👇 Оберіть дію в меню:"
    )

    await message.answer(greeting, reply_markup=get_main_reply_keyboard(), parse_mode="HTML")
    await _send_fresh_status(
        chat_id,
        message.bot,
        message.answer,
        api_client,
        sub=sub,
        station_monitor=station_monitor,
    )


@router.message(Command("stop", "unsubscribe"))
async def cmd_stop(message: Message):
    chat_id = message.chat.id
    user = message.from_user
    if message.chat.type == "private":
        username = user.username if user else None
        first_name = user.first_name if user else None
    else:
        username = message.chat.username
        first_name = message.chat.title

    await db.ensure_subscriber(chat_id, username=username, first_name=first_name)
    await db.update_setting(chat_id, "is_active", 0)
    await message.answer(
        "🔕 <b>Сповіщення призупинено.</b>\n\n"
        "Ви більше не отримуватимете автоматичні push-повідомлення.\n"
        "Команди перевірки графіка (/status, /today, /tomorrow) продовжують працювати.\n\n"
        "Щоб знову увімкнути сповіщення — натисніть /start або змініть статус у /settings.",
        parse_mode="HTML",
    )


@router.message(or_f(Command("status", "now"), F.text == "💡 Поточний стан"))
async def handle_status(message: Message, api_client: ToeApiClient, station_monitor=None):
    await _send_fresh_status(
        message.chat.id,
        message.bot,
        message.answer,
        api_client,
        station_monitor=station_monitor,
    )


@router.callback_query(F.data == "btn_status")
async def cb_btn_status(call: CallbackQuery, api_client: ToeApiClient, station_monitor=None):
    await _send_fresh_status(
        call.message.chat.id,
        call.bot,
        call.message.answer,
        api_client,
        station_monitor=station_monitor,
    )
    await call.answer()


@router.callback_query(F.data == "refresh_status")
async def cb_refresh_status(call: CallbackQuery, api_client: ToeApiClient, station_monitor=None):
    status_text = await build_status_message(
        api_client, max_cache_age=10.0, station_monitor=station_monitor
    )
    await _safe_edit_message(call, status_text, get_status_inline_keyboard(is_pinned=False))


@router.callback_query(F.data == "refresh_pinned_status")
async def cb_refresh_pinned_status(call: CallbackQuery, api_client: ToeApiClient, station_monitor=None):
    status_text = await build_status_message(
        api_client, max_cache_age=10.0, station_monitor=station_monitor
    )
    await _safe_edit_message(
        call,
        f"{PINNED_HEADER}{status_text}",
        get_status_inline_keyboard(is_pinned=True),
    )


@router.callback_query(F.data == "pin_live_status")
async def cb_pin_live_status(call: CallbackQuery, api_client: ToeApiClient, station_monitor=None):
    chat_id = call.message.chat.id
    sub = await db.get_subscriber(chat_id)
    old_pinned_id = sub.get("pinned_message_id") if sub else None

    status_text = await build_status_message(api_client, station_monitor=station_monitor)
    sent_msg = await call.message.answer(
        f"{PINNED_HEADER}{status_text}",
        reply_markup=get_status_inline_keyboard(is_pinned=True),
        parse_mode="HTML",
    )
    try:
        await call.bot.pin_chat_message(chat_id, sent_msg.message_id, disable_notification=True)
    except Exception as e:
        logger.error(f"Failed to pin message in {chat_id}: {e}")
        try:
            await sent_msg.delete()
        except Exception:
            pass
        await call.answer(
            "⚠️ Не вдалося закріпити повідомлення (перевірте дозволи чату).",
            show_alert=True,
        )
        return

    if old_pinned_id and old_pinned_id != sent_msg.message_id:
        try:
            await call.bot.unpin_chat_message(chat_id=chat_id, message_id=old_pinned_id)
        except Exception:
            pass
        try:
            await call.bot.edit_message_text(
                chat_id=chat_id,
                message_id=old_pinned_id,
                text="ℹ️ <i>Цей віджет замінено новішим закріпленим статусом.</i>",
                parse_mode="HTML",
            )
        except Exception:
            pass

    await db.update_setting(chat_id, "pinned_message_id", sent_msg.message_id)
    await call.answer(
        "📌 Віджет закріплено зверху чату! Він оновлюватиметься автоматично.",
        show_alert=True,
    )


@router.message(or_f(Command("today"), F.text == "📅 Графік на сьогодні"))
@router.callback_query(F.data == "btn_today")
async def handle_today(event: Message | CallbackQuery, api_client: ToeApiClient):
    text = await build_today_message(api_client)
    markup = get_schedule_inline_keyboard("today")

    if isinstance(event, CallbackQuery):
        await event.message.answer(text, reply_markup=markup, parse_mode="HTML")
        await event.answer()
    else:
        await event.answer(text, reply_markup=markup, parse_mode="HTML")


@router.callback_query(F.data == "refresh_today")
async def cb_refresh_today(call: CallbackQuery, api_client: ToeApiClient):
    text = await build_today_message(api_client, max_cache_age=10.0)
    await _safe_edit_message(call, text, get_schedule_inline_keyboard("today"))


@router.message(or_f(Command("tomorrow"), F.text == "📆 Графік на завтра"))
@router.callback_query(F.data == "btn_tomorrow")
async def handle_tomorrow(event: Message | CallbackQuery, api_client: ToeApiClient):
    text = await build_tomorrow_message(api_client)
    markup = get_schedule_inline_keyboard("tomorrow")

    if isinstance(event, CallbackQuery):
        await event.message.answer(text, reply_markup=markup, parse_mode="HTML")
        await event.answer()
    else:
        await event.answer(text, reply_markup=markup, parse_mode="HTML")


@router.callback_query(F.data == "refresh_tomorrow")
async def cb_refresh_tomorrow(call: CallbackQuery, api_client: ToeApiClient):
    text = await build_tomorrow_message(api_client, max_cache_age=10.0)
    await _safe_edit_message(call, text, get_schedule_inline_keyboard("tomorrow"))


@router.message(or_f(Command("accidents"), F.text == "🚨 Аварійні роботи"))
async def handle_accidents(message: Message, api_client: ToeApiClient):
    accidents_raw, planed_raw = await asyncio.gather(
        api_client.get_accidents(),
        api_client.get_planed_accidents(),
    )

    lines = [
        "🚨 <b>Аварійні та ремонтні роботи</b>",
        f"📍 <b>Адреса:</b> {html.escape(config.full_address)}\n",
    ]

    if accidents_raw is None and planed_raw is None:
        lines.append("⚠️ <b>Не вдалося перевірити дані про аварії та ремонти.</b>")
        lines.append("Сервер обленерго тимчасово не відповідає. Спробуйте пізніше.")
        await message.answer("\n".join(lines), parse_mode="HTML")
        return

    accidents = (
        ScheduleParser.filter_accidents_for_building(accidents_raw, config.BUILDING_NAME)
        if accidents_raw is not None
        else []
    )
    real_accidents = [a for a in accidents if not ScheduleParser.is_gpv_accident(a)]
    gpv_accidents = [a for a in accidents if ScheduleParser.is_gpv_accident(a)]
    planed = (
        ScheduleParser.filter_accidents_for_building(planed_raw, config.BUILDING_NAME)
        if planed_raw is not None
        else []
    )

    if not real_accidents and not planed and accidents_raw is not None and planed_raw is not None:
        lines.append("✅ <b>Аварійних або планових ремонтних робіт за вашою адресою не зафіксовано.</b>")
        if gpv_accidents:
            lines.append("\n⚡️ <b>Зараз діє погодинне відключення (ГПВ):</b>")
            for g in gpv_accidents:
                lines.append(ScheduleParser.format_accident_line(g, is_planned=False))
        else:
            lines.append("Мережа працює в штатному режимі.")
    else:
        if accidents_raw is None:
            lines.append("⚠️ <i>Не вдалося перевірити аварійні відключення (помилка API).</i>\n")
        else:
            if real_accidents:
                lines.append("⚠️ <b>Аварійні відключення:</b>")
                for a in real_accidents:
                    lines.append(ScheduleParser.format_accident_line(a, is_planned=False))
                lines.append("")
            if gpv_accidents:
                lines.append("⚡️ <b>Погодинне відключення (ГПВ):</b>")
                for g in gpv_accidents:
                    lines.append(ScheduleParser.format_accident_line(g, is_planned=False))
                lines.append("")

        if planed_raw is None:
            lines.append("⚠️ <i>Не вдалося перевірити планові ремонти (помилка API).</i>\n")
        elif planed:
            lines.append("🛠 <b>Планові ремонти:</b>")
            for p in planed:
                lines.append(ScheduleParser.format_accident_line(p, is_planned=True))
            lines.append("")

        if (accidents_raw is None or planed_raw is None) and not real_accidents and not gpv_accidents and not planed:
            lines.append("<i>За доступним розділом робіт не зафіксовано.</i>")

    await message.answer("\n".join(lines), parse_mode="HTML")


@router.message(or_f(Command("settings"), F.text == "⚙️ Налаштування сповіщень"))
async def handle_settings(message: Message):
    chat_id = message.chat.id
    sub = await db.get_subscriber(chat_id)
    if not sub:
        user = message.from_user
        username = (
            (user.username if user else None)
            if message.chat.type == "private"
            else message.chat.username
        )
        first_name = (
            (user.first_name if user else None)
            if message.chat.type == "private"
            else message.chat.title
        )
        await db.add_subscriber(chat_id, username, first_name, reactivate=False)
        sub = await db.get_subscriber(chat_id)

    text = (
        "⚙️ <b>Налаштування сповіщень та комфорту</b>\n\n"
        "Натискайте кнопки нижче для налаштування під себе:"
    )
    await message.answer(
        text,
        reply_markup=get_settings_inline_keyboard(sub or {}, is_owner=_is_owner(chat_id)),
        parse_mode="HTML",
    )


@router.callback_query(F.data == "toggle_subscription_pause")
async def cb_toggle_subscription_pause(call: CallbackQuery):
    chat_id = call.message.chat.id
    sub = await db.get_subscriber(chat_id)
    if not sub:
        await call.answer()
        return

    curr_val = sub.get("is_active", 1)
    new_val = 0 if curr_val else 1
    await db.update_setting(chat_id, "is_active", new_val)

    updated_sub = await db.get_subscriber(chat_id)
    try:
        await call.message.edit_reply_markup(
            reply_markup=get_settings_inline_keyboard(
                updated_sub or {}, is_owner=_is_owner(chat_id)
            )
        )
    except TelegramBadRequest:
        pass
    await call.answer("🔔 Усі сповіщення увімкнено" if new_val else "🔕 Сповіщення на паузі")


@router.callback_query(F.data.startswith("toggle_"))
async def cb_toggle_setting(call: CallbackQuery):
    key = call.data.removeprefix("toggle_")
    if key not in BOOLEAN_SETTING_KEYS:
        await call.answer("⚠️ Невідоме налаштування.")
        return

    chat_id = call.message.chat.id
    sub = await db.get_subscriber(chat_id)
    if not sub:
        await call.answer()
        return

    current_val = sub.get(key, 1)
    new_val = 0 if current_val else 1
    await db.update_setting(chat_id, key, new_val)

    updated_sub = await db.get_subscriber(chat_id)
    try:
        await call.message.edit_reply_markup(
            reply_markup=get_settings_inline_keyboard(
                updated_sub or {}, is_owner=_is_owner(chat_id)
            )
        )
    except TelegramBadRequest:
        pass
    status_label = "увімкнено" if new_val else "вимкнено"
    await call.answer(f"Налаштування {status_label}")


@router.callback_query(F.data == "cycle_notify_warning")
async def cb_cycle_warning(call: CallbackQuery):
    chat_id = call.message.chat.id
    sub = await db.get_subscriber(chat_id)
    if not sub:
        await call.answer()
        return

    curr_val = sub.get("notify_warning", 15)
    if curr_val == 15:
        new_val = 30
    elif curr_val == 30:
        new_val = 0
    else:
        new_val = 15

    await db.update_setting(chat_id, "notify_warning", new_val)
    updated_sub = await db.get_subscriber(chat_id)
    try:
        await call.message.edit_reply_markup(
            reply_markup=get_settings_inline_keyboard(
                updated_sub or {}, is_owner=_is_owner(chat_id)
            )
        )
    except TelegramBadRequest:
        pass
    msg = f"Попередження: {new_val} хв" if new_val else "Попередження вимкнено"
    await call.answer(msg)


@router.message(Command("restart"))
async def cmd_restart(message: Message, api_client: ToeApiClient):
    if not _is_owner(message.chat.id):
        await message.answer("⛔️ Команда доступна лише власнику бота.")
        return
    ok, response_text = await _trigger_restart(api_client)
    if ok:
        await db.set_state("pending_restart_notify_chat_id", str(message.chat.id))
    await message.answer(response_text, parse_mode="HTML")


@router.callback_query(F.data == "restart_bot")
async def cb_restart_bot(call: CallbackQuery, api_client: ToeApiClient):
    if not _is_owner(call.message.chat.id):
        await call.answer("⛔️ Команда доступна лише власнику бота.", show_alert=True)
        return
    ok, response_text = await _trigger_restart(api_client)
    if ok:
        await db.set_state("pending_restart_notify_chat_id", str(call.message.chat.id))
    await call.answer("🔄 Перезапуск..." if ok else "⚠️ Помилка", show_alert=False)
    await call.message.answer(response_text, parse_mode="HTML")


@router.callback_query(F.data == "close_settings")
async def cb_close_settings(call: CallbackQuery):
    try:
        await call.message.delete()
    except Exception:
        pass
    await call.answer("Налаштування збережено.")


@router.message(or_f(Command("help"), F.text == "ℹ️ Допомога"))
async def handle_help(message: Message):
    text = (
        "📖 <b>Довідка по боту:</b>\n\n"
        "Бот працює 24/7 та щохвилини перевіряє офіційний портал «Тернопільобленерго» (poweron.toe.com.ua).\n\n"
        "<b>Можливості та команди:</b>\n"
        "• 💡 <b>Поточний стан</b> (/status) — чи є світло зараз і коли наступне вимкнення\n"
        "• 📅 <b>Графік на сьогодні</b> (/today) — розклад на добу\n"
        "• 📆 <b>Графік на завтра</b> (/tomorrow) — розклад на наступний день\n"
        "• 🚨 <b>Аварійні роботи</b> (/accidents) — позапланові аварії та ремонти для вашого будинку\n"
        "• ⚙️ <b>Налаштування</b> (/settings) — нічний режим, ранковий дайджест, час попередження\n"
        "• 📌 <b>Закріплений статус</b> — закріплює живе повідомлення у верхній частині чату\n"
        "• 🔕 /stop — поставити сповіщення на паузу\n\n"
        f"🎯 <b>Адреса:</b> {html.escape(config.full_address)} (Черга {html.escape(config.CHER_GPV)})"
    )
    await message.answer(text, reply_markup=get_main_reply_keyboard(), parse_mode="HTML")
