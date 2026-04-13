"""Async provider for a local OpenAI-compatible LLM runtime (Ollama)."""

from __future__ import annotations

import json
import os
import re
from typing import Any

import structlog
from openai import AsyncOpenAI
from src.core.logging_runtime import write_llm_prompt_audit

logger = structlog.get_logger(__name__)

DEFAULT_URL = "http://127.0.0.1:11434/v1"
DEFAULT_MODEL = "qwen2.5-coder:7b-instruct"
DEFAULT_TIMEOUT = 600.0
DEFAULT_PROMPT_MAX_CHARS = 0


def _parse_prompt_limit() -> int:
    raw = os.getenv("APP_LOG_PROMPT_MAX_CHARS", str(DEFAULT_PROMPT_MAX_CHARS))
    try:
        return int(raw)
    except (TypeError, ValueError):
        return DEFAULT_PROMPT_MAX_CHARS


PROMPT_MAX_CHARS = _parse_prompt_limit()

QWEN_NO_THINK_MODELS = frozenset({
    "qwen3.5:9b",
})


def _agent_model_env_key(agent_name: str) -> str:
    normalized = str(agent_name or "").strip()
    if not normalized:
        return ""
    normalized = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", normalized)
    normalized = re.sub(r"[^A-Za-z0-9]+", "_", normalized).strip("_").upper()
    if not normalized:
        return ""
    return f"OLLAMA_MODEL_{normalized}"


def _resolve_agent_model(agent_name: str, default_model: str) -> str:
    env_key = _agent_model_env_key(agent_name)
    if env_key:
        override = os.getenv(env_key, "").strip()
        if override:
            return override
    return default_model


def _normalize_model_name(model_name: str) -> str:
    return str(model_name or "").strip().lower()


def _should_disable_thinking(model_name: str) -> bool:
    return _normalize_model_name(model_name) in QWEN_NO_THINK_MODELS


class LLMProvider:
    """Async LLM client for a local OpenAI-compatible endpoint."""

    def __init__(
        self,
        base_url: str | None = None,
        model: str | None = None,
        timeout: float = DEFAULT_TIMEOUT,
    ) -> None:
        resolved_base_url = str(base_url or os.getenv("OLLAMA_BASE_URL", DEFAULT_URL)).strip() or DEFAULT_URL
        resolved_model = str(model or os.getenv("OLLAMA_MODEL", DEFAULT_MODEL)).strip() or DEFAULT_MODEL
        self._client = AsyncOpenAI(
            base_url=resolved_base_url,
            api_key="local-runtime",
            timeout=timeout,
        )
        self._model = resolved_model

    def resolve_model(self, agent_name: str = "") -> str:
        return _resolve_agent_model(agent_name, self._model)

    async def generate(
        self,
        prompt: str,
        system: str = "",
        temperature: float = 0.2,
        max_tokens: int | None = None,
        *,
        agent_name: str = "",
    ) -> str:
        """Send a single prompt and return the assistant response."""
        messages: list[dict] = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": prompt})
        return await self._chat(
            messages,
            temperature,
            max_tokens,
            call_kind="generate",
            agent_name=agent_name,
        )

    async def chat(
        self,
        messages: list[dict],
        temperature: float = 0.2,
        max_tokens: int | None = None,
        *,
        agent_name: str = "",
    ) -> str:
        """Send a full message list and return the assistant response."""
        return await self._chat(
            messages,
            temperature,
            max_tokens,
            call_kind="chat",
            agent_name=agent_name,
        )

    async def generate_json(
        self,
        prompt: str,
        system: str = "",
        temperature: float = 0.0,
        *,
        agent_name: str = "",
    ) -> dict:
        """Generate a response and try to parse it as JSON.

        Falls back to an empty dict on parse failure.
        """
        messages: list[dict] = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": prompt})
        raw = await self._chat(
            messages,
            temperature,
            max_tokens=None,
            call_kind="generate_json",
            agent_name=agent_name,
        )
        return _parse_json(raw)

    async def _chat(
        self,
        messages: list[dict],
        temperature: float,
        max_tokens: int | None,
        *,
        call_kind: str,
        agent_name: str = "",
    ) -> str:
        effective_model = self.resolve_model(agent_name)
        kwargs: dict = {
            "model": effective_model,
            "messages": messages,
            "temperature": temperature,
        }
        if _should_disable_thinking(effective_model):
            kwargs["reasoning_effort"] = "none"
        if max_tokens is not None:
            kwargs["max_tokens"] = max_tokens

        message_payload = _normalize_messages_for_logging(messages)
        write_llm_prompt_audit(
            "llm_request",
            call_kind=call_kind,
            agent_name=agent_name,
            model=effective_model,
            base_model=self._model,
            temperature=temperature,
            max_tokens=max_tokens,
            message_count=len(message_payload),
            messages=message_payload,
        )

        response = await self._client.chat.completions.create(**kwargs)
        content = response.choices[0].message.content or ""
        cleaned = content.strip()
        write_llm_prompt_audit(
            "llm_response",
            call_kind=call_kind,
            agent_name=agent_name,
            model=effective_model,
            base_model=self._model,
            chars=len(cleaned),
            content=_truncate_for_prompt_audit(cleaned),
        )
        return cleaned


def _truncate_for_prompt_audit(text: str) -> str:
    if PROMPT_MAX_CHARS <= 0:
        return text
    if len(text) <= PROMPT_MAX_CHARS:
        return text
    return f"{text[:PROMPT_MAX_CHARS]}...[truncated {len(text) - PROMPT_MAX_CHARS} chars]"


def _normalize_messages_for_logging(messages: list[dict]) -> list[dict[str, Any]]:
    normalized: list[dict[str, Any]] = []
    for index, message in enumerate(messages):
        role = str(message.get("role", "")).strip()
        content = message.get("content", "")
        if isinstance(content, str):
            text = content
        else:
            try:
                text = json.dumps(content, ensure_ascii=False)
            except TypeError:
                text = str(content)
        normalized.append(
            {
                "index": index,
                "role": role,
                "content": _truncate_for_prompt_audit(text),
            }
        )
    return normalized


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
