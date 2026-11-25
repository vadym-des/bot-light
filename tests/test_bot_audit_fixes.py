import asyncio
import os
import tempfile
import unittest
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
from zoneinfo import ZoneInfo

from aiohttp import web
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError

import handlers
import keyboards
import main as main_module
import notifier as notifier_module
from api_client import ToeApiClient
from config import Config, config
from database import Database, BOOLEAN_SETTING_KEYS
from notifier import NotifierService
from schedule_parser import (
    ScheduleParser,
    STATUS_ON,
    STATUS_OFF,
    STATUS_MAYBE,
)

KYIV_TZ = ZoneInfo("Europe/Kyiv")


def make_times(overrides=None, default=STATUS_ON):
    times = {}
    for h in range(24):
        for m in (0, 30):
            times[f"{h:02d}:{m:02d}"] = default
    if overrides:
        times.update(overrides)
    return times


def make_graph(date_str, times, group="2.1", date_create="2026-11-23T08:00:00Z", graph_id=1):
    return {
        "id": graph_id,
        "dateGraph": f"{date_str}T00:00:00Z",
        "dateCreate": date_create,
        "dataJson": {group: {"times": times}},
    }


class TestConfig(unittest.TestCase):
    def test_empty_and_invalid_env_vars_do_not_crash(self):
        with patch.dict(
            os.environ,
            {
                "DEFAULT_CHAT_ID": "",
                "CHECK_INTERVAL_SECONDS": "5",
                "PORT": "   ",
                "OTG_ID": "abc",
                "RENDER_EXTERNAL_URL": "",
                "BOT_USERNAME": "@my_test_bot",
            },
            clear=False,
        ):
            cfg = Config()
            self.assertEqual(cfg.DEFAULT_CHAT_ID, 0)
            self.assertEqual(cfg.CHECK_INTERVAL_SECONDS, 15)
            self.assertEqual(cfg.PORT, 10000)
            self.assertEqual(cfg.OTG_ID, 17)
            self.assertEqual(cfg.RENDER_EXTERNAL_URL, "")
            self.assertEqual(cfg.BOT_USERNAME, "my_test_bot")

    def test_street_formatting_preserves_vul_prefix_bugs(self):
        with patch.dict(os.environ, {"STREET_NAME": "Вулична", "BUILDING_NAME": "5"}, clear=False):
            cfg = Config()
            self.assertEqual(cfg.clean_street, "Вулична")
            self.assertEqual(cfg.formatted_street, "вул. Вулична")
            self.assertEqual(cfg.short_address, "вул. Вулична, 5")

        with patch.dict(os.environ, {"STREET_NAME": "просп. Степана Бандери", "BUILDING_NAME": "12"}, clear=False):
            cfg = Config()
            self.assertEqual(cfg.formatted_street, "просп. Степана Бандери")
            self.assertEqual(cfg.short_address, "просп. Степана Бандери, 12")

        with patch.dict(os.environ, {"STREET_NAME": "вулиця Героїв Майдану", "BUILDING_NAME": "2-А"}, clear=False):
            cfg = Config()
            self.assertEqual(cfg.clean_street, "Героїв Майдану")
            self.assertEqual(cfg.formatted_street, "вул. Героїв Майдану")


class TestDatabase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.tmpdir.name, "test.db")
        self.test_db = Database(self.db_path)
        await self.test_db.init()

    async def asyncTearDown(self):
        self.tmpdir.cleanup()

    async def test_wal_and_default_subscriber_not_overwritten(self):
        async with self.test_db._connect() as conn:
            async with conn.execute("PRAGMA journal_mode;") as cur:
                mode = (await cur.fetchone())[0]
        self.assertEqual(mode.lower(), "wal")

        await self.test_db.add_subscriber(999, username="owner_user", first_name="Vadym")
        await self.test_db.update_setting(999, "is_active", 0)
        await self.test_db.ensure_default_subscriber(999, first_name="Owner")

        sub = await self.test_db.get_subscriber(999)
        self.assertEqual(sub["username"], "owner_user")
        self.assertEqual(sub["first_name"], "Vadym")
        self.assertEqual(sub["is_active"], 0)

    async def test_ensure_subscriber_does_not_reactivate_stopped_user(self):
        await self.test_db.ensure_subscriber(101, username="alice", first_name="Alice")
        await self.test_db.update_setting(101, "is_active", 0)
        await self.test_db.ensure_subscriber(101, username="alice", first_name="Alice")

        sub = await self.test_db.get_subscriber(101)
        self.assertEqual(sub["is_active"], 0)

    async def test_add_subscriber_coalesce_and_reactivate_false(self):
        await self.test_db.add_subscriber(202, username="bob", first_name="Bob")
        await self.test_db.update_setting(202, "is_active", 0)
        await self.test_db.add_subscriber(202, username=None, first_name=None, reactivate=False)

        sub = await self.test_db.get_subscriber(202)
        self.assertEqual(sub["username"], "bob")
        self.assertEqual(sub["first_name"], "Bob")
        self.assertEqual(sub["is_active"], 0)

    async def test_prefix_lookup_escapes_underscore_and_cleanup_keeps_protected(self):
        await self.test_db.set_state("warn_2026-11-23_1_10:00", "1")
        await self.test_db.set_state("warnX2026-11-23_1_10:00", "1")
        matches = await self.test_db.get_states_by_prefix("warn_2026-11-23_")
        self.assertEqual(list(matches.keys()), ["warn_2026-11-23_1_10:00"])

        await self.test_db.set_state("last_status", STATUS_OFF)
        await self.test_db.set_state("old_temp_key", "val")
        async with self.test_db._connect() as conn:
            await conn.execute(
                "UPDATE state SET updated_at = datetime('now', '-10 days') "
                "WHERE key IN ('last_status', 'old_temp_key');"
            )
            await conn.commit()

        deleted = await self.test_db.cleanup_old_state(keep_days=7)
        self.assertEqual(deleted, 1)
        self.assertEqual(await self.test_db.get_state("last_status"), STATUS_OFF)
        self.assertIsNone(await self.test_db.get_state("old_temp_key"))

    async def test_update_setting_rejects_invalid_key(self):
        with self.assertRaises(ValueError):
            await self.test_db.update_setting(1, "invalid_col", 1)


class _FakeAcquireCtx:
    def __init__(self, conn):
        self._conn = conn

    async def __aenter__(self):
        return self._conn

    async def __aexit__(self, exc_type, exc, tb):
        return False


class TestPostgresDatabase(unittest.IsolatedAsyncioTestCase):
    def test_schema_validation_and_sqlite_isolation_when_config_has_url(self):
        for bad_schema in ("", "1bad", "public; DROP TABLE x", "bot-light"):
            with self.assertRaises(ValueError):
                Database(database_url="postgresql://u:p@host:6543/postgres", schema=bad_schema)

        with patch.object(config, "DATABASE_URL", "postgresql://u:p@aws-0.pooler.supabase.com:6543/postgres"):
            with tempfile.TemporaryDirectory() as tmpdir:
                local_db = Database(os.path.join(tmpdir, "local.db"))
                self.assertFalse(local_db.is_postgres)

            default_db = Database()
            self.assertTrue(default_db.is_postgres)

    def test_build_pg_connect_kwargs(self):
        dsn, kwargs = Database._build_pg_connect_kwargs(
            "postgresql+asyncpg://user:pass@aws-0-eu-central-1.pooler.supabase.com:6543/postgres"
        )
        self.assertTrue(dsn.startswith("postgresql://"))
        self.assertEqual(kwargs["statement_cache_size"], 0)
        self.assertEqual(kwargs["max_size"], 3)
        self.assertEqual(kwargs["min_size"], 1)
        self.assertEqual(kwargs["command_timeout"], 15)
        self.assertEqual(kwargs["ssl"], "require")

        dsn_disable, kwargs_disable = Database._build_pg_connect_kwargs(
            "postgresql://user:pass@aws-0-eu-central-1.pooler.supabase.com:6543/postgres?sslmode=disable"
        )
        self.assertIn("sslmode=disable", dsn_disable)
        self.assertNotIn("ssl", kwargs_disable)

        _, kwargs_local = Database._build_pg_connect_kwargs(
            "postgresql://user:pass@localhost:5432/postgres"
        )
        self.assertNotIn("ssl", kwargs_local)

    async def test_postgres_init_and_crud_operations(self):
        executed_sql = []
        fetch_calls = []

        conn = MagicMock()

        async def fake_execute(sql, *args):
            executed_sql.append((sql, args))
            if "REVOKE ALL ON SCHEMA" in sql:
                raise RuntimeError("role anon does not exist")
            if sql.strip().startswith("DELETE FROM bot_light.state"):
                return "DELETE 4"
            return "OK"

        async def fake_fetch(sql, *args):
            fetch_calls.append((sql, args))
            if "information_schema.columns" in sql:
                return [
                    {"column_name": "chat_id"},
                    {"column_name": "quiet_night"},
                    {"column_name": "daily_digest"},
                    {"column_name": "pinned_message_id"},
                    {"column_name": "last_status_message_id"},
                ]
            if "FROM bot_light.subscribers WHERE is_active = 1" in sql:
                return [{"chat_id": 123456789012, "is_active": 1, "first_name": "Vadym"}]
            if "SELECT key, value FROM bot_light.state WHERE key LIKE" in sql:
                return [{"key": "warn_2026-11-23_1_10:00", "value": "1"}]
            return []

        async def fake_fetchrow(sql, *args):
            fetch_calls.append((sql, args))
            if "FROM bot_light.subscribers WHERE chat_id = $1" in sql:
                return {"chat_id": args[0], "is_active": 1, "notify_warning": 15}
            return None

        async def fake_fetchval(sql, *args):
            fetch_calls.append((sql, args))
            if "daily_digest_default_migrated" in sql:
                return None
            if "SELECT value FROM bot_light.state WHERE key = $1" in sql:
                return "STATUS_VAL"
            return None

        conn.execute = AsyncMock(side_effect=fake_execute)
        conn.fetch = AsyncMock(side_effect=fake_fetch)
        conn.fetchrow = AsyncMock(side_effect=fake_fetchrow)
        conn.fetchval = AsyncMock(side_effect=fake_fetchval)

        fake_pool = MagicMock()
        fake_pool.acquire = MagicMock(return_value=_FakeAcquireCtx(conn))
        fake_pool.close = AsyncMock()

        pg_db = Database(
            database_url="postgresql://postgres.proj:secret@aws-0-eu-central-1.pooler.supabase.com:6543/postgres",
            schema="bot_light",
        )

        with patch("asyncpg.create_pool", new=AsyncMock(return_value=fake_pool)) as mock_create_pool:
            with patch.object(config, "DEFAULT_CHAT_ID", 0):
                await pg_db.init()

            mock_create_pool.assert_awaited_once()
            _, call_kwargs = mock_create_pool.await_args
            self.assertEqual(call_kwargs["statement_cache_size"], 0)
            self.assertEqual(call_kwargs["max_size"], 3)
            self.assertEqual(call_kwargs["ssl"], "require")

            all_init_sql = "\n".join(sql for sql, _ in executed_sql)
            self.assertIn("CREATE SCHEMA IF NOT EXISTS bot_light;", all_init_sql)
            self.assertIn("REVOKE ALL ON SCHEMA bot_light FROM anon, authenticated;", all_init_sql)
            self.assertIn("chat_id BIGINT PRIMARY KEY", all_init_sql)
            self.assertIn("CREATE TABLE IF NOT EXISTS bot_light.state", all_init_sql)

            await pg_db.ensure_default_subscriber(123456789012)
            await pg_db.ensure_subscriber(123456789012, username="u", first_name="Vadym")
            await pg_db.ensure_subscriber(123456789012, username="u", first_name="Vadym")  # cached skip
            await pg_db.add_subscriber(123456789012, username="u", first_name="Vadym", reactivate=True)
            await pg_db.add_subscriber(123456789012, username=None, first_name=None, reactivate=False)

            sub = await pg_db.get_subscriber(123456789012)
            self.assertEqual(sub["chat_id"], 123456789012)
            active = await pg_db.get_active_subscribers()
            self.assertEqual(len(active), 1)

            await pg_db.update_setting(123456789012, "is_active", 0)
            self.assertNotIn(123456789012, pg_db._seen_chats)

            await pg_db.set_state("k1", "v1")
            self.assertEqual(await pg_db.get_state("k1"), "STATUS_VAL")
            prefixed = await pg_db.get_states_by_prefix("warn_2026-11-23_")
            self.assertEqual(prefixed, {"warn_2026-11-23_1_10:00": "1"})

            deleted = await pg_db.cleanup_old_state(keep_days=7)
            self.assertEqual(deleted, 4)

            await pg_db.close()
            fake_pool.close.assert_awaited_once()
            self.assertIsNone(pg_db._pool)


class TestApiClient(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.calls = {"sched": 0, "acc": 0}
        self.mode = {"sched": "ok", "acc": "ok"}
        self.last_query = {}

        async def handle_sched(request):
            self.calls["sched"] += 1
            self.last_query = dict(request.query)
            if self.mode["sched"] == "500":
                return web.Response(status=500, text="Server Error")
            if self.mode["sched"] == "flaky" and self.calls["sched"] == 1:
                return web.Response(status=503, text="Retry me")
            if self.mode["sched"] == "empty":
                return web.json_response({"hydra:member": []})
            return web.json_response({"hydra:member": [{"id": 1, "dateGraph": "2026-11-23T00:00:00Z"}]})

        async def handle_acc(request):
            self.calls["acc"] += 1
            if self.mode["acc"] == "404":
                return web.Response(status=404, text="Not Found")
            return web.json_response([])

        app = web.Application()
        app.router.add_get("/a_gpv_g", handle_sched)
        app.router.add_get("/pw_accidents", handle_acc)
        app.router.add_get("/pw_accident_planeds", handle_acc)

        self.runner = web.AppRunner(app)
        await self.runner.setup()
        self.site = web.TCPSite(self.runner, "127.0.0.1", 0)
        await self.site.start()
        port = self.site._server.sockets[0].getsockname()[1]
        self.client = ToeApiClient(base_url=f"http://127.0.0.1:{port}", cache_ttl=45.0)

    async def asyncTearDown(self):
        await self.client.close()
        await self.runner.cleanup()

    async def test_error_returns_none_and_not_cached(self):
        self.mode["sched"] = "500"
        res = await self.client.get_schedule()
        self.assertIsNone(res)
        self.assertEqual(self.calls["sched"], 2)  # initial + 1 retry

        self.mode["sched"] = "empty"
        res2 = await self.client.get_schedule()
        self.assertEqual(res2, [])
        self.assertTrue(self.last_query.get("before", "").endswith("T23:59:59+00:00"))

    async def test_retry_on_transient_503_and_ttl_cache(self):
        self.mode["sched"] = "flaky"
        res1 = await self.client.get_schedule()
        self.assertIsNotNone(res1)
        self.assertEqual(len(res1), 1)
        self.assertEqual(self.calls["sched"], 2)

        # Second call hits cache
        res2 = await self.client.get_schedule()
        self.assertEqual(len(res2), 1)
        self.assertEqual(self.calls["sched"], 2)

        # force_refresh bypasses cache
        res3 = await self.client.get_schedule(force_refresh=True)
        self.assertEqual(len(res3), 1)
        self.assertEqual(self.calls["sched"], 3)

    async def test_accidents_404_returns_none_vs_200_empty(self):
        self.mode["acc"] = "404"
        self.assertIsNone(await self.client.get_accidents())
        self.mode["acc"] = "ok"
        self.assertEqual(await self.client.get_accidents(force_refresh=True), [])


class TestScheduleParser(unittest.TestCase):
    def test_select_day_graph_picks_latest(self):
        members = [
            make_graph("2026-11-23", make_times({"08:00": STATUS_OFF}), date_create="2026-11-23T06:00:00Z", graph_id=10),
            make_graph("2026-11-23", make_times({"10:00": STATUS_OFF}), date_create="2026-11-23T09:00:00Z", graph_id=11),
        ]
        chosen = ScheduleParser.select_day_graph(members, "2026-11-23")
        self.assertEqual(chosen["id"], 11)

    def test_timeline_distinguishes_half_hour_outage(self):
        times = make_times({"08:00": 1, "08:30": 0, "09:00": 1, "09:30": 1, "10:00": 10})
        bar = ScheduleParser.generate_timeline_bar(times)
        first_line = bar.splitlines()[0]
        self.assertEqual(first_line[8], "🟧")
        self.assertEqual(first_line[9], "🟥")
        self.assertEqual(first_line[10], "🟨")
        self.assertEqual(first_line[0], "🟩")

    def test_format_schedule_text_no_empty_off_header_and_no_contradiction(self):
        # Only maybe intervals, no off intervals
        sched_only_maybe = {
            "dateGraph": "2026-11-23T00:00:00Z",
            "times": make_times({"18:00": STATUS_MAYBE, "18:30": STATUS_MAYBE}),
            "group": "2.1",
        }
        dt = datetime(2026, 11, 23, 12, 0, tzinfo=KYIV_TZ)
        txt = ScheduleParser.format_schedule_text(sched_only_maybe, "сьогодні", dt=dt)
        self.assertNotIn("Години відключень", txt)
        self.assertIn("Можливі відключення", txt)

        # Past off interval + upcoming maybe interval
        sched_mixed = {
            "dateGraph": "2026-11-23T00:00:00Z",
            "times": make_times({"08:00": STATUS_OFF, "18:00": STATUS_MAYBE}),
            "group": "2.1",
        }
        txt_mixed = ScheduleParser.format_schedule_text(sched_mixed, "сьогодні", dt=dt)
        self.assertNotIn("Світло подається стабільно до кінця доби", txt_mixed)
        self.assertIn("18:00 — 18:30", txt_mixed)

    def test_status_message_overnight_outage_earlier_maybe_and_unknown_status(self):
        today_sched = {
            "dateGraph": "2026-11-23T00:00:00Z",
            "times": make_times({"22:00": STATUS_OFF, "22:30": STATUS_OFF, "23:00": STATUS_OFF, "23:30": STATUS_OFF}),
            "group": "2.1",
        }
        tomorrow_sched = {
            "dateGraph": "2026-11-24T00:00:00Z",
            "times": make_times({"00:00": STATUS_OFF, "00:30": STATUS_OFF, "01:00": STATUS_OFF, "01:30": STATUS_OFF}),
            "group": "2.1",
        }
        dt_night = datetime(2026, 11, 23, 23, 0, tzinfo=KYIV_TZ)
        msg_night = ScheduleParser.format_status_message(today_sched, tomorrow_sched, dt=dt_night)
        self.assertIn("02:00 (завтра)", msg_night)
        self.assertIn("3 год", msg_night)

        # Earlier maybe + later off
        sched_early_maybe = {
            "dateGraph": "2026-11-23T00:00:00Z",
            "times": make_times({"11:00": STATUS_MAYBE, "18:00": STATUS_OFF}),
            "group": "2.1",
        }
        dt_morning = datetime(2026, 11, 23, 10, 0, tzinfo=KYIV_TZ)
        msg_early = ScheduleParser.format_status_message(sched_early_maybe, None, dt=dt_morning)
        self.assertIn("11:00", msg_early)
        self.assertIn("18:00", msg_early)

        # Unknown status not misreported as green
        sched_unknown = {
            "dateGraph": "2026-11-23T00:00:00Z",
            "times": make_times({"10:00": "99", "10:30": "99"}),
            "group": "2.1",
        }
        msg_unk = ScheduleParser.format_status_message(sched_unknown, None, dt=dt_morning)
        self.assertIn("НЕВІДОМИЙ СТАТУС (99)", msg_unk)
        self.assertNotIn("СВІТЛО Є", msg_unk)

        # Tomorrow has maybe only -> do not say no outages tomorrow
        sched_on_all = {"dateGraph": "2026-11-23T00:00:00Z", "times": make_times(), "group": "2.1"}
        sched_tmrw_maybe = {
            "dateGraph": "2026-11-24T00:00:00Z",
            "times": make_times({"14:00": STATUS_MAYBE}),
            "group": "2.1",
        }
        msg_tmrw = ScheduleParser.format_status_message(sched_on_all, sched_tmrw_maybe, dt=dt_morning)
        self.assertIn("14:00", msg_tmrw)
        self.assertNotIn("На завтра відключень також не заплановано", msg_tmrw)

    def test_building_filter_and_real_toe_accident_formatting(self):
        records = [
            {"id": 1, "buildingNames": "2, 20, 2-Б", "koment": "Інший будинок"},
            {"id": 2, "buildingNames": "1, 2-A, 3", "koment": "Аварія <КП>\r\nРемонт", "dateEvent": "2026-11-23T08:00:00+00:00", "datePlanIn": "2026-11-23T17:00:00+00:00"},
            {"id": 3, "buildingNames": "", "koment": "Вся вулиця", "dateEvent": "2026-11-23T09:00:00Z", "datePlanIn": None},
        ]
        filtered = ScheduleParser.filter_accidents_for_building(records, "2-А")
        self.assertEqual([r["id"] for r in filtered], [2, 3])

        line2 = ScheduleParser.format_accident_line(filtered[0])
        self.assertIn("Аварія &lt;КП&gt; Ремонт", line2)
        self.assertIn("23.11.2026 08:00 — 23.11.2026 17:00", line2)

        line3 = ScheduleParser.format_accident_line(filtered[1])
        self.assertIn("23.11.2026 09:00 — уточнюється", line3)


class TestNotifier(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.tmpdir.name, "notifier.db")
        self.test_db = Database(self.db_path)
        await self.test_db.init()
        await self.test_db.add_subscriber(1001, username="u1", first_name="User1")
        await self.test_db.update_setting(1001, "daily_digest", 1)

        self.db_patcher_notifier = patch.object(notifier_module, "db", self.test_db)
        self.db_patcher_notifier.start()

        self.bot = MagicMock()
        self.bot.send_message = AsyncMock()
        self.bot.edit_message_text = AsyncMock()

        self.api = MagicMock()
        self.api.get_schedule = AsyncMock(return_value=[])
        self.api.get_accidents = AsyncMock(return_value=[])
        self.api.get_planed_accidents = AsyncMock(return_value=[])

        self.service = NotifierService(bot=self.bot, api_client=self.api, bot_username="test_bot")

    async def asyncTearDown(self):
        self.db_patcher_notifier.stop()
        self.tmpdir.cleanup()

    async def test_api_failure_does_not_overwrite_pinned_message(self):
        await self.test_db.update_setting(1001, "pinned_message_id", 555)
        self.api.get_schedule = AsyncMock(return_value=None)
        await self.service.check_for_updates(now=datetime(2026, 11, 23, 12, 0, tzinfo=KYIV_TZ))
        self.bot.edit_message_text.assert_not_called()

    async def test_tomorrow_schedule_announced_when_today_missing_and_no_midnight_dupe(self):
        tmrw_times = make_times({"10:00": STATUS_OFF, "10:30": STATUS_OFF})
        self.api.get_schedule = AsyncMock(
            return_value=[make_graph("2026-11-24", tmrw_times)]
        )
        # 2026-11-23 20:00 (today missing, tomorrow published)
        await self.service.check_for_updates(now=datetime(2026, 11, 23, 20, 0, tzinfo=KYIV_TZ))
        self.assertEqual(self.bot.send_message.await_count, 1)
        first_msg = self.bot.send_message.await_args.kwargs["text"]
        self.assertIn("завтра", first_msg)

        # At 2026-11-24 00:00, same schedule must NOT re-announce as today's schedule
        self.bot.send_message.reset_mock()
        await self.service.check_for_updates(now=datetime(2026, 11, 24, 0, 0, tzinfo=KYIV_TZ))
        self.bot.send_message.assert_not_called()

    async def test_midnight_transitions_and_warning_before_0000(self):
        day1_times = make_times({"22:00": STATUS_OFF, "22:30": STATUS_OFF, "23:00": STATUS_OFF, "23:30": STATUS_OFF})
        day2_times_on = make_times()
        self.api.get_schedule = AsyncMock(
            return_value=[
                make_graph("2026-11-23", day1_times),
                make_graph("2026-11-24", day2_times_on),
            ]
        )
        await self.test_db.set_state(
            "schedule_hash_2026-11-23",
            notifier_module.hashlib.md5(notifier_module.json.dumps(day1_times, sort_keys=True).encode()).hexdigest(),
        )
        await self.test_db.set_state(
            "schedule_hash_2026-11-24",
            notifier_module.hashlib.md5(notifier_module.json.dumps(day2_times_on, sort_keys=True).encode()).hexdigest(),
        )

        # 23:30 -> inside outage, sends 1 outage alert with remaining 30 min (not 2 hours!)
        await self.service.check_for_updates(now=datetime(2026, 11, 23, 23, 30, tzinfo=KYIV_TZ))
        self.assertEqual(self.bot.send_message.await_count, 1)
        outage_msg = self.bot.send_message.await_args.kwargs["text"]
        self.assertIn("Світло вимкнено", outage_msg)
        self.assertIn("(на 30 хв)", outage_msg)

        # 00:00 -> restored!
        self.bot.send_message.reset_mock()
        await self.service.check_for_updates(now=datetime(2026, 11, 24, 0, 0, tzinfo=KYIV_TZ))
        self.assertEqual(self.bot.send_message.await_count, 1)
        restore_msg = self.bot.send_message.await_args.kwargs["text"]
        self.assertIn("Світло увімкнено!", restore_msg)

    async def test_continuous_midnight_outage_no_double_alert_and_warning_at_2345(self):
        day1_on = make_times()
        day2_off_at_0 = make_times({"00:00": STATUS_OFF, "00:30": STATUS_OFF})
        self.api.get_schedule = AsyncMock(
            return_value=[
                make_graph("2026-11-23", day1_on),
                make_graph("2026-11-24", day2_off_at_0),
            ]
        )
        await self.test_db.set_state(
            "schedule_hash_2026-11-23",
            notifier_module.hashlib.md5(notifier_module.json.dumps(day1_on, sort_keys=True).encode()).hexdigest(),
        )
        await self.test_db.set_state(
            "schedule_hash_2026-11-24",
            notifier_module.hashlib.md5(notifier_module.json.dumps(day2_off_at_0, sort_keys=True).encode()).hexdigest(),
        )

        # 23:45 -> 15 min warning for 00:00 outage tomorrow
        await self.service.check_for_updates(now=datetime(2026, 11, 23, 23, 45, tzinfo=KYIV_TZ))
        self.assertEqual(self.bot.send_message.await_count, 1)
        warn_msg = self.bot.send_message.await_args.kwargs["text"]
        self.assertIn("Через 15 хв відключення", warn_msg)
        self.assertIn("00:00", warn_msg)

        # Now test continuous 23:00-24:00 + 00:00-01:00 OFF does not double alert at 00:00
        day1_off_end = make_times({"23:00": STATUS_OFF, "23:30": STATUS_OFF})
        self.api.get_schedule = AsyncMock(
            return_value=[
                make_graph("2026-11-23", day1_off_end),
                make_graph("2026-11-24", day2_off_at_0),
            ]
        )
        await self.test_db.set_state(
            "schedule_hash_2026-11-23",
            notifier_module.hashlib.md5(notifier_module.json.dumps(day1_off_end, sort_keys=True).encode()).hexdigest(),
        )
        await self.test_db.set_state("last_status", STATUS_OFF)
        self.bot.send_message.reset_mock()
        await self.service.check_for_updates(now=datetime(2026, 11, 24, 0, 0, tzinfo=KYIV_TZ))
        self.bot.send_message.assert_not_called()

    async def test_no_warning_during_outage_and_maybe_transition_copy(self):
        times = make_times({"10:00": STATUS_OFF, "10:30": STATUS_OFF, "11:00": STATUS_MAYBE})
        self.api.get_schedule = AsyncMock(return_value=[make_graph("2026-11-23", times)])
        await self.test_db.set_state(
            "schedule_hash_2026-11-23",
            notifier_module.hashlib.md5(notifier_module.json.dumps(times, sort_keys=True).encode()).hexdigest(),
        )
        await self.test_db.set_state("last_status", STATUS_OFF)

        # At 10:50 user is in STATUS_OFF; 11:00 is STATUS_MAYBE -> must NOT send charging warning!
        await self.service.check_for_updates(now=datetime(2026, 11, 23, 10, 50, tzinfo=KYIV_TZ))
        self.bot.send_message.assert_not_called()

        # At 11:30 user moved from STATUS_MAYBE to STATUS_ON
        await self.test_db.set_state("last_status", STATUS_MAYBE)
        await self.service.check_for_updates(now=datetime(2026, 11, 23, 11, 30, tzinfo=KYIV_TZ))
        self.assertEqual(self.bot.send_message.await_count, 1)
        msg = self.bot.send_message.await_args.kwargs["text"]
        self.assertIn("Період можливого відключення завершився", msg)
        self.assertNotIn("Електропостачання відновлено", msg)

    async def test_morning_digest_and_stale_pin_cleanup(self):
        await self.test_db.update_setting(1001, "pinned_message_id", 777)
        self.bot.edit_message_text = AsyncMock(
            side_effect=TelegramBadRequest(method=MagicMock(), message="Bad Request: message to edit not found")
        )
        times = make_times({"14:00": STATUS_OFF})
        self.api.get_schedule = AsyncMock(return_value=[make_graph("2026-11-23", times)])
        await self.test_db.set_state(
            "schedule_hash_2026-11-23",
            notifier_module.hashlib.md5(notifier_module.json.dumps(times, sort_keys=True).encode()).hexdigest(),
        )

        # 08:15 (within widened 08:00-08:30 window)
        await self.service.check_for_updates(now=datetime(2026, 11, 23, 8, 15, tzinfo=KYIV_TZ))
        sub = await self.test_db.get_subscriber(1001)
        self.assertIsNone(sub["pinned_message_id"])
        self.assertEqual(self.bot.send_message.await_count, 1)
        self.assertIn("Доброго ранку!", self.bot.send_message.await_args.kwargs["text"])

    async def test_accidents_and_planned_building_filter_and_no_midnight_dupe(self):
        self.api.get_schedule = AsyncMock(return_value=[])
        self.api.get_accidents = AsyncMock(
            return_value=[
                {
                    "id": 10,
                    "buildingNames": "2, 20",
                    "koment": "Не наш будинок",
                    "dateEvent": "2026-11-23T22:00:00+00:00",
                    "datePlanIn": "2026-11-24T03:00:00+00:00",
                },
                {
                    "id": 11,
                    "buildingNames": "1, 2-А, 3",
                    "koment": "Обрив проводу",
                    "dateEvent": "2026-11-23T23:00:00+00:00",
                    "datePlanIn": "2026-11-24T04:00:00+00:00",
                },
            ]
        )
        self.api.get_planed_accidents = AsyncMock(
            return_value=[
                {
                    "id": 20,
                    "buildingNames": "2-A",
                    "koment": "Планове обслуговування",
                    "dateEvent": "2026-11-24T09:00:00+00:00",
                    "datePlanIn": "2026-11-24T15:00:00+00:00",
                }
            ]
        )

        await self.service.check_for_updates(now=datetime(2026, 11, 23, 23, 30, tzinfo=KYIV_TZ))
        self.assertEqual(self.bot.send_message.await_count, 2)
        sent_texts = [call.kwargs["text"] for call in self.bot.send_message.await_args_list]
        self.assertTrue(any("Обрив проводу" in t for t in sent_texts))
        self.assertTrue(any("Планове обслуговування" in t for t in sent_texts))
        self.assertFalse(any("Не наш будинок" in t for t in sent_texts))

        # After midnight, identical active accidents/repairs must NOT duplicate
        self.bot.send_message.reset_mock()
        await self.service.check_for_updates(now=datetime(2026, 11, 24, 0, 1, tzinfo=KYIV_TZ))
        self.bot.send_message.assert_not_called()

    async def test_accident_dedup_ignores_ephemeral_id_building_names_and_empty_flicker(self):
        self.api.get_schedule = AsyncMock(return_value=[])
        base_rec = {
            "id": 115803819,
            "buildingNames": "1, 2-А, 3",
            "koment": "Обрив проводу",
            "dateEvent": "2026-11-25T15:48:16+00:00",
            "datePlanIn": "2026-11-25T20:00:00+00:00",
        }
        # Poll 1: initial accident -> 1 alert
        self.api.get_accidents = AsyncMock(return_value=[base_rec])
        await self.service.check_for_updates(now=datetime(2026, 11, 25, 17, 0, tzinfo=KYIV_TZ))
        self.assertEqual(self.bot.send_message.await_count, 1)

        # Poll 2: TOE resync assigns new ephemeral id and expands buildingNames -> 0 alerts
        self.bot.send_message.reset_mock()
        self.api.get_accidents = AsyncMock(
            return_value=[{**base_rec, "id": 115810815, "buildingNames": "1, 2-А, 3, 99"}]
        )
        await self.service.check_for_updates(now=datetime(2026, 11, 25, 17, 3, tzinfo=KYIV_TZ))
        self.bot.send_message.assert_not_called()

        # Poll 3: temporary empty [] during TOE table truncate -> 0 alerts
        self.api.get_accidents = AsyncMock(return_value=[])
        await self.service.check_for_updates(now=datetime(2026, 11, 25, 17, 4, tzinfo=KYIV_TZ))
        self.bot.send_message.assert_not_called()

        # Poll 4: same record returns with yet another id -> still 0 alerts
        self.api.get_accidents = AsyncMock(
            return_value=[{**base_rec, "id": 115820000}]
        )
        await self.service.check_for_updates(now=datetime(2026, 11, 25, 17, 5, tzinfo=KYIV_TZ))
        self.bot.send_message.assert_not_called()

        # Poll 5: old record + brand new accident -> alert contains ONLY the new accident
        self.api.get_accidents = AsyncMock(
            return_value=[
                {**base_rec, "id": 115820001},
                {
                    "id": 115820002,
                    "buildingNames": "2-А",
                    "koment": "Аварія на ТП",
                    "dateEvent": "2026-11-25T17:06:00+00:00",
                    "datePlanIn": "2026-11-25T21:00:00+00:00",
                },
            ]
        )
        await self.service.check_for_updates(now=datetime(2026, 11, 25, 17, 6, tzinfo=KYIV_TZ))
        self.assertEqual(self.bot.send_message.await_count, 1)
        alert_text = self.bot.send_message.await_args.kwargs["text"]
        self.assertIn("Аварія на ТП", alert_text)
        self.assertNotIn("Обрив проводу", alert_text)

    async def test_gpv_in_pw_accidents_not_broadcast_as_emergency_but_gav_is(self):
        self.api.get_schedule = AsyncMock(return_value=[])
        self.api.get_accidents = AsyncMock(
            return_value=[
                {
                    "id": 115803819,
                    "buildingNames": "1, 2-А, 3",
                    "koment": "Застосування ГПВ",
                    "dateEvent": "2026-11-25T15:48:16+00:00",
                    "datePlanIn": "2026-11-25T20:00:00+00:00",
                }
            ]
        )
        await self.service.check_for_updates(now=datetime(2026, 11, 25, 17, 0, tzinfo=KYIV_TZ))
        self.bot.send_message.assert_not_called()

        # Emergency ГАВ MUST still broadcast
        self.api.get_accidents = AsyncMock(
            return_value=[
                {
                    "id": 115803820,
                    "buildingNames": "1, 2-А, 3",
                    "koment": "Застосування ГАВ",
                    "dateEvent": "2026-11-25T17:10:00+00:00",
                    "datePlanIn": "2026-11-25T20:00:00+00:00",
                }
            ]
        )
        await self.service.check_for_updates(now=datetime(2026, 11, 25, 17, 10, tzinfo=KYIV_TZ))
        self.assertEqual(self.bot.send_message.await_count, 1)
        self.assertIn("Застосування ГАВ", self.bot.send_message.await_args.kwargs["text"])

    async def test_schedule_change_broadcast_does_not_duplicate_address_or_header(self):
        tmrw_times = make_times({"10:00": STATUS_OFF, "10:30": STATUS_OFF})
        self.api.get_schedule = AsyncMock(
            return_value=[make_graph("2026-11-26", tmrw_times)]
        )
        await self.service.check_for_updates(now=datetime(2026, 11, 25, 19, 0, tzinfo=KYIV_TZ))
        self.assertEqual(self.bot.send_message.await_count, 1)
        msg = self.bot.send_message.await_args.kwargs["text"]
        self.assertEqual(msg.count(config.short_address), 1)
        self.assertIn("26.11.2026", msg)


class TestHandlersAndKeyboards(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.tmpdir.name, "handlers.db")
        self.test_db = Database(self.db_path)
        await self.test_db.init()
        self.db_patcher = patch.object(handlers, "db", self.test_db)
        self.db_patcher.start()
        keyboards.set_bot_username("light_test_bot")

    async def asyncTearDown(self):
        keyboards.set_bot_username("")
        self.db_patcher.stop()
        self.tmpdir.cleanup()

    def test_keyboards_pinned_vs_regular_and_schedule(self):
        kb_pinned = keyboards.get_status_inline_keyboard(is_pinned=True)
        callbacks_pinned = [btn.callback_data for row in kb_pinned.inline_keyboard for btn in row if btn.callback_data]
        self.assertIn("refresh_pinned_status", callbacks_pinned)
        self.assertNotIn("pin_live_status", callbacks_pinned)

        kb_sched = keyboards.get_schedule_inline_keyboard("tomorrow")
        callbacks_sched = [btn.callback_data for row in kb_sched.inline_keyboard for btn in row if btn.callback_data]
        self.assertIn("refresh_tomorrow", callbacks_sched)
        self.assertIn("btn_status", callbacks_sched)
        self.assertIn("btn_today", callbacks_sched)

        keyboards.set_bot_username("")
        kb_no_share = keyboards.get_status_inline_keyboard(is_pinned=False)
        urls = [btn.url for row in kb_no_share.inline_keyboard for btn in row if btn.url]
        self.assertEqual(urls, [])

    async def test_autosubscribe_middleware_group_vs_private(self):
        mw = handlers.AutoSubscribeMiddleware()
        next_handler = AsyncMock(return_value="ok")

        # Group message must subscribe chat.id (-100500), NOT user.id (777)
        group_chat = SimpleNamespace(id=-100500, type="supergroup", username=None, title="Будинок 2-А")
        sender = SimpleNamespace(id=777, is_bot=False, username="sender", first_name="Sender")
        await mw(next_handler, MagicMock(), {"event_chat": group_chat, "event_from_user": sender})

        self.assertIsNotNone(await self.test_db.get_subscriber(-100500))
        self.assertIsNone(await self.test_db.get_subscriber(777))

    async def test_start_escapes_html_and_stop_deactivates(self):
        api = MagicMock()
        api.get_schedule = AsyncMock(return_value=[])

        msg = MagicMock()
        msg.chat = SimpleNamespace(id=444, type="private", username="u444", title=None)
        msg.from_user = SimpleNamespace(id=444, is_bot=False, username="u444", first_name="Alex <3 & Co")
        msg.bot = MagicMock()
        msg.bot.delete_message = AsyncMock()
        sent_status = SimpleNamespace(message_id=88)
        msg.answer = AsyncMock(side_effect=[SimpleNamespace(message_id=87), sent_status])

        await handlers.cmd_start(msg, api_client=api)
        greeting_text = msg.answer.await_args_list[0].args[0]
        self.assertIn("Alex &lt;3 &amp; Co", greeting_text)

        stop_msg = MagicMock()
        stop_msg.chat = msg.chat
        stop_msg.from_user = msg.from_user
        stop_msg.answer = AsyncMock()
        await handlers.cmd_stop(stop_msg)
        sub = await self.test_db.get_subscriber(444)
        self.assertEqual(sub["is_active"], 0)

    async def test_toggle_whitelist_and_pin_cleanup_on_error(self):
        await self.test_db.add_subscriber(555, username="u555", first_name="U555")
        call = MagicMock()
        call.data = "toggle_is_active"
        call.message = MagicMock()
        call.message.chat = SimpleNamespace(id=555)
        call.answer = AsyncMock()

        await handlers.cb_toggle_setting(call)
        sub = await self.test_db.get_subscriber(555)
        self.assertEqual(sub["is_active"], 1)  # NOT toggled via toggle_ prefix!
        self.assertIn("Невідоме", call.answer.await_args.args[0])

        # Valid toggle
        self.assertIn("notify_outage", BOOLEAN_SETTING_KEYS)
        call.data = "toggle_notify_outage"
        call.message.edit_reply_markup = AsyncMock()
        await handlers.cb_toggle_setting(call)
        sub = await self.test_db.get_subscriber(555)
        self.assertEqual(sub["notify_outage"], 0)

        # Pin failure deletes sent message
        api = MagicMock()
        api.get_schedule = AsyncMock(return_value=[])
        pin_call = MagicMock()
        pin_call.data = "pin_live_status"
        pin_call.message = MagicMock()
        pin_call.message.chat = SimpleNamespace(id=555)
        sent_widget = MagicMock()
        sent_widget.message_id = 999
        sent_widget.delete = AsyncMock()
        pin_call.message.answer = AsyncMock(return_value=sent_widget)
        pin_call.bot = MagicMock()
        pin_call.bot.pin_chat_message = AsyncMock(side_effect=RuntimeError("No rights"))
        pin_call.answer = AsyncMock()

        await handlers.cb_pin_live_status(pin_call, api_client=api)
        sent_widget.delete.assert_awaited_once()
        sub = await self.test_db.get_subscriber(555)
        self.assertIsNone(sub["pinned_message_id"])

    async def test_accidents_handler_reports_api_failure_honestly(self):
        api = MagicMock()
        api.get_accidents = AsyncMock(return_value=None)
        api.get_planed_accidents = AsyncMock(return_value=None)

        msg = MagicMock()
        msg.answer = AsyncMock()
        await handlers.handle_accidents(msg, api_client=api)
        text = msg.answer.await_args.args[0]
        self.assertIn("Не вдалося перевірити", text)
        self.assertNotIn("штатному режимі", text)

    async def test_accidents_handler_separates_gpv_from_emergency(self):
        api = MagicMock()
        api.get_accidents = AsyncMock(
            return_value=[
                {
                    "id": 115803819,
                    "buildingNames": "1, 2-А, 3",
                    "koment": "Застосування ГПВ",
                    "dateEvent": "2026-11-25T15:48:16+00:00",
                    "datePlanIn": "2026-11-25T20:00:00+00:00",
                }
            ]
        )
        api.get_planed_accidents = AsyncMock(return_value=[])

        msg = MagicMock()
        msg.answer = AsyncMock()
        await handlers.handle_accidents(msg, api_client=api)
        text = msg.answer.await_args.args[0]
        self.assertIn("Аварійних або планових ремонтних робіт за вашою адресою не зафіксовано", text)
        self.assertIn("погодинне відключення (ГПВ)", text)
        self.assertNotIn("Аварійні відключення:", text)

    async def test_pause_toggle_and_refresh_callbacks(self):
        await self.test_db.add_subscriber(666, username="u666", first_name="U666")
        call = MagicMock()
        call.data = "toggle_subscription_pause"
        call.message = MagicMock()
        call.message.chat = SimpleNamespace(id=666)
        call.message.edit_reply_markup = AsyncMock()
        call.answer = AsyncMock()

        await handlers.cb_toggle_subscription_pause(call)
        sub = await self.test_db.get_subscriber(666)
        self.assertEqual(sub["is_active"], 0)

        api = MagicMock()
        api.get_schedule = AsyncMock(return_value=[])
        pinned_call = MagicMock()
        pinned_call.data = "refresh_pinned_status"
        pinned_call.message = MagicMock()
        pinned_call.message.edit_text = AsyncMock()
        pinned_call.answer = AsyncMock()
        await handlers.cb_refresh_pinned_status(pinned_call, api_client=api)
        edited_text = pinned_call.message.edit_text.await_args.args[0]
        self.assertTrue(edited_text.startswith(handlers.PINNED_HEADER))

        tmrw_call = MagicMock()
        tmrw_call.data = "refresh_tomorrow"
        tmrw_call.message = MagicMock()
        tmrw_call.message.edit_text = AsyncMock()
        tmrw_call.answer = AsyncMock()
        await handlers.cb_refresh_tomorrow(tmrw_call, api_client=api)
        tmrw_text = tmrw_call.message.edit_text.await_args.args[0]
        self.assertIn("Графік на завтра", tmrw_text)

    def test_main_has_no_api_info_endpoint(self):
        import inspect
        src = inspect.getsource(main_module)
        self.assertNotIn("/api/info", src)
        self.assertIn("/health", src)


if __name__ == "__main__":
    unittest.main()
