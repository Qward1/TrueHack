"""Tests for the storage layer (in-memory SQLite via tempfile)."""

from __future__ import annotations

import pytest

from src.storage.chat_repository import ChatRepository
from src.storage.database import Database


@pytest.fixture
async def repo(tmp_path):
    """Fresh in-memory repo for each test."""
    db_path = str(tmp_path / "test.db")
    async with Database(db_path) as db:
        yield ChatRepository(db)


class TestChatCRUD:
    @pytest.mark.asyncio
    async def test_create_and_get_chat(self, repo):
        chat = await repo.create_chat("Тестовый чат")
        assert chat.title == "Тестовый чат"
        assert chat.id

        fetched = await repo.get_chat(chat.id)
        assert fetched is not None
        assert fetched.id == chat.id
        assert fetched.title == "Тестовый чат"

    @pytest.mark.asyncio
    async def test_get_nonexistent_chat_returns_none(self, repo):
        result = await repo.get_chat("nonexistent-id")
        assert result is None

    @pytest.mark.asyncio
    async def test_list_chats(self, repo):
        await repo.create_chat("Chat A")
        await repo.create_chat("Chat B")
        chats = await repo.list_chats()
        assert len(chats) == 2
        titles = {c.title for c in chats}
        assert titles == {"Chat A", "Chat B"}

    @pytest.mark.asyncio
    async def test_update_title(self, repo):
        chat = await repo.create_chat("Old title")
        await repo.update_chat_title(chat.id, "New title")
        fetched = await repo.get_chat(chat.id)
        assert fetched.title == "New title"

    @pytest.mark.asyncio
    async def test_delete_chat(self, repo):
        chat = await repo.create_chat()
        await repo.delete_chat(chat.id)
        assert await repo.get_chat(chat.id) is None
        assert await repo.list_chats() == []


class TestMessages:
    @pytest.mark.asyncio
    async def test_add_and_get_messages(self, repo):
        chat = await repo.create_chat()
        await repo.add_message(chat.id, "user", "Привет!")
        await repo.add_message(chat.id, "assistant", "Привет, чем могу помочь?")

        msgs = await repo.get_messages(chat.id)
        assert len(msgs) == 2
        assert msgs[0].role == "user"
        assert msgs[1].role == "assistant"

    @pytest.mark.asyncio
    async def test_message_with_metadata(self, repo):
        chat = await repo.create_chat()
        meta = {"intent": "generate", "confidence": 0.95}
        msg = await repo.add_message(chat.id, "user", "Напиши код", metadata=meta)
        assert msg.metadata == meta

        msgs = await repo.get_messages(chat.id)
        assert msgs[0].metadata == meta

    @pytest.mark.asyncio
    async def test_get_recent_messages_limit(self, repo):
        chat = await repo.create_chat()
        for i in range(15):
            await repo.add_message(chat.id, "user", f"Message {i}")

        recent = await repo.get_recent_messages(chat.id, limit=5)
        assert len(recent) == 5
        # should be the last 5, oldest-first
        assert recent[-1].content == "Message 14"
        assert recent[0].content == "Message 10"

    @pytest.mark.asyncio
    async def test_messages_deleted_with_chat(self, repo):
        chat = await repo.create_chat()
        await repo.add_message(chat.id, "user", "hello")
        await repo.delete_chat(chat.id)
        # should not raise; chat no longer exists
        msgs = await repo.get_messages(chat.id)
        assert msgs == []


class TestCodeArtifacts:
    @pytest.mark.asyncio
    async def test_save_and_get_latest_code(self, repo):
        chat = await repo.create_chat()
        msg = await repo.add_message(chat.id, "assistant", "Here is the code")

        artifact = await repo.save_code_artifact(
            chat_id=chat.id,
            message_id=msg.id,
            code='print("hello")',
            validation_status="passed",
        )
        assert artifact.code == 'print("hello")'
        assert artifact.validation_status == "passed"
        assert artifact.language == "lua"

        latest = await repo.get_latest_code(chat.id)
        assert latest == 'print("hello")'

    @pytest.mark.asyncio
    async def test_get_latest_code_returns_most_recent(self, repo):
        chat = await repo.create_chat()
        msg = await repo.add_message(chat.id, "assistant", "code")

        await repo.save_code_artifact(chat.id, msg.id, "print(1)", "passed")
        await repo.save_code_artifact(chat.id, msg.id, "print(2)", "passed")

        assert await repo.get_latest_code(chat.id) == "print(2)"

    @pytest.mark.asyncio
    async def test_get_latest_code_no_artifacts_returns_none(self, repo):
        chat = await repo.create_chat()
        assert await repo.get_latest_code(chat.id) is None

    @pytest.mark.asyncio
    async def test_artifact_with_test_results(self, repo):
        chat = await repo.create_chat()
        msg = await repo.add_message(chat.id, "assistant", "code")
        results = {"passed": True, "output": "ok", "errors": ""}

        artifact = await repo.save_code_artifact(
            chat.id, msg.id, "print(1)", "passed", test_results=results
        )
        assert artifact.test_results == results
