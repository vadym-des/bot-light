import logging
import os
import re
from contextlib import asynccontextmanager
from typing import List, Optional, Dict, Any, Tuple
from urllib.parse import urlparse
import aiosqlite
from config import config

logger = logging.getLogger(__name__)

_SCHEMA_RE = re.compile(r"^[a-zA-Z_][a-zA-Z0-9_]*$")

BOOLEAN_SETTING_KEYS = frozenset({
    "notify_outage",
    "notify_restore",
    "notify_schedule_change",
    "quiet_night",
    "daily_digest",
})

ALLOWED_SETTING_KEYS = BOOLEAN_SETTING_KEYS | frozenset({
    "notify_warning",
    "pinned_message_id",
    "last_status_message_id",
    "is_active",
})

PROTECTED_STATE_KEYS = (
    "daily_digest_default_migrated",
    "last_status",
    "active_accidents_hash",
    "active_planed_accidents_hash",
    "pending_restart_notify_chat_id",
)


class Database:
    def __init__(
        self,
        db_path: Optional[str] = None,
        database_url: Optional[str] = None,
        schema: Optional[str] = None,
    ):
        if database_url is not None:
            self.database_url = database_url.strip()
        elif db_path is not None:
            self.database_url = ""
        else:
            self.database_url = config.DATABASE_URL

        self.db_path = db_path or config.DB_PATH

        raw_schema = (schema if schema is not None else config.DB_SCHEMA).strip()
        if not _SCHEMA_RE.match(raw_schema):
            raise ValueError(f"Invalid PostgreSQL schema name: {raw_schema!r}")
        self.schema = raw_schema

        self._pool = None
        self._seen_chats: set[int] = set()

    @property
    def is_postgres(self) -> bool:
        return bool(self.database_url)

    @staticmethod
    def _build_pg_connect_kwargs(database_url: str) -> Tuple[str, Dict[str, Any]]:
        dsn = database_url.strip()
        if dsn.startswith("postgresql+asyncpg://"):
            dsn = "postgresql://" + dsn[len("postgresql+asyncpg://"):]
        elif dsn.startswith("postgres+asyncpg://"):
            dsn = "postgresql://" + dsn[len("postgres+asyncpg://"):]

        parsed = urlparse(dsn)
        hostname = (parsed.hostname or "").lower()
        query_lower = (parsed.query or "").lower()

        kwargs: Dict[str, Any] = {
            "min_size": 1,
            "max_size": 3,
            "command_timeout": 15,
            "statement_cache_size": 0,
        }
        if "sslmode=" not in query_lower and hostname not in {"localhost", "127.0.0.1", "::1", ""}:
            kwargs["ssl"] = "require"

        return dsn, kwargs

    def _require_pool(self):
        if self._pool is None:
            raise RuntimeError("PostgreSQL pool is not initialized. Call init() first.")
        return self._pool

    @asynccontextmanager
    async def _connect(self, row_factory: bool = False):
        if self.db_path != ":memory:":
            parent = os.path.dirname(self.db_path)
            if parent:
                os.makedirs(parent, exist_ok=True)
        conn = await aiosqlite.connect(self.db_path)
        try:
            await conn.execute("PRAGMA busy_timeout = 5000;")
            if row_factory:
                conn.row_factory = aiosqlite.Row
            yield conn
        finally:
            await conn.close()

    async def close(self):
        if self._pool is not None:
            await self._pool.close()
            self._pool = None

    async def init(self):
        self._seen_chats.clear()

        if self.is_postgres:
            await self._init_postgres()
        else:
            await self._init_sqlite()

        if config.DEFAULT_CHAT_ID:
            await self.ensure_default_subscriber(config.DEFAULT_CHAT_ID)
            logger.info(f"Default subscriber {config.DEFAULT_CHAT_ID} ensured.")

    async def _init_postgres(self):
        try:
            import asyncpg
        except ImportError as exc:
            raise RuntimeError(
                "PostgreSQL backend requires the 'asyncpg' package. Install it with: pip install asyncpg"
            ) from exc

        if self._pool is not None:
            await self.close()

        dsn, connect_kwargs = self._build_pg_connect_kwargs(self.database_url)
        self._pool = await asyncpg.create_pool(dsn, **connect_kwargs)

        async with self._pool.acquire() as conn:
            await conn.execute(f"CREATE SCHEMA IF NOT EXISTS {self.schema};")
            try:
                await conn.execute(f"REVOKE ALL ON SCHEMA {self.schema} FROM anon, authenticated;")
            except Exception:
                pass

            await conn.execute(f"""
                CREATE TABLE IF NOT EXISTS {self.schema}.subscribers (
                    chat_id BIGINT PRIMARY KEY,
                    username TEXT,
                    first_name TEXT,
                    notify_outage INTEGER DEFAULT 1,
                    notify_restore INTEGER DEFAULT 1,
                    notify_warning INTEGER DEFAULT 15,
                    notify_schedule_change INTEGER DEFAULT 1,
                    quiet_night INTEGER DEFAULT 1,
                    daily_digest INTEGER DEFAULT 0,
                    pinned_message_id BIGINT DEFAULT NULL,
                    last_status_message_id BIGINT DEFAULT NULL,
                    is_active INTEGER DEFAULT 1,
                    created_at TIMESTAMPTZ DEFAULT NOW()
                );
            """)
            await conn.execute(f"""
                CREATE TABLE IF NOT EXISTS {self.schema}.state (
                    key TEXT PRIMARY KEY,
                    value TEXT,
                    updated_at TIMESTAMPTZ DEFAULT NOW()
                );
            """)

            rows = await conn.fetch(
                """
                SELECT column_name
                FROM information_schema.columns
                WHERE table_schema = $1 AND table_name = 'subscribers';
                """,
                self.schema,
            )
            existing_cols = {row["column_name"] for row in rows}

            migrations = [
                ("quiet_night", "INTEGER DEFAULT 1"),
                ("daily_digest", "INTEGER DEFAULT 0"),
                ("pinned_message_id", "BIGINT DEFAULT NULL"),
                ("last_status_message_id", "BIGINT DEFAULT NULL"),
            ]
            for col_name, col_type in migrations:
                if col_name not in existing_cols:
                    await conn.execute(
                        f"ALTER TABLE {self.schema}.subscribers ADD COLUMN {col_name} {col_type};"
                    )

            migrated = await conn.fetchval(
                f"SELECT value FROM {self.schema}.state WHERE key = 'daily_digest_default_migrated';"
            )
            if not migrated:
                await conn.execute(f"UPDATE {self.schema}.subscribers SET daily_digest = 0;")
                await conn.execute(
                    f"""
                    INSERT INTO {self.schema}.state (key, value, updated_at)
                    VALUES ('daily_digest_default_migrated', '1', NOW())
                    ON CONFLICT (key) DO UPDATE SET
                        value = '1',
                        updated_at = NOW();
                    """
                )

        logger.info(f"PostgreSQL database initialized (schema={self.schema}).")

    async def _init_sqlite(self):
        async with self._connect() as db:
            await db.execute("PRAGMA journal_mode = WAL;")
            await db.execute("PRAGMA synchronous = NORMAL;")
            await db.execute("""
                CREATE TABLE IF NOT EXISTS subscribers (
                    chat_id INTEGER PRIMARY KEY,
                    username TEXT,
                    first_name TEXT,
                    notify_outage INTEGER DEFAULT 1,
                    notify_restore INTEGER DEFAULT 1,
                    notify_warning INTEGER DEFAULT 15,
                    notify_schedule_change INTEGER DEFAULT 1,
                    quiet_night INTEGER DEFAULT 1,
                    daily_digest INTEGER DEFAULT 0,
                    pinned_message_id INTEGER DEFAULT NULL,
                    last_status_message_id INTEGER DEFAULT NULL,
                    is_active INTEGER DEFAULT 1,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );
            """)
            await db.execute("""
                CREATE TABLE IF NOT EXISTS state (
                    key TEXT PRIMARY KEY,
                    value TEXT,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );
            """)

            async with db.execute("PRAGMA table_info(subscribers);") as cursor:
                cols_info = await cursor.fetchall()
            existing_cols = {row[1] for row in cols_info}

            migrations = [
                ("quiet_night", "INTEGER DEFAULT 1"),
                ("daily_digest", "INTEGER DEFAULT 0"),
                ("pinned_message_id", "INTEGER DEFAULT NULL"),
                ("last_status_message_id", "INTEGER DEFAULT NULL"),
            ]
            for col_name, col_type in migrations:
                if col_name not in existing_cols:
                    await db.execute(f"ALTER TABLE subscribers ADD COLUMN {col_name} {col_type};")

            async with db.execute("SELECT value FROM state WHERE key = 'daily_digest_default_migrated'") as cursor:
                row = await cursor.fetchone()
            if not row:
                await db.execute("UPDATE subscribers SET daily_digest = 0;")
                await db.execute("""
                    INSERT INTO state (key, value, updated_at)
                    VALUES ('daily_digest_default_migrated', '1', CURRENT_TIMESTAMP)
                    ON CONFLICT(key) DO UPDATE SET
                        value = '1',
                        updated_at = CURRENT_TIMESTAMP;
                """)

            await db.commit()
        logger.info("SQLite database initialized successfully with WAL and migrations.")

    async def ensure_default_subscriber(self, chat_id: int, first_name: str = "Owner") -> bool:
        if self.is_postgres:
            pool = self._require_pool()
            async with pool.acquire() as conn:
                await conn.execute(
                    f"""
                    INSERT INTO {self.schema}.subscribers (chat_id, first_name, is_active)
                    VALUES ($1, $2, 1)
                    ON CONFLICT (chat_id) DO NOTHING;
                    """,
                    chat_id,
                    first_name,
                )
        else:
            async with self._connect() as db:
                await db.execute(
                    """
                    INSERT OR IGNORE INTO subscribers (chat_id, first_name, is_active)
                    VALUES (?, ?, 1);
                    """,
                    (chat_id, first_name),
                )
                await db.commit()
        self._seen_chats.add(chat_id)
        return True

    async def ensure_subscriber(
        self,
        chat_id: int,
        username: Optional[str] = None,
        first_name: Optional[str] = None,
    ) -> None:
        if chat_id in self._seen_chats:
            return
        if self.is_postgres:
            pool = self._require_pool()
            async with pool.acquire() as conn:
                await conn.execute(
                    f"""
                    INSERT INTO {self.schema}.subscribers (chat_id, username, first_name, is_active)
                    VALUES ($1, $2, $3, 1)
                    ON CONFLICT (chat_id) DO NOTHING;
                    """,
                    chat_id,
                    username,
                    first_name,
                )
        else:
            async with self._connect() as db:
                await db.execute(
                    """
                    INSERT OR IGNORE INTO subscribers (chat_id, username, first_name, is_active)
                    VALUES (?, ?, ?, 1);
                    """,
                    (chat_id, username, first_name),
                )
                await db.commit()
        self._seen_chats.add(chat_id)

    async def add_subscriber(
        self,
        chat_id: int,
        username: Optional[str] = None,
        first_name: Optional[str] = None,
        reactivate: bool = True,
    ) -> bool:
        if self.is_postgres:
            pool = self._require_pool()
            async with pool.acquire() as conn:
                if reactivate:
                    await conn.execute(
                        f"""
                        INSERT INTO {self.schema}.subscribers (chat_id, username, first_name, is_active)
                        VALUES ($1, $2, $3, 1)
                        ON CONFLICT (chat_id) DO UPDATE SET
                            username = COALESCE(EXCLUDED.username, {self.schema}.subscribers.username),
                            first_name = COALESCE(EXCLUDED.first_name, {self.schema}.subscribers.first_name),
                            is_active = 1;
                        """,
                        chat_id,
                        username,
                        first_name,
                    )
                else:
                    await conn.execute(
                        f"""
                        INSERT INTO {self.schema}.subscribers (chat_id, username, first_name, is_active)
                        VALUES ($1, $2, $3, 1)
                        ON CONFLICT (chat_id) DO UPDATE SET
                            username = COALESCE(EXCLUDED.username, {self.schema}.subscribers.username),
                            first_name = COALESCE(EXCLUDED.first_name, {self.schema}.subscribers.first_name);
                        """,
                        chat_id,
                        username,
                        first_name,
                    )
        else:
            async with self._connect() as db:
                if reactivate:
                    await db.execute(
                        """
                        INSERT INTO subscribers (chat_id, username, first_name, is_active)
                        VALUES (?, ?, ?, 1)
                        ON CONFLICT(chat_id) DO UPDATE SET
                            username = COALESCE(excluded.username, subscribers.username),
                            first_name = COALESCE(excluded.first_name, subscribers.first_name),
                            is_active = 1;
                        """,
                        (chat_id, username, first_name),
                    )
                else:
                    await db.execute(
                        """
                        INSERT INTO subscribers (chat_id, username, first_name, is_active)
                        VALUES (?, ?, ?, 1)
                        ON CONFLICT(chat_id) DO UPDATE SET
                            username = COALESCE(excluded.username, subscribers.username),
                            first_name = COALESCE(excluded.first_name, subscribers.first_name);
                        """,
                        (chat_id, username, first_name),
                    )
                await db.commit()
        self._seen_chats.add(chat_id)
        return True

    async def get_subscriber(self, chat_id: int) -> Optional[Dict[str, Any]]:
        if self.is_postgres:
            pool = self._require_pool()
            async with pool.acquire() as conn:
                row = await conn.fetchrow(
                    f"SELECT * FROM {self.schema}.subscribers WHERE chat_id = $1;",
                    chat_id,
                )
                return dict(row) if row else None

        async with self._connect(row_factory=True) as db:
            async with db.execute("SELECT * FROM subscribers WHERE chat_id = ?", (chat_id,)) as cursor:
                row = await cursor.fetchone()
                if row:
                    return dict(row)
                return None

    async def get_active_subscribers(self) -> List[Dict[str, Any]]:
        if self.is_postgres:
            pool = self._require_pool()
            async with pool.acquire() as conn:
                rows = await conn.fetch(
                    f"SELECT * FROM {self.schema}.subscribers WHERE is_active = 1;"
                )
                return [dict(r) for r in rows]

        async with self._connect(row_factory=True) as db:
            async with db.execute("SELECT * FROM subscribers WHERE is_active = 1") as cursor:
                rows = await cursor.fetchall()
                return [dict(r) for r in rows]

    async def update_setting(self, chat_id: int, key: str, value: Any):
        if key not in ALLOWED_SETTING_KEYS:
            raise ValueError(f"Invalid setting key: {key}")

        if self.is_postgres:
            pool = self._require_pool()
            async with pool.acquire() as conn:
                await conn.execute(
                    f"UPDATE {self.schema}.subscribers SET {key} = $1 WHERE chat_id = $2;",
                    value,
                    chat_id,
                )
        else:
            async with self._connect() as db:
                await db.execute(f"UPDATE subscribers SET {key} = ? WHERE chat_id = ?", (value, chat_id))
                await db.commit()

        if key == "is_active":
            self._seen_chats.discard(chat_id)

    async def set_state(self, key: str, value: str):
        if self.is_postgres:
            pool = self._require_pool()
            async with pool.acquire() as conn:
                await conn.execute(
                    f"""
                    INSERT INTO {self.schema}.state (key, value, updated_at)
                    VALUES ($1, $2, NOW())
                    ON CONFLICT (key) DO UPDATE SET
                        value = EXCLUDED.value,
                        updated_at = NOW();
                    """,
                    key,
                    value,
                )
            return

        async with self._connect() as db:
            await db.execute("""
                INSERT INTO state (key, value, updated_at)
                VALUES (?, ?, CURRENT_TIMESTAMP)
                ON CONFLICT(key) DO UPDATE SET
                    value = excluded.value,
                    updated_at = CURRENT_TIMESTAMP;
            """, (key, value))
            await db.commit()

    async def get_state(self, key: str, default: Optional[str] = None) -> Optional[str]:
        if self.is_postgres:
            pool = self._require_pool()
            async with pool.acquire() as conn:
                val = await conn.fetchval(
                    f"SELECT value FROM {self.schema}.state WHERE key = $1;",
                    key,
                )
                return val if val is not None else default

        async with self._connect() as db:
            async with db.execute("SELECT value FROM state WHERE key = ?", (key,)) as cursor:
                row = await cursor.fetchone()
                if row:
                    return row[0]
                return default

    async def get_states_by_prefix(self, prefix: str) -> Dict[str, str]:
        escaped = prefix.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        pattern = f"{escaped}%"

        if self.is_postgres:
            pool = self._require_pool()
            async with pool.acquire() as conn:
                rows = await conn.fetch(
                    f"SELECT key, value FROM {self.schema}.state WHERE key LIKE $1 ESCAPE '\\';",
                    pattern,
                )
                return {row["key"]: row["value"] for row in rows}

        async with self._connect() as db:
            async with db.execute(
                "SELECT key, value FROM state WHERE key LIKE ? ESCAPE '\\'",
                (pattern,),
            ) as cursor:
                rows = await cursor.fetchall()
                return {row[0]: row[1] for row in rows}

    async def cleanup_old_state(self, keep_days: int = 7) -> int:
        days = max(1, int(keep_days))

        if self.is_postgres:
            pool = self._require_pool()
            async with pool.acquire() as conn:
                status = await conn.execute(
                    f"""
                    DELETE FROM {self.schema}.state
                    WHERE updated_at < NOW() - ($1::int * INTERVAL '1 day')
                      AND key <> ALL($2::text[]);
                    """,
                    days,
                    list(PROTECTED_STATE_KEYS),
                )
            try:
                return int(str(status).split()[-1])
            except (IndexError, ValueError):
                return 0

        placeholders = ", ".join("?" for _ in PROTECTED_STATE_KEYS)
        sql = f"""
            DELETE FROM state
            WHERE updated_at < datetime('now', ?)
              AND key NOT IN ({placeholders});
        """
        params = (f"-{days} days", *PROTECTED_STATE_KEYS)
        async with self._connect() as db:
            cursor = await db.execute(sql, params)
            await db.commit()
            return cursor.rowcount if cursor.rowcount is not None else 0


db = Database()
