import html
import logging
import re
from datetime import datetime
from typing import Dict, List, Optional, Any
from zoneinfo import ZoneInfo
from config import config

logger = logging.getLogger(__name__)

STATUS_ON = "0"
STATUS_OFF = "1"
STATUS_MAYBE = "10"
KNOWN_STATUSES = frozenset({STATUS_ON, STATUS_OFF, STATUS_MAYBE})

STATUS_LABELS = {
    STATUS_ON: "Світло є",
    STATUS_OFF: "Світла немає (відключення)",
    STATUS_MAYBE: "Можливе відключення",
}

STATUS_EMOJIS = {
    STATUS_ON: "🟢",
    STATUS_OFF: "🔴",
    STATUS_MAYBE: "🟡",
}

_HOMOGLYPH_TRANS = str.maketrans({
    "a": "а",
    "b": "в",
    "e": "е",
    "i": "і",
    "k": "к",
    "m": "м",
    "h": "н",
    "o": "о",
    "p": "р",
    "c": "с",
    "t": "т",
    "x": "х",
})

_TZ_SUFFIX_RE = re.compile(r"(?:Z|[+-]\d{2}:?\d{2})$", re.IGNORECASE)


def _safe_int(val: Any, default: int = 0) -> int:
    try:
        return int(val)
    except (TypeError, ValueError):
        return default


def minutes_to_str(total_minutes: int) -> str:
    if total_minutes >= 24 * 60:
        return "24:00"
    h = total_minutes // 60
    m = total_minutes % 60
    return f"{h:02d}:{m:02d}"


def format_duration(minutes: int) -> str:
    if minutes <= 0:
        return "менше хвилини"
    h = minutes // 60
    m = minutes % 60
    if h > 0 and m > 0:
        return f"{h} год {m} хв"
    if h > 0:
        return f"{h} год"
    return f"{m} хв"


class ScheduleParser:
    @staticmethod
    def select_day_graph(
        schedule_members: Optional[List[Dict[str, Any]]],
        target_date_str: str,
    ) -> Optional[Dict[str, Any]]:
        """
        Selects the most recent graph entry matching target_date_str (YYYY-MM-DD),
        sorting deterministically by (dateCreate, id).
        """
        if not schedule_members:
            return None

        matches = [
            m
            for m in schedule_members
            if isinstance(m, dict) and str(m.get("dateGraph") or "").startswith(target_date_str)
        ]
        if not matches:
            return None

        return max(
            matches,
            key=lambda m: (str(m.get("dateCreate") or ""), _safe_int(m.get("id"), 0)),
        )

    @staticmethod
    def _normalize_times(raw_times: Any) -> Dict[str, str]:
        if not isinstance(raw_times, dict):
            return {}
        normalized: Dict[str, str] = {}
        for k, v in raw_times.items():
            key_str = str(k).strip()
            val_str = STATUS_ON if v is None else str(v).strip()
            normalized[key_str] = val_str
        return normalized

    @staticmethod
    def extract_day_schedule(
        graph_member: Optional[Dict[str, Any]],
        group: Optional[str] = None,
    ) -> Optional[Dict[str, Any]]:
        """
        Extracts date and normalized times mapping for the requested group from a hydra:member entry.
        """
        if not isinstance(graph_member, dict):
            return None

        target_group = (config.CHER_GPV if group is None else group).strip()
        data_json = graph_member.get("dataJson")
        if not isinstance(data_json, dict):
            return None

        group_data = data_json.get(target_group)
        if not isinstance(group_data, dict):
            for k, v in data_json.items():
                if str(k).strip() == target_group and isinstance(v, dict):
                    group_data = v
                    break

        if not isinstance(group_data, dict):
            return None

        raw_times = group_data.get("times")
        if not isinstance(raw_times, dict):
            return None

        times = ScheduleParser._normalize_times(raw_times)
        date_graph = str(graph_member.get("dateGraph") or "")
        date_create = str(graph_member.get("dateCreate") or "")

        return {
            "dateGraph": date_graph,
            "dateCreate": date_create,
            "times": times,
            "group": target_group,
        }

    @staticmethod
    def get_intervals(times: Dict[str, Any]) -> List[Dict[str, Any]]:
        """
        Groups 48 half-hour slots into contiguous status intervals.
        """
        norm_times = ScheduleParser._normalize_times(times)
        slots = []
        for h in range(24):
            for m in (0, 30):
                slot_key = f"{h:02d}:{m:02d}"
                val = norm_times.get(slot_key, STATUS_ON)
                slots.append((slot_key, val))

        intervals = []
        if not slots:
            return intervals

        curr_status = slots[0][1]
        start_idx = 0

        for i in range(1, len(slots)):
            status = slots[i][1]
            if status != curr_status:
                end_minutes = i * 30
                intervals.append({
                    "start": minutes_to_str(start_idx * 30),
                    "end": minutes_to_str(end_minutes),
                    "start_minutes": start_idx * 30,
                    "end_minutes": end_minutes,
                    "status": curr_status,
                    "label": STATUS_LABELS.get(curr_status, "Невідомо"),
                    "emoji": STATUS_EMOJIS.get(curr_status, "⚪"),
                })
                curr_status = status
                start_idx = i

        intervals.append({
            "start": minutes_to_str(start_idx * 30),
            "end": "24:00",
            "start_minutes": start_idx * 30,
            "end_minutes": 24 * 60,
            "status": curr_status,
            "label": STATUS_LABELS.get(curr_status, "Невідомо"),
            "emoji": STATUS_EMOJIS.get(curr_status, "⚪"),
        })

        return intervals

    @staticmethod
    def get_current_status(times: Dict[str, Any], dt: Optional[datetime] = None) -> Dict[str, Any]:
        """
        Determines current power status, active interval, and next change info based on times dict.
        """
        if dt is None:
            tz = ZoneInfo(config.TIMEZONE)
            dt = datetime.now(tz)

        curr_minutes = dt.hour * 60 + dt.minute
        intervals = ScheduleParser.get_intervals(times)

        current_interval = None
        next_interval = None

        for idx, interval in enumerate(intervals):
            if interval["start_minutes"] <= curr_minutes < interval["end_minutes"]:
                current_interval = interval
                if idx + 1 < len(intervals):
                    next_interval = intervals[idx + 1]
                break

        if not current_interval and intervals:
            current_interval = intervals[-1]

        minutes_remaining = 0
        if current_interval:
            minutes_remaining = max(0, current_interval["end_minutes"] - curr_minutes)

        return {
            "current_time": dt.strftime("%H:%M"),
            "current_interval": current_interval,
            "next_interval": next_interval,
            "minutes_remaining": minutes_remaining,
            "status": current_interval["status"] if current_interval else STATUS_ON,
            "label": current_interval["label"] if current_interval else "Світло є",
            "emoji": current_interval["emoji"] if current_interval else "🟢",
        }

    @staticmethod
    def generate_timeline_bar(times: Dict[str, Any]) -> str:
        """
        Generates a visual timeline emoji bar representing 24 hours with 30-min granularity distinction.
        """
        norm_times = ScheduleParser._normalize_times(times)
        bar_chars = []
        for h in range(24):
            val00 = norm_times.get(f"{h:02d}:00", STATUS_ON)
            val30 = norm_times.get(f"{h:02d}:30", STATUS_ON)
            if val00 == STATUS_OFF and val30 == STATUS_OFF:
                bar_chars.append("🟥")
            elif val00 == STATUS_OFF or val30 == STATUS_OFF:
                bar_chars.append("🟧")
            elif val00 == STATUS_MAYBE or val30 == STATUS_MAYBE:
                bar_chars.append("🟨")
            elif val00 == STATUS_ON and val30 == STATUS_ON:
                bar_chars.append("🟩")
            else:
                bar_chars.append("⚪")

        line1 = "".join(bar_chars[:12]) + " (00:00 - 12:00)"
        line2 = "".join(bar_chars[12:]) + " (12:00 - 24:00)"
        return f"{line1}\n{line2}"

    @staticmethod
    def _format_interval_tag(interval: Dict[str, Any], curr_minutes: int) -> str:
        if interval["end_minutes"] <= curr_minutes:
            return " — <i>минуло</i>"
        if interval["start_minutes"] <= curr_minutes < interval["end_minutes"]:
            return " ◀️ <b>зараз</b>"
        return ""

    @staticmethod
    def format_schedule_text(
        day_schedule: Dict[str, Any],
        title_prefix: str = "сьогодні",
        dt: Optional[datetime] = None,
    ) -> str:
        """
        Builds a clean, logical, human-friendly HTML message describing the schedule for a given day.
        """
        times = day_schedule.get("times", {})
        date_graph = day_schedule.get("dateGraph", "")
        group = day_schedule.get("group", config.CHER_GPV)

        formatted_date = ""
        if date_graph:
            try:
                d = datetime.fromisoformat(str(date_graph).replace("Z", "+00:00"))
                formatted_date = d.strftime("%d.%m.%Y")
            except Exception:
                formatted_date = str(date_graph)[:10]

        if dt is None:
            tz = ZoneInfo(config.TIMEZONE)
            dt = datetime.now(tz)

        curr_minutes = dt.hour * 60 + dt.minute
        is_today = "сьогодні" in title_prefix.lower()

        intervals = ScheduleParser.get_intervals(times)
        off_intervals = [i for i in intervals if i["status"] == STATUS_OFF]
        maybe_intervals = [i for i in intervals if i["status"] == STATUS_MAYBE]

        lines = [
            f"📅 <b>Графік на {html.escape(title_prefix)} ({html.escape(formatted_date)})</b>",
            f"📍 {html.escape(config.short_address)} (Черга {html.escape(str(group))})\n",
        ]

        if not off_intervals and not maybe_intervals:
            lines.append("🎉 <b>Відключень не заплановано! Світло буде весь день 🟢</b>\n")
        elif is_today:
            upcoming_off = [i for i in off_intervals if i["end_minutes"] > curr_minutes]
            upcoming_maybe = [i for i in maybe_intervals if i["end_minutes"] > curr_minutes]

            if not upcoming_off and not upcoming_maybe:
                if off_intervals:
                    lines.append("🎉 <b>Усі відключення на сьогодні вже завершилися!</b>")
                    lines.append("Світло подається стабільно до кінця доби 🟢\n")
                    lines.append("<i>Сьогодні були відключення:</i>")
                    for i in off_intervals:
                        dur = format_duration(i["end_minutes"] - i["start_minutes"])
                        lines.append(f"  • {i['start']} — {i['end']} ({dur}) — <i>минуло</i>")
                    lines.append("")
                if maybe_intervals:
                    if not off_intervals:
                        lines.append("🎉 <b>Усі можливі відключення на сьогодні вже минули!</b>")
                        lines.append("Світло подається стабільно до кінця доби 🟢\n")
                        lines.append("<i>Сьогодні були можливі відключення:</i>")
                    else:
                        lines.append("<i>Сьогодні також були можливі відключення:</i>")
                    for i in maybe_intervals:
                        dur = format_duration(i["end_minutes"] - i["start_minutes"])
                        lines.append(f"  • {i['start']} — {i['end']} ({dur}) — <i>минуло</i>")
                    lines.append("")
            else:
                if off_intervals:
                    lines.append("🔴 <b>Години відключень:</b>")
                    for i in off_intervals:
                        dur = format_duration(i["end_minutes"] - i["start_minutes"])
                        tag = ScheduleParser._format_interval_tag(i, curr_minutes)
                        lines.append(f"  • <b>{i['start']} — {i['end']}</b> ({dur}){tag}")
                    lines.append("")

                if maybe_intervals:
                    lines.append("🟡 <b>Можливі відключення (за потреби):</b>")
                    for i in maybe_intervals:
                        dur = format_duration(i["end_minutes"] - i["start_minutes"])
                        tag = ScheduleParser._format_interval_tag(i, curr_minutes)
                        lines.append(f"  • {i['start']} — {i['end']} ({dur}){tag}")
                    lines.append("")
        else:
            if off_intervals:
                lines.append("🔴 <b>Години відключень:</b>")
                for i in off_intervals:
                    dur = format_duration(i["end_minutes"] - i["start_minutes"])
                    lines.append(f"  • <b>{i['start']} — {i['end']}</b> ({dur})")
                lines.append("")

            if maybe_intervals:
                lines.append("🟡 <b>Можливі відключення (за потреби):</b>")
                for i in maybe_intervals:
                    dur = format_duration(i["end_minutes"] - i["start_minutes"])
                    lines.append(f"  • {i['start']} — {i['end']} ({dur})")
                lines.append("")

        lines.append("📊 <b>Шкала доби:</b>")
        lines.append(ScheduleParser.generate_timeline_bar(times))
        lines.append("🟩 є | 🟥 немає | 🟧 30 хв | 🟨 можливе")

        return "\n".join(lines)

    @staticmethod
    def format_status_message(
        today_sched: Optional[Dict[str, Any]],
        tomorrow_sched: Optional[Dict[str, Any]] = None,
        dt: Optional[datetime] = None,
    ) -> str:
        """
        Builds a human, logical real-time status message without robotic clutter.
        """
        if dt is None:
            tz = ZoneInfo(config.TIMEZONE)
            dt = datetime.now(tz)

        curr_time_str = dt.strftime("%H:%M")
        curr_minutes = dt.hour * 60 + dt.minute
        addr_line = f"📍 {html.escape(config.short_address)} (Черга {html.escape(config.CHER_GPV)})"

        if not today_sched:
            formatted_today = dt.strftime("%d.%m.%Y")
            return (
                f"ℹ️ <b>Офіційний графік на сьогодні ({formatted_today}) відсутній на сайті обленерго.</b>\n"
                f"{addr_line}\n"
                f"🕒 <b>Зараз:</b> {curr_time_str}\n\n"
                "💡 За плановим погодинним розкладом даних немає.\n"
                "⚠️ <b>Якщо світла немає зараз, причини:</b>\n"
                "• <b>Аварійне відключення (ГАВ/СГАВ)</b> за командою «Укренерго»\n"
                "• <b>Позапланові ремонтні роботи</b> на лініях електромереж\n"
                "• Затримка публікації погодинного графіка сервером обленерго\n\n"
                "<i>Бот щохвилини перевіряє базу обленерго і сповістить, щойно дані з'являться.</i>"
            )

        times = today_sched.get("times", {})
        status_info = ScheduleParser.get_current_status(times, dt)
        curr_status = status_info["status"]
        curr_interval = status_info["current_interval"]
        next_interval = status_info["next_interval"]
        mins_left = status_info["minutes_remaining"]

        intervals = ScheduleParser.get_intervals(times)
        off_intervals = [i for i in intervals if i["status"] == STATUS_OFF]
        maybe_intervals = [i for i in intervals if i["status"] == STATUS_MAYBE]

        upcoming_off = [i for i in off_intervals if i["start_minutes"] > curr_minutes]
        upcoming_maybe = [i for i in maybe_intervals if i["start_minutes"] > curr_minutes]

        lines = []

        if curr_status == STATUS_OFF:
            end_time = curr_interval["end"] if curr_interval else "кінця інтервалу"
            continues_overnight = False

            if curr_interval and curr_interval["end_minutes"] == 24 * 60 and tomorrow_sched:
                t_intervals = ScheduleParser.get_intervals(tomorrow_sched.get("times", {}))
                if t_intervals and t_intervals[0]["status"] == STATUS_OFF:
                    continues_overnight = True
                    t_first = t_intervals[0]
                    end_time = f"{t_first['end']} (завтра)"
                    mins_left = (24 * 60 - curr_minutes) + t_first["end_minutes"]

            time_left_str = format_duration(mins_left)
            lines.append("🔴 <b>СВІТЛА НЕМАЄ</b>\n")
            lines.append(addr_line)
            lines.append(f"🕒 <b>Зараз:</b> {curr_time_str}\n")
            lines.append(f"💡 <b>Орієнтовне увімкнення:</b> о <b>{end_time}</b>")
            lines.append(f"⏳ Залишилось чекати: <b>{time_left_str}</b>")

            if next_interval and next_interval["status"] == STATUS_MAYBE:
                lines.append(
                    f"\n🟡 Одразу після цього можливе відключення до <b>{next_interval['end']}</b>"
                )

            if upcoming_off:
                next_off = upcoming_off[0]
                lines.append(f"\n⏭ Наступне вимкнення сьогодні: <b>{next_off['start']} — {next_off['end']}</b>")
                if upcoming_maybe and (
                    not next_interval
                    or next_interval["status"] != STATUS_MAYBE
                    or upcoming_maybe[0]["start_minutes"] != next_interval["start_minutes"]
                ):
                    next_m = upcoming_maybe[0]
                    lines.append(f"🟡 Також можливе відключення: <b>{next_m['start']} — {next_m['end']}</b>")
            elif upcoming_maybe:
                if not next_interval or next_interval["status"] != STATUS_MAYBE:
                    next_m = upcoming_maybe[0]
                    lines.append(f"\n🟡 Наступне можливе відключення сьогодні: <b>{next_m['start']} — {next_m['end']}</b>")
            elif not continues_overnight:
                lines.append("\n🎉 <i>Після цього вимкнень на сьогодні більше не заплановано.</i>")

        elif curr_status == STATUS_MAYBE:
            end_time = curr_interval["end"] if curr_interval else "кінця інтервалу"
            time_left_str = format_duration(mins_left)
            lines.append("🟡 <b>МОЖЛИВЕ ВІДКЛЮЧЕННЯ</b>\n")
            lines.append(addr_line)
            lines.append(f"🕒 <b>Зараз:</b> {curr_time_str}\n")
            lines.append(f"⚠️ Зона можливого відключення: <b>до {end_time}</b> (залишилось {time_left_str})")
            lines.append("<i>Світло вимикається лише у разі перевищення лімітів.</i>")

            if upcoming_off:
                next_off = upcoming_off[0]
                wait_off = format_duration(next_off["start_minutes"] - curr_minutes)
                lines.append(f"\n🔴 Точне вимкнення: о <b>{next_off['start']}</b> (через {wait_off})")
            elif upcoming_maybe:
                next_m = upcoming_maybe[0]
                wait_m = format_duration(next_m["start_minutes"] - curr_minutes)
                lines.append(
                    f"\n🟡 Наступне можливе відключення: о <b>{next_m['start']}</b> "
                    f"(через {wait_m}) — до <b>{next_m['end']}</b>"
                )
            else:
                lines.append("\n🎉 <i>Після цього вимкнень на сьогодні більше не заплановано.</i>")

        elif curr_status == STATUS_ON:
            lines.append("🟢 <b>СВІТЛО Є</b>\n")
            lines.append(addr_line)
            lines.append(f"🕒 <b>Зараз:</b> {curr_time_str}\n")

            if upcoming_off and upcoming_maybe and upcoming_maybe[0]["start_minutes"] < upcoming_off[0]["start_minutes"]:
                next_m = upcoming_maybe[0]
                wait_m = format_duration(next_m["start_minutes"] - curr_minutes)
                dur_m = format_duration(next_m["end_minutes"] - next_m["start_minutes"])
                lines.append("🟡 <b>Найближче можливе відключення:</b>")
                lines.append(
                    f"О <b>{next_m['start']}</b> (через {wait_m}) — до <b>{next_m['end']}</b> ({dur_m})"
                )

                next_off = upcoming_off[0]
                wait_off = format_duration(next_off["start_minutes"] - curr_minutes)
                dur_off = format_duration(next_off["end_minutes"] - next_off["start_minutes"])
                lines.append("\n⏳ <b>Наступне гарантоване відключення:</b>")
                lines.append(
                    f"О <b>{next_off['start']}</b> (через {wait_off}) — триватиме до <b>{next_off['end']}</b> ({dur_off})"
                )
                if len(upcoming_off) > 1:
                    later_ranges = ", ".join(f"{i['start']}–{i['end']}" for i in upcoming_off[1:])
                    lines.append(f"<i>Також пізніше сьогодні: {later_ranges}</i>")

            elif upcoming_off:
                next_off = upcoming_off[0]
                wait_time = format_duration(next_off["start_minutes"] - curr_minutes)
                dur = format_duration(next_off["end_minutes"] - next_off["start_minutes"])
                lines.append("⏳ <b>Наступне відключення:</b>")
                lines.append(
                    f"О <b>{next_off['start']}</b> (через {wait_time}) — триватиме до <b>{next_off['end']}</b> ({dur})"
                )

                if len(upcoming_off) > 1:
                    later_ranges = ", ".join(f"{i['start']}–{i['end']}" for i in upcoming_off[1:])
                    lines.append(f"<i>Також пізніше сьогодні: {later_ranges}</i>")
                if upcoming_maybe:
                    later_maybe = ", ".join(f"{i['start']}–{i['end']}" for i in upcoming_maybe)
                    lines.append(f"🟡 <i>Можливі відключення сьогодні: {later_maybe}</i>")

            elif upcoming_maybe:
                next_m = upcoming_maybe[0]
                wait_time = format_duration(next_m["start_minutes"] - curr_minutes)
                dur = format_duration(next_m["end_minutes"] - next_m["start_minutes"])
                lines.append(
                    f"🟡 Наступне можливе відключення: о <b>{next_m['start']}</b> "
                    f"(через {wait_time}) — до <b>{next_m['end']}</b> ({dur})"
                )
            else:
                lines.append("🎉 <b>На сьогодні відключень більше немає!</b>")
                lines.append("До кінця доби світло подаватиметься стабільно 🟢\n")

                if tomorrow_sched:
                    t_times = tomorrow_sched.get("times", {})
                    t_intervals = ScheduleParser.get_intervals(t_times)
                    t_off = [i for i in t_intervals if i["status"] == STATUS_OFF]
                    t_maybe = [i for i in t_intervals if i["status"] == STATUS_MAYBE]

                    if t_off and t_maybe and t_maybe[0]["start_minutes"] < t_off[0]["start_minutes"]:
                        lines.append(
                            f"📅 <i>Завтра перше можливе відключення: о <b>{t_maybe[0]['start']}</b> "
                            f"(до {t_maybe[0]['end']}), точне — о <b>{t_off[0]['start']}</b> (до {t_off[0]['end']})</i>"
                        )
                    elif t_off:
                        first_t = t_off[0]
                        lines.append(
                            f"📅 <i>Завтра перше вимкнення: о <b>{first_t['start']}</b> (до {first_t['end']})</i>"
                        )
                    elif t_maybe:
                        first_m = t_maybe[0]
                        lines.append(
                            f"📅 <i>Завтра можливе відключення: о <b>{first_m['start']}</b> (до {first_m['end']})</i>"
                        )
                    else:
                        lines.append("📅 <i>На завтра відключень також не заплановано 🎉</i>")
                else:
                    if dt.hour < 17:
                        lines.append("📅 <i>Графік на завтра обленерго зазвичай оприлюднює ввечері (після 18:00).</i>")
                    elif dt.hour <= 22:
                        lines.append("📅 <i>Обленерго ще готує графік на завтра. Бот сповістить одразу після публікації.</i>")
                    else:
                        lines.append("📅 <i>Графік на завтра наразі ще не оприлюднено обленерго.</i>")
        else:
            lines.append(f"⚪ <b>НЕВІДОМИЙ СТАТУС ({html.escape(str(curr_status))})</b>\n")
            lines.append(addr_line)
            lines.append(f"🕒 <b>Зараз:</b> {curr_time_str}\n")
            lines.append("ℹ️ Отримано нестандартний код статусу від API обленерго.")

        return "\n".join(lines)

    @staticmethod
    def format_daily_digest(day_schedule: Dict[str, Any]) -> str:
        """
        Builds a compact morning overview for the daily digest.
        """
        times = day_schedule.get("times", {})
        date_graph = day_schedule.get("dateGraph", "")
        formatted_date = ""
        if date_graph:
            try:
                d = datetime.fromisoformat(str(date_graph).replace("Z", "+00:00"))
                formatted_date = d.strftime("%d.%m.%Y")
            except Exception:
                formatted_date = str(date_graph)[:10]

        intervals = ScheduleParser.get_intervals(times)
        off_intervals = [i for i in intervals if i["status"] == STATUS_OFF]
        maybe_intervals = [i for i in intervals if i["status"] == STATUS_MAYBE]

        lines = [
            f"☕️ <b>Доброго ранку! Графік на сьогодні ({html.escape(formatted_date)})</b>",
            f"📍 {html.escape(config.short_address)} (Черга {html.escape(config.CHER_GPV)})\n",
        ]

        if not off_intervals and not maybe_intervals:
            lines.append("🎉 <b>Сьогодні вимкнень не заплановано!</b> Світло буде весь день 🟢")
        else:
            if off_intervals:
                lines.append("🔴 <b>Години відключень:</b>")
                for i in off_intervals:
                    dur_str = format_duration(i["end_minutes"] - i["start_minutes"])
                    lines.append(f"  • <b>{i['start']} — {i['end']}</b> ({dur_str})")
                lines.append("")

            if maybe_intervals:
                lines.append("🟡 <b>Можливі відключення:</b>")
                for i in maybe_intervals:
                    dur_str = format_duration(i["end_minutes"] - i["start_minutes"])
                    lines.append(f"  • {i['start']} — {i['end']} ({dur_str})")
                lines.append("")

            lines.append("🟢 <i>У решту годин світло подається безперебійно. Гарного дня!</i>")

        return "\n".join(lines)

    @staticmethod
    def _normalize_building_token(token: str) -> str:
        cleaned = re.sub(r"\s+", "", token.strip().lower())
        return cleaned.translate(_HOMOGLYPH_TRANS)

    @staticmethod
    def filter_accidents_for_building(
        records: Optional[List[Dict[str, Any]]],
        building_name: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        if not records:
            return []

        target_raw = config.BUILDING_NAME if building_name is None else building_name
        norm_target = ScheduleParser._normalize_building_token(target_raw)
        if not norm_target:
            return [r for r in records if isinstance(r, dict)]

        filtered: List[Dict[str, Any]] = []
        for rec in records:
            if not isinstance(rec, dict):
                continue
            raw_buildings = rec.get("buildingNames")
            if raw_buildings is None or not str(raw_buildings).strip():
                filtered.append(rec)
                continue

            tokens = [
                ScheduleParser._normalize_building_token(part)
                for part in re.split(r"[,;]+", str(raw_buildings))
                if part.strip()
            ]
            if norm_target in tokens:
                filtered.append(rec)

        return filtered

    @staticmethod
    def format_toe_datetime(raw: Optional[str]) -> str:
        if raw is None:
            return "уточнюється"
        s = str(raw).strip()
        if not s:
            return "уточнюється"
        naive = _TZ_SUFFIX_RE.sub("", s)
        try:
            dt = datetime.fromisoformat(naive)
            return dt.strftime("%d.%m.%Y %H:%M")
        except ValueError:
            return s

    @staticmethod
    def format_accident_line(record: Dict[str, Any], is_planned: bool = False) -> str:
        raw_desc = record.get("koment") or record.get("reason") or ""
        desc = re.sub(r"\s+", " ", str(raw_desc)).strip()
        if not desc:
            desc = "Планові ремонтні роботи" if is_planned else "Аварійне відключення"
        if len(desc) > 250:
            desc = desc[:247].rstrip() + "..."
        safe_desc = html.escape(desc)

        raw_start = record.get("dateEvent") or record.get("dateStart")
        raw_end = record.get("datePlanIn") or record.get("dateEnd")
        end_str = html.escape(ScheduleParser.format_toe_datetime(raw_end))

        if raw_start is not None and str(raw_start).strip():
            start_str = html.escape(ScheduleParser.format_toe_datetime(raw_start))
            return f"• {safe_desc} ({start_str} — {end_str})"
        return f"• {safe_desc} (до {end_str})"
