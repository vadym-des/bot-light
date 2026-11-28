import asyncio
import os
import tempfile
import unittest
from datetime import datetime
from unittest.mock import AsyncMock, MagicMock, patch
from zoneinfo import ZoneInfo

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

import notifier as notifier_module
from database import Database
from notifier import NotifierService
from schedule_parser import (
    ScheduleParser,
    STATUS_MAYBE,
    STATUS_OFF,
    STATUS_ON,
)
from station_monitor import StationMonitor

KYIV_TZ = ZoneInfo("Europe/Kyiv")


def _make_times(overrides=None, default=STATUS_ON):
    times = {}
    for h in range(24):
        for m in (0, 30):
            times[f"{h:02d}:{m:02d}"] = default
    if overrides:
        times.update(overrides)
    return times


def _make_day_sched(date_str="2026-11-28", overrides=None, default=STATUS_ON):
    return {
        "dateGraph": f"{date_str}T00:00:00Z",
        "times": _make_times(overrides=overrides, default=default),
    }


class TestStationMonitorLogic(unittest.TestCase):
    def test_initial_sync_debounce_flicker_and_ttl(self):
        now = [100.0]
        monitor = StationMonitor(ttl_seconds=180, debounce_seconds=20, clock_fn=lambda: now[0])

        # Initially stale/empty
        self.assertFalse(monitor.is_fresh())
        self.assertIsNone(monitor.get_live_grid())

        # Initial sync establishes baseline without emitting transition
        self.assertIsNone(monitor.record_update("present"))
        self.assertEqual(monitor.confirmed_grid, "present")
        self.assertEqual(monitor.get_live_grid(), "present")

        # Transition starts pending
        now[0] = 110.0
        self.assertIsNone(monitor.record_update("absent"))
        self.assertEqual(monitor.pending_grid, "absent")
        self.assertEqual(monitor.confirmed_grid, "present")

        # Flicker back to present cancels pending transition
        now[0] = 115.0
        self.assertIsNone(monitor.record_update("present"))
        self.assertIsNone(monitor.pending_grid)
        self.assertEqual(monitor.confirmed_grid, "present")

        # Start transition again and verify debounce threshold
        now[0] = 120.0
        self.assertIsNone(monitor.record_update("absent"))
        now[0] = 139.0
        self.assertIsNone(monitor.check_pending_transition())
        now[0] = 140.0
        self.assertEqual(monitor.check_pending_transition(), "absent")
        self.assertEqual(monitor.confirmed_grid, "absent")
        self.assertIsNone(monitor.pending_grid)

        # Unknown cancels pending without confirming
        now[0] = 150.0
        self.assertIsNone(monitor.record_update("present"))
        self.assertEqual(monitor.pending_grid, "present")
        now[0] = 155.0
        self.assertIsNone(monitor.record_update("unknown"))
        self.assertIsNone(monitor.pending_grid)
        self.assertIsNone(monitor.get_live_grid())

        # Freshness TTL expiry falls back to None
        now[0] = 200.0
        monitor.record_update("present")
        self.assertEqual(monitor.get_live_grid(), "present")
        now[0] = 381.0
        self.assertFalse(monitor.is_fresh())
        self.assertIsNone(monitor.get_live_grid())


class TestStationHttpAndBroadcast(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.tmpdir.name, "test_station.db")
        self.test_db = Database(self.db_path)
        await self.test_db.init()

        await self.test_db.add_subscriber(1001, username="user1", first_name="User 1")
        await self.test_db.add_subscriber(1002, username="user2", first_name="User 2")
        await self.test_db.add_subscriber(1003, username="stopped", first_name="Stopped")
        await self.test_db.update_setting(1003, "is_active", 0)
        await self.test_db.update_setting(1002, "notify_outage", 0)

        self.bot = MagicMock()
        self.bot.send_message = AsyncMock()
        self.bot.edit_message_text = AsyncMock()

        self.api_client = MagicMock()
        self.api_client.get_schedule = AsyncMock(return_value=None)

    async def asyncTearDown(self):
        self.tmpdir.cleanup()

    async def test_http_endpoint_auth_validation_and_broadcast(self):
        token_box = {"token": ""}
        monitor = StationMonitor(ttl_seconds=180, debounce_seconds=0)
        notifier = NotifierService(
            bot=self.bot,
            api_client=self.api_client,
            station_monitor=monitor,
        )

        app = web.Application()
        app.router.add_post(
            "/api/station-state",
            monitor.create_http_handler(notifier, token_getter=lambda: token_box["token"]),
        )

        async with TestClient(TestServer(app)) as client:
            # 1. Empty token -> 503 disabled
            resp = await client.post("/api/station-state", json={"grid": "present"})
            self.assertEqual(resp.status, 503)

            token_box["token"] = "super-secret-token"

            # 2. Missing / wrong token -> 401
            resp = await client.post("/api/station-state", json={"grid": "present"})
            self.assertEqual(resp.status, 401)
            resp = await client.post(
                "/api/station-state",
                json={"grid": "present"},
                headers={"Authorization": "Bearer wrong"},
            )
            self.assertEqual(resp.status, 401)

            # 3. Invalid JSON or invalid grid -> 400
            resp = await client.post(
                "/api/station-state",
                data="not-json",
                headers={"Authorization": "Bearer super-secret-token"},
            )
            self.assertEqual(resp.status, 400)
            resp = await client.post(
                "/api/station-state",
                json={"grid": "invalid"},
                headers={"Authorization": "Bearer super-secret-token"},
            )
            self.assertEqual(resp.status, 400)

            # 4. Initial present sync -> 200, no broadcast
            with patch.object(notifier_module, "db", self.test_db):
                resp = await client.post(
                    "/api/station-state",
                    json={"grid": "present"},
                    headers={"Authorization": "Bearer super-secret-token"},
                )
                self.assertEqual(resp.status, 200)
                self.assertEqual(self.bot.send_message.await_count, 0)

                # 5. Transition to absent -> broadcasts outage to active subscribers with notify_outage=1
                resp = await client.post(
                    "/api/station-state",
                    json={"grid": "absent"},
                    headers={"Authorization": "Bearer super-secret-token"},
                )
                self.assertEqual(resp.status, 200)
                self.assertEqual(self.bot.send_message.await_count, 1)
                sent_kwargs = self.bot.send_message.await_args.kwargs
                self.assertEqual(sent_kwargs["chat_id"], 1001)
                self.assertIn("Світло зникло", sent_kwargs["text"])

                # 6. Transition back to present -> broadcasts restore to all active subscribers with notify_restore=1
                self.bot.send_message.reset_mock()
                resp = await client.post(
                    "/api/station-state",
                    json={"grid": "present"},
                    headers={"Authorization": "Bearer super-secret-token"},
                )
                self.assertEqual(resp.status, 200)
                sent_chats = {call.kwargs["chat_id"] for call in self.bot.send_message.await_args_list}
                self.assertEqual(sent_chats, {1001, 1002})
                for call in self.bot.send_message.await_args_list:
                    self.assertIn("Світло є", call.kwargs["text"])

        monitor.stop()

    async def test_http_endpoint_delayed_debounce_broadcast(self):
        monitor = StationMonitor(ttl_seconds=180, debounce_seconds=0)
        monitor.debounce_seconds = 1  # exercise async _delayed_check path cleanly

        notifier = NotifierService(
            bot=self.bot,
            api_client=self.api_client,
            station_monitor=monitor,
        )
        app = web.Application()
        app.router.add_post(
            "/api/station-state",
            monitor.create_http_handler(notifier, token_getter=lambda: "tok"),
        )

        async with TestClient(TestServer(app)) as client:
            with patch.object(notifier_module, "db", self.test_db):
                await client.post(
                    "/api/station-state",
                    json={"grid": "present"},
                    headers={"Authorization": "Bearer tok"},
                )
                resp = await client.post(
                    "/api/station-state",
                    json={"grid": "absent"},
                    headers={"Authorization": "Bearer tok"},
                )
                self.assertEqual(resp.status, 200)
                self.assertEqual(self.bot.send_message.await_count, 0)
                await asyncio.sleep(1.2)
                self.assertEqual(self.bot.send_message.await_count, 1)
                self.assertIn("Світло зникло", self.bot.send_message.await_args.kwargs["text"])

        monitor.stop()

    async def test_station_1line_context_single_source_of_truth_and_past_schedule_filter(self):
        import hashlib
        import json as json_mod

        times = _make_times(
            {
                "10:00": STATUS_MAYBE,
                "10:30": STATUS_OFF,
                "11:00": STATUS_OFF,
                "11:30": STATUS_OFF,
                "12:00": STATUS_OFF,
                "12:30": STATUS_OFF,
                "13:00": STATUS_MAYBE,
                "18:00": STATUS_OFF,
                "18:30": STATUS_OFF,
            }
        )
        graph = {
            "id": 1,
            "dateGraph": "2026-11-28T00:00:00Z",
            "dateCreate": "2026-11-28T08:00:00Z",
            "dataJson": {"2.1": {"times": times}},
        }
        self.api_client.get_schedule = AsyncMock(return_value=[graph])
        self.api_client.get_accidents = AsyncMock(return_value=[])
        self.api_client.get_planed_accidents = AsyncMock(return_value=[])

        monitor = StationMonitor(ttl_seconds=180, debounce_seconds=0)
        notifier = NotifierService(
            bot=self.bot,
            api_client=self.api_client,
            station_monitor=monitor,
        )

        with patch.object(notifier_module, "db", self.test_db):
            # 1. Absent earlier than schedule (10:15, OFF starts at 10:30..13:00)
            await notifier.handle_station_transition(
                "absent", now=datetime(2026, 11, 28, 10, 15, tzinfo=KYIV_TZ)
            )
            self.assertEqual(
                self.bot.send_message.await_args.kwargs["text"],
                "🔴 <b>Світло зникло — раніше графіка (до 13:00)</b>",
            )

            # 2. Absent during scheduled OFF (11:00)
            self.bot.send_message.reset_mock()
            await notifier.handle_station_transition(
                "absent", now=datetime(2026, 11, 28, 11, 0, tzinfo=KYIV_TZ)
            )
            self.assertEqual(
                self.bot.send_message.await_args.kwargs["text"],
                "🔴 <b>Світло зникло — до 13:00 (за графіком)</b>",
            )

            # 3. Absent outside schedule (15:00, next OFF at 18:00 > 60 min)
            self.bot.send_message.reset_mock()
            await notifier.handle_station_transition(
                "absent", now=datetime(2026, 11, 28, 15, 0, tzinfo=KYIV_TZ)
            )
            self.assertEqual(
                self.bot.send_message.await_args.kwargs["text"],
                "🔴 <b>Світло зникло — поза графіком</b>",
            )

            # 4. Present at 13:02 -> next outage at 18:00
            self.bot.send_message.reset_mock()
            await notifier.handle_station_transition(
                "present", now=datetime(2026, 11, 28, 13, 2, tzinfo=KYIV_TZ)
            )
            for call in self.bot.send_message.await_args_list:
                self.assertEqual(
                    call.kwargs["text"],
                    "🟢 <b>Світло є — наступне вимкнення о 18:00</b>",
                )

            # 5. Present at 19:00 -> no more outages today
            self.bot.send_message.reset_mock()
            await notifier.handle_station_transition(
                "present", now=datetime(2026, 11, 28, 19, 0, tzinfo=KYIV_TZ)
            )
            for call in self.bot.send_message.await_args_list:
                self.assertEqual(
                    call.kwargs["text"],
                    "🟢 <b>Світло є — сьогодні більше без вимкнень</b>",
                )

            # 6. Seed schedule state, then test single-source-of-truth + warning suppression + past-only edit
            await self.test_db.set_state(
                "schedule_hash_2026-11-28",
                hashlib.md5(json_mod.dumps(times, sort_keys=True).encode()).hexdigest(),
            )
            await self.test_db.set_state("schedule_times_2026-11-28", json_mod.dumps(times, sort_keys=True))

            # Station reports absent at 10:15 -> 15-min warning for 10:30 OFF must be suppressed,
            # AND scheduled transition at 10:30 must be suppressed because station is live.
            self.bot.send_message.reset_mock()
            monitor.record_update("absent", now_dt=datetime(2026, 11, 28, 10, 15, tzinfo=KYIV_TZ))
            await notifier.check_for_updates(now=datetime(2026, 11, 28, 10, 15, tzinfo=KYIV_TZ))
            await notifier.check_for_updates(now=datetime(2026, 11, 28, 10, 30, tzinfo=KYIV_TZ))
            self.bot.send_message.assert_not_called()

            # Past-only schedule edit at 14:00 (09:00 changed from ON to OFF) -> must NOT broadcast
            past_edited = dict(times)
            past_edited["09:00"] = STATUS_OFF
            self.api_client.get_schedule = AsyncMock(
                return_value=[
                    {
                        "id": 2,
                        "dateGraph": "2026-11-28T00:00:00Z",
                        "dateCreate": "2026-11-28T14:00:00Z",
                        "dataJson": {"2.1": {"times": past_edited}},
                    }
                ]
            )
            await notifier.check_for_updates(now=datetime(2026, 11, 28, 14, 0, tzinfo=KYIV_TZ))
            self.bot.send_message.assert_not_called()

            # Future schedule edit at 14:05 (16:00 changed to OFF) -> MUST broadcast
            future_edited = dict(past_edited)
            future_edited["16:00"] = STATUS_OFF
            self.api_client.get_schedule = AsyncMock(
                return_value=[
                    {
                        "id": 3,
                        "dateGraph": "2026-11-28T00:00:00Z",
                        "dateCreate": "2026-11-28T14:05:00Z",
                        "dataJson": {"2.1": {"times": future_edited}},
                    }
                ]
            )
            await notifier.check_for_updates(now=datetime(2026, 11, 28, 14, 5, tzinfo=KYIV_TZ))
            self.assertEqual(self.bot.send_message.await_count, 2)
            self.assertIn("Графік на сьогодні", self.bot.send_message.await_args.kwargs["text"])

        monitor.stop()


class TestScheduleParserPhysicalGrid(unittest.TestCase):
    def test_format_status_message_with_physical_grid(self):
        dt = datetime(2026, 11, 28, 12, 15, tzinfo=KYIV_TZ)

        # Without schedule
        no_sched_present = ScheduleParser.format_status_message(None, None, dt, physical_grid="present")
        self.assertTrue(no_sched_present.startswith("🟢 <b>СВІТЛО Є (факт зі станції)</b>"))
        no_sched_absent = ScheduleParser.format_status_message(None, None, dt, physical_grid="absent")
        self.assertTrue(no_sched_absent.startswith("🔴 <b>СВІТЛА НЕМАЄ (факт зі станції)</b>"))

        # Schedule says ON, station agrees -> confirmed
        sched_on = _make_day_sched(default=STATUS_ON)
        confirmed_on = ScheduleParser.format_status_message(sched_on, None, dt, physical_grid="present")
        self.assertTrue(confirmed_on.startswith("🟢 <b>СВІТЛО Є (підтверджено станцією)</b>"))

        # Schedule says ON, station says absent -> override headline + note
        override_off = ScheduleParser.format_status_message(sched_on, None, dt, physical_grid="absent")
        self.assertTrue(override_off.startswith("🔴 <b>СВІТЛА НЕМАЄ (факт зі станції)</b>"))
        self.assertIn("немає 220V (попри графік)", override_off)

        # Schedule says OFF, station says present -> override headline + note
        sched_off = _make_day_sched(overrides={"12:00": STATUS_OFF, "12:30": STATUS_OFF}, default=STATUS_ON)
        override_on = ScheduleParser.format_status_message(sched_off, None, dt, physical_grid="present")
        self.assertTrue(override_on.startswith("🟢 <b>СВІТЛО Є (факт зі станції)</b>"))
        self.assertIn("є 220V (попри графік)", override_on)

        # Schedule says OFF, station agrees -> confirmed
        confirmed_off = ScheduleParser.format_status_message(sched_off, None, dt, physical_grid="absent")
        self.assertTrue(confirmed_off.startswith("🔴 <b>СВІТЛА НЕМАЄ (підтверджено станцією)</b>"))

        # Schedule says MAYBE, station says present -> fact
        sched_maybe = _make_day_sched(overrides={"12:00": STATUS_MAYBE, "12:30": STATUS_MAYBE}, default=STATUS_ON)
        maybe_present = ScheduleParser.format_status_message(sched_maybe, None, dt, physical_grid="present")
        self.assertTrue(maybe_present.startswith("🟢 <b>СВІТЛО Є (факт зі станції)</b>"))
        self.assertIn("є 220V (попри графік)", maybe_present)

        # physical_grid=None preserves standard output
        standard_on = ScheduleParser.format_status_message(sched_on, None, dt, physical_grid=None)
        self.assertTrue(standard_on.startswith("🟢 <b>СВІТЛО Є</b>"))
