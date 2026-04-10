"""Async provider for a local OpenAI-compatible LLM runtime."""

from __future__ import annotations

import json
import re

import structlog
from openai import AsyncOpenAI

logger = structlog.get_logger(__name__)

DEFAULT_URL = "http://127.0.0.1:1234/v1"
DEFAULT_MODEL = "local-model"
DEFAULT_TIMEOUT = 600.0


class LLMProvider:
    """Async LLM client for a local OpenAI-compatible endpoint."""

    def __init__(
        self,
        base_url: str = DEFAULT_URL,
        model: str = DEFAULT_MODEL,
        timeout: float = DEFAULT_TIMEOUT,
    ) -> None:
        self._client = AsyncOpenAI(
            base_url=base_url,
            api_key="local-runtime",
            timeout=timeout,
        )
        self._model = model

    async def generate(
        self,
        prompt: str,
        system: str = "",
        temperature: float = 0.2,
        max_tokens: int | None = None,
    ) -> str:
        """Send a single prompt and return the assistant response."""
        messages: list[dict] = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": prompt})
        return await self._chat(messages, temperature, max_tokens)

    async def chat(
        self,
        messages: list[dict],
        temperature: float = 0.2,
        max_tokens: int | None = None,
    ) -> str:
        """Send a full message list and return the assistant response."""
        return await self._chat(messages, temperature, max_tokens)

    async def generate_json(
        self,
        prompt: str,
        system: str = "",
        temperature: float = 0.0,
    ) -> dict:
        """Generate a response and try to parse it as JSON.

        Falls back to an empty dict on parse failure.
        """
        raw = await self.generate(prompt, system, temperature)
        return _parse_json(raw)

    async def _chat(
        self,
        messages: list[dict],
        temperature: float,
        max_tokens: int | None,
    ) -> str:
        kwargs: dict = {
            "model": self._model,
            "messages": messages,
            "temperature": temperature,
        }
        if max_tokens is not None:
            kwargs["max_tokens"] = max_tokens

        response = await self._client.chat.completions.create(**kwargs)
        content = response.choices[0].message.content or ""
        logger.debug(
            "llm_response",
            model=self._model,
            chars=len(content),
            temperature=temperature,
        )
        return content.strip()


def _parse_json(text: str) -> dict:
    """Robustly extract a JSON dict from LLM output."""
    cleaned = text.strip()

    # Try direct parse first
    try:
        result = json.loads(cleaned)
        if isinstance(result, dict):
            return result
    except json.JSONDecodeError:
        pass

    # Strip markdown fences
    fence = re.search(r"```(?:json)?\s*(.*?)```", cleaned, re.DOTALL)
    if fence:
        cleaned = fence.group(1).strip()
        try:
            result = json.loads(cleaned)
            if isinstance(result, dict):
                return result
        except json.JSONDecodeError:
            pass

    # Extract outermost braces
    start = cleaned.find("{")
    end = cleaned.rfind("}")
    if start != -1 and end > start:
        try:
            result = json.loads(cleaned[start : end + 1])
            if isinstance(result, dict):
                return result
        except json.JSONDecodeError:
            pass

    logger.warning("llm_json_parse_failed", text=text[:200])
    return {}
