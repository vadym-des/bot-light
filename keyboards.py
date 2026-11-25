import urllib.parse
from typing import Optional
from aiogram.types import (
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    ReplyKeyboardMarkup,
    KeyboardButton,
)
from config import config

_runtime_bot_username: str = ""


def set_bot_username(username: Optional[str]) -> None:
    global _runtime_bot_username
    _runtime_bot_username = (username or "").strip().lstrip("@")


def get_bot_username() -> str:
    return _runtime_bot_username or config.BOT_USERNAME


def get_main_reply_keyboard() -> ReplyKeyboardMarkup:
    """
    Bottom persistent reply keyboard for fast single-tap access.
    """
    keyboard = [
        [
            KeyboardButton(text="💡 Поточний стан"),
            KeyboardButton(text="📅 Графік на сьогодні"),
        ],
        [
            KeyboardButton(text="📆 Графік на завтра"),
            KeyboardButton(text="🚨 Аварійні роботи"),
        ],
        [
            KeyboardButton(text="⚙️ Налаштування сповіщень"),
            KeyboardButton(text="ℹ️ Допомога"),
        ],
    ]
    return ReplyKeyboardMarkup(keyboard=keyboard, resize_keyboard=True)


def _build_share_row(effective_username: str) -> Optional[list[InlineKeyboardButton]]:
    if not effective_username:
        return None
    share_text = f"Бот відключень світла ({config.CITY_NAME}, {config.short_address}) ⚡️"
    bot_url = f"https://t.me/{effective_username}"
    share_url = (
        f"https://t.me/share/url?url={urllib.parse.quote(bot_url, safe='')}"
        f"&text={urllib.parse.quote(share_text)}"
    )
    return [InlineKeyboardButton(text="👥 Поділитися з сусідом", url=share_url)]


def get_status_inline_keyboard(
    is_pinned: bool = False,
    bot_username: Optional[str] = None,
) -> InlineKeyboardMarkup:
    """
    Inline keyboard attached to live status messages (pinned or regular).
    """
    effective_username = (bot_username or get_bot_username()).strip().lstrip("@")

    if is_pinned:
        keyboard = [
            [
                InlineKeyboardButton(text="🔄 Оновити зараз", callback_data="refresh_pinned_status"),
                InlineKeyboardButton(text="📅 Сьогодні", callback_data="btn_today"),
            ],
            [
                InlineKeyboardButton(text="📆 Завтра", callback_data="btn_tomorrow"),
            ],
        ]
    else:
        keyboard = [
            [
                InlineKeyboardButton(text="🔄 Оновити зараз", callback_data="refresh_status"),
                InlineKeyboardButton(text="📅 Сьогодні", callback_data="btn_today"),
            ],
            [
                InlineKeyboardButton(text="📆 Завтра", callback_data="btn_tomorrow"),
                InlineKeyboardButton(text="📌 Закріпити статус", callback_data="pin_live_status"),
            ],
        ]

    share_row = _build_share_row(effective_username)
    if share_row:
        keyboard.append(share_row)

    return InlineKeyboardMarkup(inline_keyboard=keyboard)


def get_schedule_inline_keyboard(day: str) -> InlineKeyboardMarkup:
    """
    Inline keyboard attached to daily schedule messages (today / tomorrow).
    """
    normalized_day = "tomorrow" if day == "tomorrow" else "today"
    other_btn = (
        InlineKeyboardButton(text="📅 Сьогодні", callback_data="btn_today")
        if normalized_day == "tomorrow"
        else InlineKeyboardButton(text="📆 Завтра", callback_data="btn_tomorrow")
    )
    keyboard = [
        [
            InlineKeyboardButton(text="🔄 Оновити графік", callback_data=f"refresh_{normalized_day}"),
            InlineKeyboardButton(text="💡 Поточний стан", callback_data="btn_status"),
        ],
        [other_btn],
    ]
    return InlineKeyboardMarkup(inline_keyboard=keyboard)


def get_settings_inline_keyboard(
    user_settings: dict,
    is_owner: bool = False,
) -> InlineKeyboardMarkup:
    """
    Interactive toggle buttons for notification preferences.
    """
    is_active = user_settings.get("is_active", 1)
    pause_label = "🔔 Усі сповіщення: Увімкнено" if is_active else "🔕 Усі сповіщення: Пауза"

    notify_outage = "✅ Увімкнено" if user_settings.get("notify_outage", 1) else "❌ Вимкнено"
    notify_restore = "✅ Увімкнено" if user_settings.get("notify_restore", 1) else "❌ Вимкнено"
    notify_change = "✅ Увімкнено" if user_settings.get("notify_schedule_change", 1) else "❌ Вимкнено"
    quiet_night = "🌙 Без звуку" if user_settings.get("quiet_night", 1) else "🔔 Зі звуком"
    daily_digest = "✅ Увімкнено" if user_settings.get("daily_digest", 0) else "❌ Вимкнено"

    warning_mins = user_settings.get("notify_warning", 15)
    if warning_mins == 15:
        warn_text = "⏱ За 15 хв"
    elif warning_mins == 30:
        warn_text = "⏱ За 30 хв"
    else:
        warn_text = "❌ Вимкнено"

    keyboard = [
        [
            InlineKeyboardButton(
                text=pause_label,
                callback_data="toggle_subscription_pause",
            )
        ],
        [
            InlineKeyboardButton(
                text=f"Вимкнення світла: {notify_outage}",
                callback_data="toggle_notify_outage",
            )
        ],
        [
            InlineKeyboardButton(
                text=f"Увімкнення світла: {notify_restore}",
                callback_data="toggle_notify_restore",
            )
        ],
        [
            InlineKeyboardButton(
                text=f"Зміна графіка: {notify_change}",
                callback_data="toggle_notify_schedule_change",
            )
        ],
        [
            InlineKeyboardButton(
                text=f"Попередження: {warn_text}",
                callback_data="cycle_notify_warning",
            )
        ],
        [
            InlineKeyboardButton(
                text=f"Нічний режим (23-07): {quiet_night}",
                callback_data="toggle_quiet_night",
            )
        ],
        [
            InlineKeyboardButton(
                text=f"Ранковий дайджест (08:00): {daily_digest}",
                callback_data="toggle_daily_digest",
            )
        ],
        [
            InlineKeyboardButton(text="📌 Закріпити живий статус", callback_data="pin_live_status")
        ],
    ]
    if is_owner:
        keyboard.append([
            InlineKeyboardButton(text="🔄 Перезапустити бота", callback_data="restart_bot")
        ])
    keyboard.append([
        InlineKeyboardButton(text="🔙 Закрити налаштування", callback_data="close_settings")
    ])
    return InlineKeyboardMarkup(inline_keyboard=keyboard)
