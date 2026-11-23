import asyncio
import base64
import logging
import time
from datetime import datetime, timedelta
from typing import Optional, Dict, Any, List, Tuple
from zoneinfo import ZoneInfo
import aiohttp
from config import config

logger = logging.getLogger(__name__)

BASE_URL = "https://api-poweron.toe.com.ua/api"


class ToeApiClient:
    def __init__(self, base_url: str = BASE_URL, cache_ttl: float = 45.0):
        self.base_url = base_url.rstrip("/")
        self.cache_ttl = max(0.0, float(cache_ttl))
        self._session: Optional[aiohttp.ClientSession] = None
        self._cache: Dict[str, Tuple[float, List[Dict[str, Any]]]] = {}
        self._locks: Dict[str, asyncio.Lock] = {}

    async def get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            timeout = aiohttp.ClientTimeout(total=20)
            self._session = aiohttp.ClientSession(
                timeout=timeout,
                headers={
                    "User-Agent": (
                        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
                    ),
                    "Accept": "application/json",
                },
            )
        return self._session

    async def close(self):
        if self._session and not self._session.closed:
            await self._session.close()

    def clear_cache(self):
        self._cache.clear()

    async def _get_json(
        self,
        url: str,
        params: Optional[Dict[str, Any]] = None,
        headers: Optional[Dict[str, str]] = None,
        retries: int = 2,
    ) -> Optional[Any]:
        session = await self.get_session()
        attempts = max(1, retries)

        for attempt in range(attempts):
            try:
                async with session.get(url, params=params, headers=headers) as resp:
                    if resp.status == 200:
                        try:
                            return await resp.json()
                        except Exception as e:
                            logger.error(f"Invalid JSON from {url}: {e}")
                            return None

                    text = await resp.text()
                    logger.error(f"Error fetching {url}: HTTP {resp.status} - {text[:200]}")
                    if resp.status >= 500 and attempt + 1 < attempts:
                        await asyncio.sleep(1.0)
                        continue
                    return None
            except (aiohttp.ClientError, asyncio.TimeoutError) as e:
                logger.warning(
                    f"Network error fetching {url} (attempt {attempt + 1}/{attempts}): {e}"
                )
                if attempt + 1 < attempts:
                    await asyncio.sleep(1.0)
                    continue
                return None
            except Exception as e:
                logger.error(f"Unexpected exception fetching {url}: {e}")
                return None
        return None

    async def _fetch_cached_list(
        self,
        cache_key: str,
        url: str,
        params: Optional[Dict[str, Any]] = None,
        headers: Optional[Dict[str, str]] = None,
        force_refresh: bool = False,
        max_cache_age: Optional[float] = None,
    ) -> Optional[List[Dict[str, Any]]]:
        effective_ttl = self.cache_ttl if max_cache_age is None else max(0.0, float(max_cache_age))

        if not force_refresh and effective_ttl > 0:
            cached = self._cache.get(cache_key)
            if cached is not None:
                ts, items = cached
                if (time.monotonic() - ts) < effective_ttl:
                    return list(items)

        lock = self._locks.setdefault(cache_key, asyncio.Lock())
        async with lock:
            if not force_refresh and effective_ttl > 0:
                cached = self._cache.get(cache_key)
                if cached is not None:
                    ts, items = cached
                    if (time.monotonic() - ts) < effective_ttl:
                        return list(items)

            data = await self._get_json(url, params=params, headers=headers)
            if data is None:
                return None

            if isinstance(data, list):
                items = data
            elif isinstance(data, dict):
                members = data.get("hydra:member", [])
                items = members if isinstance(members, list) else []
            else:
                logger.error(f"Unexpected JSON payload type from {url}: {type(data).__name__}")
                return None

            self._cache[cache_key] = (time.monotonic(), list(items))
            return list(items)

    async def get_schedule(
        self,
        city_id: Optional[int] = None,
        street_id: Optional[int] = None,
        building_name: Optional[str] = None,
        group: Optional[str] = None,
        force_refresh: bool = False,
        max_cache_age: Optional[float] = None,
    ) -> Optional[List[Dict[str, Any]]]:
        """
        Fetches today's and tomorrow's GPV schedule for the specified address and queue.
        Returns None on network/API failure, or a list (possibly empty) on HTTP 200.
        """
        city_id = config.CITY_ID if city_id is None else city_id
        street_id = config.STREET_ID if street_id is None else street_id
        building_name = config.BUILDING_NAME if building_name is None else building_name
        group = config.CHER_GPV if group is None else group

        tz = ZoneInfo(config.TIMEZONE)
        now = datetime.now(tz)

        yesterday = (now - timedelta(days=1)).strftime("%Y-%m-%dT12:00:00+00:00")
        tomorrow = (now + timedelta(days=1)).strftime("%Y-%m-%dT23:59:59+00:00")

        c_val = f"{city_id}/{street_id}/{building_name}"
        x_debug_key = base64.b64encode(c_val.encode("utf-8")).decode("ascii")

        params = {
            "before": tomorrow,
            "after": yesterday,
            "group[]": group,
            "time": c_val.replace("/", ""),
        }
        headers = {"X-debug-key": x_debug_key}
        cache_key = f"schedule:{city_id}:{street_id}:{building_name}:{group}"

        return await self._fetch_cached_list(
            cache_key=cache_key,
            url=f"{self.base_url}/a_gpv_g",
            params=params,
            headers=headers,
            force_refresh=force_refresh,
            max_cache_age=max_cache_age,
        )

    async def get_accidents(
        self,
        otg_id: Optional[int] = None,
        city_id: Optional[int] = None,
        street_id: Optional[int] = None,
        force_refresh: bool = False,
        max_cache_age: Optional[float] = None,
    ) -> Optional[List[Dict[str, Any]]]:
        """
        Fetches unplanned/emergency outages.
        Returns None on network/API failure, or a list on HTTP 200.
        """
        otg_id = config.OTG_ID if otg_id is None else otg_id
        city_id = config.CITY_ID if city_id is None else city_id
        street_id = config.STREET_ID if street_id is None else street_id

        params = {
            "page": 1,
            "otg.id": otg_id,
            "city.id": city_id,
            "street.id": street_id,
        }
        cache_key = f"accidents:{otg_id}:{city_id}:{street_id}"

        return await self._fetch_cached_list(
            cache_key=cache_key,
            url=f"{self.base_url}/pw_accidents",
            params=params,
            force_refresh=force_refresh,
            max_cache_age=max_cache_age,
        )

    async def get_planed_accidents(
        self,
        otg_id: Optional[int] = None,
        city_id: Optional[int] = None,
        street_id: Optional[int] = None,
        force_refresh: bool = False,
        max_cache_age: Optional[float] = None,
    ) -> Optional[List[Dict[str, Any]]]:
        """
        Fetches planned repair outages.
        Returns None on network/API failure, or a list on HTTP 200.
        """
        otg_id = config.OTG_ID if otg_id is None else otg_id
        city_id = config.CITY_ID if city_id is None else city_id
        street_id = config.STREET_ID if street_id is None else street_id

        params = {
            "page": 1,
            "otg.id": otg_id,
            "city.id": city_id,
            "street.id": street_id,
        }
        cache_key = f"planed_accidents:{otg_id}:{city_id}:{street_id}"

        return await self._fetch_cached_list(
            cache_key=cache_key,
            url=f"{self.base_url}/pw_accident_planeds",
            params=params,
            force_refresh=force_refresh,
            max_cache_age=max_cache_age,
        )
