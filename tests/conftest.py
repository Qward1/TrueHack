"""Shared pytest fixtures — FakeLLMProvider for offline testing."""

from __future__ import annotations

import os
from typing import Any

import pytest

# Ensure lua54 is findable without system PATH change
_LUA_DIR = r"C:\lua54"
if os.path.isdir(_LUA_DIR) and _LUA_DIR not in os.environ.get("PATH", ""):
    os.environ["PATH"] = _LUA_DIR + os.pathsep + os.environ.get("PATH", "")

from src.core.llm import LLMProvider

LUA_CMD = "lua54"


class FakeLLMProvider(LLMProvider):
    """Deterministic LLM stub for tests — returns pre-configured responses."""

    def __init__(
        self,
        text_response: str = 'print("hello")',
        structured_response: dict | None = None,
    ) -> None:
        self._text = text_response
        self._structured = structured_response or {"intent": "generate_clear", "confidence": 0.9}

    async def generate(
        self,
        prompt: str,
        system: str = "",
        temperature: float | None = None,
        max_tokens: int | None = None,
    ) -> str:
        return self._text

    async def generate_structured(
        self,
        prompt: str,
        system: str,
        schema: dict,
        fallback: dict | None = None,
    ) -> dict:
        return self._structured


@pytest.fixture
def fake_llm() -> FakeLLMProvider:
    """Default FakeLLMProvider with a simple generate_clear intent."""
    return FakeLLMProvider()


@pytest.fixture
def settings():
    """Application settings (real config, no side effects)."""
    from src.core.config import get_settings
    return get_settings()
