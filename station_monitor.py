import asyncio
import hmac
import logging
import time
from datetime import datetime
from typing import Callable, Optional
from zoneinfo import ZoneInfo

from aiohttp import web

from config import config

logger = logging.getLogger(__name__)

VALID_GRID_STATES = frozenset({"present", "absent", "unknown"})


class StationMonitor:
    """
    Tracks physical 220V grid state reported by AllpowersBar with TTL freshness
    and transition debouncing to avoid spam on brief flickers.
    """

    def __init__(
        self,
        ttl_seconds: int = 180,
        debounce_seconds: int = 20,
        clock_fn: Callable[[], float] = time.monotonic,
    ):
        self.ttl_seconds = max(1, int(ttl_seconds))
        self.debounce_seconds = max(0, int(debounce_seconds))
        self._clock = clock_fn

        self.raw_grid: Optional[str] = None
        self.last_seen_mono: Optional[float] = None
        self.last_seen_at: Optional[datetime] = None

        self.confirmed_grid: Optional[str] = None
        self.pending_grid: Optional[str] = None
        self.pending_since_mono: Optional[float] = None
        self._debounce_task: Optional[asyncio.Task] = None

    def is_fresh(self, now_mono: Optional[float] = None) -> bool:
        if self.last_seen_mono is None:
            return False
        now = self._clock() if now_mono is None else now_mono
        return (now - self.last_seen_mono) <= self.ttl_seconds

    def get_live_grid(self, now_mono: Optional[float] = None) -> Optional[str]:
        if not self.is_fresh(now_mono):
            return None
        if self.raw_grid in ("present", "absent"):
            return self.raw_grid
        return None

    def record_update(
        self,
        grid: str,
        now_mono: Optional[float] = None,
        now_dt: Optional[datetime] = None,
    ) -> Optional[str]:
        if grid not in VALID_GRID_STATES:
            raise ValueError(f"Invalid grid state: {grid!r}")

        now = self._clock() if now_mono is None else now_mono
        if now_dt is None:
            now_dt = datetime.now(ZoneInfo(config.TIMEZONE))

        self.raw_grid = grid
        self.last_seen_mono = now
        self.last_seen_at = now_dt

        if grid == "unknown":
            self.pending_grid = None
            self.pending_since_mono = None
            return None

        # Initial sync / restart: capture baseline without broadcasting
        if self.confirmed_grid is None:
            self.confirmed_grid = grid
            self.pending_grid = None
            self.pending_since_mono = None
            return None

        if grid == self.confirmed_grid:
            self.pending_grid = None
            self.pending_since_mono = None
            return None

        if self.debounce_seconds <= 0:
            self.confirmed_grid = grid
            self.pending_grid = None
            self.pending_since_mono = None
            return grid

        if self.pending_grid != grid:
            self.pending_grid = grid
            self.pending_since_mono = now
            return None

        if self.pending_since_mono is not None and (now - self.pending_since_mono) >= self.debounce_seconds:
            self.confirmed_grid = grid
            self.pending_grid = None
            self.pending_since_mono = None
            return grid

        return None

    def check_pending_transition(self, now_mono: Optional[float] = None) -> Optional[str]:
        if self.pending_grid is None or self.pending_since_mono is None:
            return None
        now = self._clock() if now_mono is None else now_mono
        if not self.is_fresh(now):
            return None
        if self.raw_grid != self.pending_grid:
            return None
        if (now - self.pending_since_mono) >= self.debounce_seconds:
            self.confirmed_grid = self.pending_grid
            self.pending_grid = None
            self.pending_since_mono = None
            return self.confirmed_grid
        return None

    def stop(self) -> None:
        if self._debounce_task and not self._debounce_task.done():
            self._debounce_task.cancel()
        self._debounce_task = None

    def create_http_handler(
        self,
        notifier_service,
        token_getter: Optional[Callable[[], str]] = None,
    ):
        get_token = token_getter or (lambda: config.STATION_API_TOKEN)

        async def _delayed_check() -> None:
            try:
                await asyncio.sleep(max(0.05, float(self.debounce_seconds)))
                confirmed = self.check_pending_transition()
                if confirmed:
                    await notifier_service.handle_station_transition(confirmed)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.error(f"Error in station debounce transition task: {e}", exc_info=True)

        async def handle_station_state(request: web.Request) -> web.Response:
            expected_token = (get_token() or "").strip()
            if not expected_token:
                return web.json_response({"ok": False, "error": "disabled"}, status=503)

            auth_header = request.headers.get("Authorization", "")
            if not auth_header.startswith("Bearer "):
                return web.json_response({"ok": False, "error": "unauthorized"}, status=401)

            provided_token = auth_header[7:].strip()
            if not provided_token or not hmac.compare_digest(provided_token, expected_token):
                return web.json_response({"ok": False, "error": "unauthorized"}, status=401)

            try:
                payload = await request.json()
            except Exception:
                return web.json_response({"ok": False, "error": "invalid_json"}, status=400)

            if not isinstance(payload, dict):
                return web.json_response({"ok": False, "error": "invalid_payload"}, status=400)

            grid = payload.get("grid")
            if not isinstance(grid, str) or grid not in VALID_GRID_STATES:
                return web.json_response({"ok": False, "error": "invalid_grid"}, status=400)

            transition = self.record_update(grid)

            if transition:
                if self._debounce_task and not self._debounce_task.done():
                    self._debounce_task.cancel()
                self._debounce_task = None
                try:
                    await notifier_service.handle_station_transition(transition)
                except Exception as e:
                    logger.error(f"Error handling immediate station transition: {e}", exc_info=True)
            elif self.pending_grid is not None:
                if self._debounce_task is None or self._debounce_task.done():
                    self._debounce_task = asyncio.create_task(_delayed_check())
            else:
                if self._debounce_task and not self._debounce_task.done():
                    self._debounce_task.cancel()
                self._debounce_task = None

            return web.json_response(
                {
                    "ok": True,
                    "grid": grid,
                    "confirmed": self.confirmed_grid,
                    "pending": self.pending_grid,
                },
                status=200,
            )

        return handle_station_state
