import asyncio
import hashlib
import html
import json
import logging
from datetime import datetime, timedelta
from typing import Optional, Dict, Any, List
from zoneinfo import ZoneInfo
from aiogram import Bot
from aiogram.exceptions import TelegramForbiddenError, TelegramBadRequest, TelegramRetryAfter

from config import config
from api_client import ToeApiClient
from schedule_parser import (
    ScheduleParser,
    STATUS_ON,
    STATUS_OFF,
    STATUS_MAYBE,
    format_duration,
)
from database import db
from keyboards import get_status_inline_keyboard

logger = logging.getLogger(__name__)

_STALE_PIN_ERROR_MARKERS = (
    "message to edit not found",
    "message can't be edited",
    "message_id_invalid",
    "chat not found",
)


class NotifierService:
    def __init__(
        self,
        bot: Bot,
        api_client: ToeApiClient,
        bot_username: Optional[str] = None,
        station_monitor=None,
    ):
        self.bot = bot
        self.api = api_client
        self.bot_username = bot_username
        self.station_monitor = station_monitor
        self.is_running = False

    async def _send_with_retry(
        self,
        chat_id: int,
        text: str,
        silent: bool,
        sub: Optional[Dict[str, Any]] = None,
    ) -> None:
        try:
            await self.bot.send_message(
                chat_id=chat_id,
                text=text,
                parse_mode="HTML",
                disable_notification=silent,
                disable_web_page_preview=True,
            )
        except TelegramRetryAfter as e:
            await asyncio.sleep(float(e.retry_after) + 0.1)
            try:
                await self.bot.send_message(
                    chat_id=chat_id,
                    text=text,
                    parse_mode="HTML",
                    disable_notification=silent,
                    disable_web_page_preview=True,
                )
            except TelegramForbiddenError:
                logger.warning(f"User {chat_id} blocked bot on retry. Deactivating.")
                await db.update_setting(chat_id, "is_active", 0)
                if sub is not None:
                    sub["is_active"] = 0
            except Exception as retry_err:
                logger.error(f"Retry send failed for {chat_id}: {retry_err}")
        except TelegramForbiddenError:
            logger.warning(f"User {chat_id} blocked bot. Deactivating.")
            await db.update_setting(chat_id, "is_active", 0)
            if sub is not None:
                sub["is_active"] = 0
        except TelegramBadRequest as e:
            logger.warning(f"Bad request sending to {chat_id}: {e}")
        except Exception as e:
            logger.error(f"Error sending message to {chat_id}: {e}")

    async def broadcast(
        self,
        message: str,
        filter_key: Optional[str] = None,
        subscribers: Optional[List[Dict[str, Any]]] = None,
        now: Optional[datetime] = None,
    ):
        """
        Sends message to all active subscribers, checking individual user preferences
        and applying quiet night mode if active (23:00 - 07:00).
        """
        if now is None:
            tz = ZoneInfo(config.TIMEZONE)
            now = datetime.now(tz)
        is_night = now.hour >= 23 or now.hour < 7

        if subscribers is None:
            subscribers = await db.get_active_subscribers()

        for sub in subscribers:
            if not sub.get("is_active", 1):
                continue
            if filter_key and not sub.get(filter_key, 1):
                continue

            silent = is_night and bool(sub.get("quiet_night", 1))
            await self._send_with_retry(sub["chat_id"], message, silent=silent, sub=sub)
            await asyncio.sleep(0.05)

    async def update_pinned_messages(
        self,
        status_text: str,
        subscribers: Optional[List[Dict[str, Any]]] = None,
    ):
        """
        Updates pinned live status message for subscribers who have enabled it.
        Cleans up stale pinned_message_id values and handles flood limits.
        """
        if subscribers is None:
            subscribers = await db.get_active_subscribers()

        reply_markup = get_status_inline_keyboard(
            is_pinned=True,
            bot_username=self.bot_username,
        )

        for sub in subscribers:
            if not sub.get("is_active", 1):
                continue
            pinned_id = sub.get("pinned_message_id")
            if not pinned_id:
                continue

            chat_id = sub["chat_id"]
            try:
                await self.bot.edit_message_text(
                    chat_id=chat_id,
                    message_id=pinned_id,
                    text=status_text,
                    reply_markup=reply_markup,
                    parse_mode="HTML",
                )
            except TelegramForbiddenError:
                logger.warning(f"User {chat_id} blocked bot during pin update. Deactivating.")
                await db.update_setting(chat_id, "is_active", 0)
                await db.update_setting(chat_id, "pinned_message_id", None)
                sub["is_active"] = 0
                sub["pinned_message_id"] = None
            except TelegramRetryAfter as e:
                logger.warning(f"Flood control on pin update, sleeping {e.retry_after}s")
                await asyncio.sleep(float(e.retry_after) + 0.1)
            except TelegramBadRequest as e:
                err_lower = str(e).lower()
                if "message is not modified" in err_lower:
                    pass
                elif any(marker in err_lower for marker in _STALE_PIN_ERROR_MARKERS):
                    logger.info(f"Clearing stale pinned_message_id={pinned_id} for {chat_id}: {e}")
                    await db.update_setting(chat_id, "pinned_message_id", None)
                    sub["pinned_message_id"] = None
                else:
                    logger.warning(f"BadRequest updating pinned message for {chat_id}: {e}")
            except Exception as e:
                logger.warning(f"Error updating pinned message for {chat_id}: {e}")

            await asyncio.sleep(0.05)

    async def _collect_new_records(
        self,
        records: List[Dict[str, Any]],
        state_prefix: str,
    ) -> List[Dict[str, Any]]:
        """
        Deduplicates accident/repair records by semantic fingerprint against persisted state,
        marking newly seen records before broadcasting to prevent duplicate alerts on API resyncs.
        """
        if not records:
            return []

        sent_states = await db.get_states_by_prefix(state_prefix)
        new_records: List[Dict[str, Any]] = []
        seen_fps: set[str] = set()

        for rec in records:
            fp = ScheduleParser.accident_fingerprint(rec)
            state_key = f"{state_prefix}{fp}"
            if fp in seen_fps or state_key in sent_states:
                continue
            seen_fps.add(fp)
            await db.set_state(state_key, "1")
            new_records.append(rec)

        return new_records

    async def _check_accidents_and_planned(
        self,
        accidents_raw: Optional[List[Dict[str, Any]]],
        planed_raw: Optional[List[Dict[str, Any]]],
        subscribers: List[Dict[str, Any]],
        now: datetime,
    ) -> None:
        if accidents_raw is not None:
            accidents = [
                a
                for a in ScheduleParser.filter_accidents_for_building(
                    accidents_raw, config.BUILDING_NAME
                )
                if not ScheduleParser.is_gpv_accident(a)
            ]
            new_accidents = await self._collect_new_records(accidents, "acc_sent_")
            if new_accidents:
                lines = ["🚨 <b>Аварійне відключення:</b>"]
                for a in new_accidents:
                    lines.append(ScheduleParser.format_accident_line(a, is_planned=False))
                await self.broadcast(
                    "\n".join(lines),
                    filter_key="notify_outage",
                    subscribers=subscribers,
                    now=now,
                )

        if planed_raw is not None:
            planed = ScheduleParser.filter_accidents_for_building(
                planed_raw, config.BUILDING_NAME
            )
            new_planed = await self._collect_new_records(planed, "planed_sent_")
            if new_planed:
                lines = ["🛠 <b>Планові ремонтні роботи:</b>"]
                for p in new_planed:
                    lines.append(ScheduleParser.format_accident_line(p, is_planned=True))
                await self.broadcast(
                    "\n".join(lines),
                    filter_key="notify_schedule_change",
                    subscribers=subscribers,
                    now=now,
                )

    @staticmethod
    def _has_future_slot_changes(
        saved_times: Dict[str, Any],
        new_times: Dict[str, Any],
        now: datetime,
    ) -> bool:
        current_slot_floor = now.hour * 60 + (0 if now.minute < 30 else 30)
        for key in set(saved_times.keys()) | set(new_times.keys()):
            parts = str(key).split(":")
            if len(parts) != 2:
                continue
            try:
                slot_minutes = int(parts[0]) * 60 + int(parts[1])
            except ValueError:
                continue
            if slot_minutes >= current_slot_floor and saved_times.get(key) != new_times.get(key):
                return True
        return False

    async def _check_schedule_changes(
        self,
        day_sched: Optional[Dict[str, Any]],
        date_str: str,
        is_tomorrow: bool,
        subscribers: List[Dict[str, Any]],
        now: datetime,
    ) -> None:
        if not day_sched:
            return

        times = day_sched.get("times", {})
        times_json = json.dumps(times, sort_keys=True)
        new_hash = hashlib.md5(times_json.encode("utf-8")).hexdigest()
        hash_key = f"schedule_hash_{date_str}"
        times_key = f"schedule_times_{date_str}"
        saved_hash = await db.get_state(hash_key)
        has_outages = any(v in (STATUS_OFF, STATUS_MAYBE) for v in times.values())

        safe_addr = html.escape(config.short_address)
        formatted_date = ScheduleParser._format_date_graph(day_sched.get("dateGraph", ""))
        date_suffix = f" ({html.escape(formatted_date)})" if formatted_date else ""

        if saved_hash is None:
            await db.set_state(hash_key, new_hash)
            await db.set_state(times_key, times_json)
            if is_tomorrow:
                schedule_text = ScheduleParser.format_schedule_text(
                    day_sched, "завтра (НОВИЙ)", dt=now, include_header=False
                )
                header = (
                    f"📆 <b>Графік на завтра{date_suffix}</b>"
                    if has_outages
                    else f"📆 <b>Графік на завтра{date_suffix} — без відключень</b>"
                )
                msg = f"{header}\n📍 {safe_addr}\n\n{schedule_text}"
                await self.broadcast(
                    msg,
                    filter_key="notify_schedule_change",
                    subscribers=subscribers,
                    now=now,
                )
            elif has_outages:
                schedule_text = ScheduleParser.format_schedule_text(
                    day_sched, "сьогодні", dt=now, include_header=False
                )
                msg = f"📆 <b>Графік на сьогодні{date_suffix}</b>\n📍 {safe_addr}\n\n{schedule_text}"
                await self.broadcast(
                    msg,
                    filter_key="notify_schedule_change",
                    subscribers=subscribers,
                    now=now,
                )
        elif saved_hash != new_hash:
            saved_times_raw = await db.get_state(times_key)
            await db.set_state(hash_key, new_hash)
            await db.set_state(times_key, times_json)

            if not is_tomorrow and saved_times_raw:
                try:
                    saved_times = json.loads(saved_times_raw)
                except Exception:
                    saved_times = None
                if isinstance(saved_times, dict) and not self._has_future_slot_changes(
                    saved_times, times, now
                ):
                    logger.info("Only past timeslots changed for today; skipping broadcast.")
                    return

            day_label = "tomorrow" if is_tomorrow else "today"
            logger.info(f"{day_label.capitalize()} timeslots changed! Broadcasting notification...")
            if is_tomorrow:
                schedule_text = ScheduleParser.format_schedule_text(
                    day_sched, "завтра (ОНОВЛЕНО)", dt=now, include_header=False
                )
                header = f"🔄 <b>Графік на завтра{date_suffix} оновлено</b>"
            else:
                schedule_text = ScheduleParser.format_schedule_text(
                    day_sched, "сьогодні (ОНОВЛЕНО)", dt=now, include_header=False
                )
                header = f"🔄 <b>Графік на сьогодні{date_suffix} оновлено</b>"
            msg = f"{header}\n📍 {safe_addr}\n\n{schedule_text}"
            await self.broadcast(
                msg,
                filter_key="notify_schedule_change",
                subscribers=subscribers,
                now=now,
            )

    @staticmethod
    def _resolve_schedule_context(
        today_sched: Optional[Dict[str, Any]],
        tomorrow_sched: Optional[Dict[str, Any]],
        now: datetime,
    ) -> Dict[str, Any]:
        curr_minutes = now.hour * 60 + now.minute
        t_first_off = None
        if tomorrow_sched:
            t_intervals = ScheduleParser.get_intervals(tomorrow_sched.get("times", {}))
            if t_intervals and t_intervals[0]["start_minutes"] == 0 and t_intervals[0]["status"] == STATUS_OFF:
                t_first_off = t_intervals[0]

        if not today_sched:
            return {
                "curr_status": None,
                "curr_interval": None,
                "curr_off_end": None,
                "curr_off_rem_minutes": 0,
                "upcoming_off": None,
                "near_upcoming_off": None,
            }

        times = today_sched.get("times", {})
        intervals = ScheduleParser.get_intervals(times)
        current_status_info = ScheduleParser.get_current_status(times, now)
        curr_status = current_status_info["status"]
        curr_interval = current_status_info["current_interval"]

        curr_off_end = None
        curr_off_rem_minutes = 0
        if curr_status == STATUS_OFF and curr_interval:
            curr_off_end = curr_interval["end"]
            curr_off_rem_minutes = max(0, curr_interval["end_minutes"] - curr_minutes)
            if curr_interval["end_minutes"] == 24 * 60 and t_first_off:
                curr_off_end = f"{t_first_off['end']} (завтра)"
                curr_off_rem_minutes = (24 * 60 - curr_minutes) + t_first_off["end_minutes"]

        upcoming_off = None
        for inter in intervals:
            if inter["status"] == STATUS_OFF and inter["start_minutes"] > curr_minutes:
                upcoming_off = dict(inter)
                if upcoming_off["end_minutes"] == 24 * 60 and t_first_off:
                    upcoming_off["end"] = f"{t_first_off['end']} (завтра)"
                break

        if upcoming_off is None and curr_minutes >= 23 * 60 and t_first_off:
            upcoming_off = {
                **t_first_off,
                "start": "00:00",
                "start_minutes": 24 * 60,
                "end": f"{t_first_off['end']} (завтра)",
            }

        near_upcoming_off = None
        if upcoming_off and (upcoming_off["start_minutes"] - curr_minutes) <= 60:
            near_upcoming_off = upcoming_off

        return {
            "curr_status": curr_status,
            "curr_interval": curr_interval,
            "curr_off_end": curr_off_end,
            "curr_off_rem_minutes": curr_off_rem_minutes,
            "upcoming_off": upcoming_off,
            "near_upcoming_off": near_upcoming_off,
        }

    async def check_for_updates(self, now: Optional[datetime] = None):
        """
        Main periodic check cycle:
        1. Fetches schedule, emergency accidents, and planned repairs concurrently
        2. Notifies about building-specific emergency / planned outages
        3. Updates pinned live status messages (only when schedule API succeeded)
        4. Detects new/changed schedules per date without midnight duplicates
        5. Detects real-time power state transitions across midnight
        6. Sends advance warnings (without warning during active outage)
        7. Sends morning digest (08:00-08:30) and runs daily state cleanup
        """
        if now is None:
            tz = ZoneInfo(config.TIMEZONE)
            now = datetime.now(tz)

        today_str = now.strftime("%Y-%m-%d")
        tomorrow_str = (now + timedelta(days=1)).strftime("%Y-%m-%d")

        schedule_members, accidents_raw, planed_raw = await asyncio.gather(
            self.api.get_schedule(force_refresh=True),
            self.api.get_accidents(force_refresh=True),
            self.api.get_planed_accidents(force_refresh=True),
        )

        subscribers = await db.get_active_subscribers()

        try:
            await self._check_accidents_and_planned(accidents_raw, planed_raw, subscribers, now)
        except Exception as e:
            logger.error(f"Error checking accidents/planned repairs: {e}", exc_info=True)

        # Daily state cleanup regardless of whether today's schedule is published
        cleanup_key = f"state_cleanup_{today_str}"
        if not await db.get_state(cleanup_key):
            await db.set_state(cleanup_key, "1")
            deleted = await db.cleanup_old_state(keep_days=7)
            if deleted:
                logger.info(f"Cleaned up {deleted} stale rows from state table.")

        if schedule_members is None:
            logger.warning("Schedule API unavailable; skipping schedule updates and pinned message edits.")
            return

        today_graph = ScheduleParser.select_day_graph(schedule_members, today_str)
        tomorrow_graph = ScheduleParser.select_day_graph(schedule_members, tomorrow_str)

        today_sched = (
            ScheduleParser.extract_day_schedule(today_graph, config.CHER_GPV) if today_graph else None
        )
        tomorrow_sched = (
            ScheduleParser.extract_day_schedule(tomorrow_graph, config.CHER_GPV) if tomorrow_graph else None
        )

        # Update live pinned messages
        physical_grid = self.station_monitor.get_live_grid() if self.station_monitor else None
        live_status = ScheduleParser.format_status_message(
            today_sched, tomorrow_sched, now, physical_grid=physical_grid
        )
        pinned_text = f"📌 <b>ЖИВИЙ СТАТУС (ОНОВЛЮЄТЬСЯ АВТОМАТИЧНО)</b>\n\n{live_status}"
        await self.update_pinned_messages(pinned_text, subscribers=subscribers)

        # Check schedule changes for BOTH today and tomorrow before returning early if today is missing
        await self._check_schedule_changes(
            today_sched, today_str, is_tomorrow=False, subscribers=subscribers, now=now
        )
        await self._check_schedule_changes(
            tomorrow_sched, tomorrow_str, is_tomorrow=True, subscribers=subscribers, now=now
        )

        if not today_sched:
            return

        times = today_sched.get("times", {})
        curr_minutes = now.hour * 60 + now.minute
        sched_ctx = self._resolve_schedule_context(today_sched, tomorrow_sched, now)
        current_status = sched_ctx["curr_status"]
        curr_interval = sched_ctx["curr_interval"]

        last_status = await db.get_state("last_status")
        interval_start = curr_interval["start"] if curr_interval else "unknown"
        alert_sent_key = f"alert_sent_{today_str}_{interval_start}_{current_status}"

        station_live = physical_grid in ("present", "absent")

        # --- Real-Time Status Transitions ---
        if station_live:
            # Station telemetry is the single source of truth; suppress schedule ON/OFF/MAYBE broadcasts.
            if last_status is None and current_status in (STATUS_OFF, STATUS_ON):
                await db.set_state("last_status", current_status)
        else:
            if current_status == STATUS_OFF:
                if last_status != STATUS_OFF and not await db.get_state(alert_sent_key):
                    await db.set_state(alert_sent_key, "1")
                    await db.set_state("last_status", STATUS_OFF)
                    end_time = sched_ctx["curr_off_end"] or "кінця інтервалу"
                    rem_minutes = sched_ctx["curr_off_rem_minutes"]
                    dur_str = format_duration(rem_minutes) if rem_minutes > 0 else ""
                    dur_suffix = f" (на {dur_str})" if dur_str else ""
                    msg = f"🔴 <b>Світло вимкнено до {end_time}</b>{dur_suffix}"
                    await self.broadcast(msg, filter_key="notify_outage", subscribers=subscribers, now=now)

            elif current_status == STATUS_ON:
                if last_status == STATUS_OFF:
                    await db.set_state("last_status", STATUS_ON)
                    upcoming_off = sched_ctx["upcoming_off"]
                    if upcoming_off:
                        msg = f"🟢 <b>Світло є — наступне вимкнення о {upcoming_off['start']}</b>"
                    else:
                        msg = "🟢 <b>Світло є — сьогодні більше без вимкнень</b>"
                    await self.broadcast(msg, filter_key="notify_restore", subscribers=subscribers, now=now)
                elif last_status != STATUS_ON:
                    await db.set_state("last_status", STATUS_ON)

        # --- Advance Warnings ---
        if current_status != STATUS_OFF and physical_grid != "absent":
            intervals = ScheduleParser.get_intervals(times)
            candidate_intervals: List[Dict[str, Any]] = [
                {**inter, "date_str": today_str}
                for inter in intervals
                if inter["status"] == STATUS_OFF
            ]

            if curr_minutes >= 24 * 60 - 30 and tomorrow_sched:
                t_intervals = ScheduleParser.get_intervals(tomorrow_sched.get("times", {}))
                if t_intervals and t_intervals[0]["start_minutes"] == 0:
                    t_first = t_intervals[0]
                    if t_first["status"] == STATUS_OFF:
                        candidate_intervals.append(
                            {
                                **t_first,
                                "start": "00:00",
                                "start_minutes": 24 * 60,
                                "date_str": tomorrow_str,
                            }
                        )

            if candidate_intervals:
                sent_warns = await db.get_states_by_prefix(f"warn_{today_str}_")
                if curr_minutes >= 24 * 60 - 30:
                    sent_warns.update(await db.get_states_by_prefix(f"warn_{tomorrow_str}_"))

                for sub in subscribers:
                    if not sub.get("is_active", 1):
                        continue
                    warn_mins = sub.get("notify_warning", 15)
                    if not warn_mins or warn_mins <= 0:
                        continue

                    chat_id = sub["chat_id"]
                    is_night = now.hour >= 23 or now.hour < 7
                    silent = is_night and bool(sub.get("quiet_night", 1))

                    for inter in candidate_intervals:
                        diff = inter["start_minutes"] - curr_minutes
                        if 0 < diff <= warn_mins:
                            warn_state_key = f"warn_{inter['date_str']}_{chat_id}_{inter['start']}"
                            if warn_state_key in sent_warns:
                                continue

                            sent_warns[warn_state_key] = "1"
                            await db.set_state(warn_state_key, "1")

                            warning_msg = (
                                f"⚠️ <b>Через {diff} хв вимкнення ({inter['start']}–{inter['end']})</b>"
                            )
                            await self._send_with_retry(
                                chat_id, warning_msg, silent=silent, sub=sub
                            )
                            await asyncio.sleep(0.05)

        # --- Morning Digest (08:00 - 08:30) ---
        if now.hour == 8 and now.minute <= 30:
            digest_state_key = f"digest_sent_{today_str}"
            if not await db.get_state(digest_state_key):
                await db.set_state(digest_state_key, "1")
                digest_text = ScheduleParser.format_daily_digest(today_sched)
                await self.broadcast(
                    digest_text,
                    filter_key="daily_digest",
                    subscribers=subscribers,
                    now=now,
                )

    async def handle_station_transition(
        self,
        new_grid: str,
        now: Optional[datetime] = None,
    ) -> None:
        """
        Broadcasts a crisp 1-line factual grid transition reported by AllpowersBar
        ('present' or 'absent') enriched with TOE schedule context, and refreshes pinned cards.
        """
        if new_grid not in ("present", "absent"):
            return

        if now is None:
            tz = ZoneInfo(config.TIMEZONE)
            now = datetime.now(tz)

        schedule_members = None
        today_sched = None
        tomorrow_sched = None
        try:
            schedule_members = await self.api.get_schedule()
            if schedule_members is not None:
                today_str = now.strftime("%Y-%m-%d")
                tomorrow_str = (now + timedelta(days=1)).strftime("%Y-%m-%d")
                today_graph = ScheduleParser.select_day_graph(schedule_members, today_str)
                tomorrow_graph = ScheduleParser.select_day_graph(schedule_members, tomorrow_str)
                today_sched = (
                    ScheduleParser.extract_day_schedule(today_graph, config.CHER_GPV)
                    if today_graph
                    else None
                )
                tomorrow_sched = (
                    ScheduleParser.extract_day_schedule(tomorrow_graph, config.CHER_GPV)
                    if tomorrow_graph
                    else None
                )
        except Exception as e:
            logger.warning(f"Could not read schedule context for station transition: {e}")

        sched_ctx = self._resolve_schedule_context(today_sched, tomorrow_sched, now)

        if new_grid == "absent":
            if sched_ctx["curr_off_end"]:
                msg = f"🔴 <b>Світло зникло — до {sched_ctx['curr_off_end']} (за графіком)</b>"
            elif sched_ctx["near_upcoming_off"]:
                msg = (
                    f"🔴 <b>Світло зникло — раніше графіка "
                    f"(до {sched_ctx['near_upcoming_off']['end']})</b>"
                )
            else:
                msg = "🔴 <b>Світло зникло — поза графіком</b>"
            filter_key = "notify_outage"
            await db.set_state("last_status", STATUS_OFF)
        else:
            upcoming_off = sched_ctx["upcoming_off"]
            if upcoming_off:
                msg = f"🟢 <b>Світло є — наступне вимкнення о {upcoming_off['start']}</b>"
            elif today_sched:
                msg = "🟢 <b>Світло є — сьогодні більше без вимкнень</b>"
            else:
                msg = "🟢 <b>Світло є</b>"
            filter_key = "notify_restore"
            await db.set_state("last_status", STATUS_ON)

        subscribers = await db.get_active_subscribers()
        await self.broadcast(msg, filter_key=filter_key, subscribers=subscribers, now=now)

        if schedule_members is not None:
            try:
                live_status = ScheduleParser.format_status_message(
                    today_sched, tomorrow_sched, now, physical_grid=new_grid
                )
                pinned_text = f"📌 <b>ЖИВИЙ СТАТУС (ОНОВЛЮЄТЬСЯ АВТОМАТИЧНО)</b>\n\n{live_status}"
                await self.update_pinned_messages(pinned_text, subscribers=subscribers)
            except Exception as e:
                logger.warning(f"Could not refresh pinned status after station transition: {e}")

    async def start(self):
        """
        Starts the drift-free background polling loop.
        """
        self.is_running = True
        logger.info(
            f"Starting notifier background service with interval {config.CHECK_INTERVAL_SECONDS}s"
        )
        loop = asyncio.get_running_loop()
        while self.is_running:
            started_at = loop.time()
            try:
                await self.check_for_updates()
            except Exception as e:
                logger.error(f"Error in notifier check loop: {e}", exc_info=True)
            elapsed = loop.time() - started_at
            sleep_for = max(1.0, config.CHECK_INTERVAL_SECONDS - elapsed)
            await asyncio.sleep(sleep_for)

    def stop(self):
        self.is_running = False
