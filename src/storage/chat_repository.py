"""Repository for chats, messages, and code artefacts."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

import structlog

from src.storage.database import Database
from src.storage.models import Chat, CodeArtifact, Message

logger = structlog.get_logger(__name__)


def _now() -> datetime:
    return datetime.now(tz=timezone.utc)


def _dt(value: str) -> datetime:
    """Parse ISO datetime string; attach UTC timezone if missing."""
    dt = datetime.fromisoformat(value)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _json_dump(obj: Any) -> str | None:
    return json.dumps(obj, ensure_ascii=False) if obj is not None else None


def _json_load(text: str | None) -> Any:
    return json.loads(text) if text else None


class ChatRepository:
    """CRUD for chats, messages, and code artefacts."""

    def __init__(self, db: Database) -> None:
        self._db = db

    # ── Chats ─────────────────────────────────────────────────────────

    async def create_chat(self, title: str = "Новый чат") -> Chat:
        """Create and persist a new chat, returning the model."""
        now = _now()
        chat = Chat(id=str(uuid4()), title=title, created_at=now, updated_at=now)
        await self._db.conn.execute(
            "INSERT INTO chats (id, title, created_at, updated_at) VALUES (?, ?, ?, ?)",
            (chat.id, chat.title, chat.created_at.isoformat(), chat.updated_at.isoformat()),
        )
        await self._db.conn.commit()
        logger.debug("chat_created", chat_id=chat.id)
        return chat

    async def get_chat(self, chat_id: str) -> Chat | None:
        """Return a chat by ID, or ``None`` if not found."""
        async with self._db.conn.execute(
            "SELECT * FROM chats WHERE id = ?", (chat_id,)
        ) as cur:
            row = await cur.fetchone()
        if row is None:
            return None
        return Chat(
            id=row["id"],
            title=row["title"],
            created_at=_dt(row["created_at"]),
            updated_at=_dt(row["updated_at"]),
        )

    async def list_chats(self) -> list[Chat]:
        """Return all chats ordered by creation time descending."""
        async with self._db.conn.execute(
            "SELECT * FROM chats ORDER BY created_at DESC"
        ) as cur:
            rows = await cur.fetchall()
        return [
            Chat(
                id=r["id"],
                title=r["title"],
                created_at=_dt(r["created_at"]),
                updated_at=_dt(r["updated_at"]),
            )
            for r in rows
        ]

    async def delete_chat(self, chat_id: str) -> None:
        """Delete a chat and all its messages / artefacts."""
        await self._db.conn.execute("DELETE FROM code_artifacts WHERE chat_id = ?", (chat_id,))
        await self._db.conn.execute("DELETE FROM messages WHERE chat_id = ?", (chat_id,))
        await self._db.conn.execute("DELETE FROM chats WHERE id = ?", (chat_id,))
        await self._db.conn.commit()
        logger.debug("chat_deleted", chat_id=chat_id)

    async def update_chat_title(self, chat_id: str, title: str) -> None:
        """Update the human-readable title of a chat."""
        await self._db.conn.execute(
            "UPDATE chats SET title = ?, updated_at = ? WHERE id = ?",
            (title, _now().isoformat(), chat_id),
        )
        await self._db.conn.commit()

    # ── Messages ──────────────────────────────────────────────────────

    async def add_message(
        self,
        chat_id: str,
        role: str,
        content: str,
        metadata: dict | None = None,
    ) -> Message:
        """Append a message to a chat."""
        now = _now()
        msg = Message(
            id=str(uuid4()),
            chat_id=chat_id,
            role=role,
            content=content,
            timestamp=now,
            metadata=metadata,
        )
        await self._db.conn.execute(
            "INSERT INTO messages (id, chat_id, role, content, timestamp, metadata) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (msg.id, msg.chat_id, msg.role, msg.content, msg.timestamp.isoformat(), _json_dump(metadata)),
        )
        # bump chat.updated_at
        await self._db.conn.execute(
            "UPDATE chats SET updated_at = ? WHERE id = ?",
            (now.isoformat(), chat_id),
        )
        await self._db.conn.commit()
        return msg

    async def get_messages(self, chat_id: str, limit: int = 50) -> list[Message]:
        """Return up to *limit* messages for a chat, oldest first."""
        async with self._db.conn.execute(
            "SELECT * FROM messages WHERE chat_id = ? ORDER BY timestamp ASC LIMIT ?",
            (chat_id, limit),
        ) as cur:
            rows = await cur.fetchall()
        return [
            Message(
                id=r["id"],
                chat_id=r["chat_id"],
                role=r["role"],
                content=r["content"],
                timestamp=_dt(r["timestamp"]),
                metadata=_json_load(r["metadata"]),
            )
            for r in rows
        ]

    async def get_recent_messages(self, chat_id: str, limit: int = 10) -> list[Message]:
        """Return the *limit* most recent messages, oldest first (for LLM context)."""
        async with self._db.conn.execute(
            "SELECT * FROM ("
            "  SELECT * FROM messages WHERE chat_id = ? ORDER BY timestamp DESC LIMIT ?"
            ") ORDER BY timestamp ASC",
            (chat_id, limit),
        ) as cur:
            rows = await cur.fetchall()
        return [
            Message(
                id=r["id"],
                chat_id=r["chat_id"],
                role=r["role"],
                content=r["content"],
                timestamp=_dt(r["timestamp"]),
                metadata=_json_load(r["metadata"]),
            )
            for r in rows
        ]

    # ── Code artefacts ────────────────────────────────────────────────

    async def save_code_artifact(
        self,
        chat_id: str,
        message_id: str,
        code: str,
        validation_status: str = "pending",
        test_results: dict | None = None,
    ) -> CodeArtifact:
        """Persist a Lua code artefact linked to a message."""
        now = _now()
        artifact = CodeArtifact(
            id=str(uuid4()),
            chat_id=chat_id,
            message_id=message_id,
            code=code,
            language="lua",
            validation_status=validation_status,
            test_results=test_results,
            created_at=now,
        )
        await self._db.conn.execute(
            "INSERT INTO code_artifacts "
            "(id, chat_id, message_id, code, language, validation_status, test_results, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                artifact.id,
                artifact.chat_id,
                artifact.message_id,
                artifact.code,
                artifact.language,
                artifact.validation_status,
                _json_dump(test_results),
                artifact.created_at.isoformat(),
            ),
        )
        await self._db.conn.commit()
        logger.debug("artifact_saved", artifact_id=artifact.id, status=validation_status)
        return artifact

    async def get_latest_code(self, chat_id: str) -> str | None:
        """Return the most recently saved Lua code for a chat, or ``None``."""
        async with self._db.conn.execute(
            "SELECT code FROM code_artifacts WHERE chat_id = ? ORDER BY created_at DESC LIMIT 1",
            (chat_id,),
        ) as cur:
            row = await cur.fetchone()
        return row["code"] if row else None
