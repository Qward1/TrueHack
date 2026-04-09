"""SQLite connection wrapper using aiosqlite."""

from __future__ import annotations

from pathlib import Path
from types import TracebackType

import aiosqlite
import structlog

logger = structlog.get_logger(__name__)

_DDL = """
CREATE TABLE IF NOT EXISTS chats (
    id         TEXT PRIMARY KEY,
    title      TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS messages (
    id        TEXT PRIMARY KEY,
    chat_id   TEXT NOT NULL,
    role      TEXT NOT NULL,
    content   TEXT NOT NULL,
    timestamp TEXT NOT NULL,
    metadata  TEXT
);

CREATE TABLE IF NOT EXISTS code_artifacts (
    id                TEXT PRIMARY KEY,
    chat_id           TEXT NOT NULL,
    message_id        TEXT NOT NULL,
    code              TEXT NOT NULL,
    language          TEXT NOT NULL DEFAULT 'lua',
    validation_status TEXT NOT NULL,
    test_results      TEXT,
    created_at        TEXT NOT NULL
);
"""


class Database:
    """Async SQLite wrapper. Use as an async context manager."""

    def __init__(self, db_path: str) -> None:
        self._db_path = db_path
        self._conn: aiosqlite.Connection | None = None

    async def init(self) -> None:
        """Open the connection and create tables if they don't exist."""
        Path(self._db_path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = await aiosqlite.connect(self._db_path)
        self._conn.row_factory = aiosqlite.Row
        await self._conn.executescript(_DDL)
        await self._conn.commit()
        logger.info("db_initialised", path=self._db_path)

    async def close(self) -> None:
        """Close the database connection."""
        if self._conn:
            await self._conn.close()
            self._conn = None

    @property
    def conn(self) -> aiosqlite.Connection:
        if self._conn is None:
            raise RuntimeError("Database not initialised. Call await db.init() first.")
        return self._conn

    async def __aenter__(self) -> "Database":
        await self.init()
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: TracebackType | None,
    ) -> None:
        await self.close()
