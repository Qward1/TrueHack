"""LLM abstraction layer.

Provides:
- LLMProvider — abstract base class for any LLM provider.
- LMStudioProvider — concrete implementation using LM Studio's OpenAI-compatible API.
- LLMManager — per-agent provider factory with lazy initialisation.

Adding a new provider (e.g. Ollama) is a matter of writing a new subclass of
:class:`LLMProvider` and calling :func:`register_provider` to register it
under the short name used in ``settings.yaml`` (``llm.provider``).
"""

from __future__ import annotations

import json
import re
import time
from abc import ABC, abstractmethod

import structlog
from openai import APIConnectionError, APIError, AsyncOpenAI

from src.core.config import Settings, get_settings

logger = structlog.get_logger(__name__)


# ─── Abstract base class ──────────────────────────────────────────────────
class LLMProvider(ABC):
    """Abstract interface for any LLM provider."""

    @abstractmethod
    async def generate(
        self,
        prompt: str,
        system: str = "",
        temperature: float = 0.7,
        max_tokens: int = 1024,
    ) -> str:
        """Generate a free-form text completion for ``prompt``.

        Concrete providers may also carry per-instance defaults (set by
        :class:`LLMManager`); when they do, omitting ``temperature`` /
        ``max_tokens`` falls back to those instance defaults rather than the
        signature defaults declared here.
        """

    @abstractmethod
    async def generate_structured(
        self,
        prompt: str,
        system: str,
        schema: dict,
    ) -> dict:
        """Generate a JSON response conforming to ``schema``."""


# ─── LM Studio implementation ─────────────────────────────────────────────
_JSON_FENCE_RE = re.compile(r"^\s*```(?:json)?\s*(.*?)\s*```\s*$", re.DOTALL)


class LMStudioProvider(LLMProvider):
    """LLM provider backed by LM Studio's local OpenAI-compatible server."""

    def __init__(
        self,
        model: str,
        base_url: str = "http://localhost:1234/v1",
        api_key: str = "lm-studio",
        default_temperature: float = 0.7,
        default_max_tokens: int = 1024,
    ) -> None:
        self.model = model
        self.base_url = base_url
        self._default_temperature = default_temperature
        self._default_max_tokens = default_max_tokens
        self._client = AsyncOpenAI(base_url=base_url, api_key=api_key)

    async def generate(
        self,
        prompt: str,
        system: str = "",
        temperature: float | None = None,
        max_tokens: int | None = None,
    ) -> str:
        """Call ``chat.completions.create`` and return the assistant's text.

        If ``temperature`` / ``max_tokens`` are not supplied, the instance
        defaults configured by :class:`LLMManager` for a specific agent are
        used instead.
        """
        temp = self._default_temperature if temperature is None else temperature
        toks = self._default_max_tokens if max_tokens is None else max_tokens

        messages: list[dict[str, str]] = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": prompt})

        start = time.perf_counter()
        try:
            response = await self._client.chat.completions.create(
                model=self.model,
                messages=messages,
                temperature=temp,
                max_tokens=toks,
            )
        except APIConnectionError as exc:
            raise ConnectionError(
                f"LM Studio is unreachable at {self.base_url}. "
                "Make sure LM Studio is running and the local server is enabled."
            ) from exc
        except APIError as exc:
            raise RuntimeError(f"LM Studio API error: {exc}") from exc

        elapsed = time.perf_counter() - start
        content = response.choices[0].message.content or ""

        logger.info(
            "llm_call",
            provider="lmstudio",
            model=self.model,
            prompt_len=len(prompt),
            response_len=len(content),
            temperature=temp,
            max_tokens=toks,
            elapsed_s=round(elapsed, 3),
        )
        return content

    async def generate_structured(
        self,
        prompt: str,
        system: str,
        schema: dict,
    ) -> dict:
        """Ask the model for JSON, strip markdown fences, retry up to 2 times."""
        json_instruction = (
            "You must respond with ONLY valid JSON matching this schema: "
            f"{json.dumps(schema)}. No explanations, no markdown, just JSON."
        )
        full_system = f"{system}\n\n{json_instruction}".strip()

        last_error: Exception | None = None
        for attempt in range(3):  # 1 initial attempt + 2 retries
            raw = await self.generate(
                prompt=prompt,
                system=full_system,
                temperature=0.1,
            )
            cleaned = self._strip_json_fence(raw)
            try:
                parsed = json.loads(cleaned)
            except json.JSONDecodeError as exc:
                last_error = exc
                logger.warning(
                    "llm_json_parse_failed",
                    attempt=attempt + 1,
                    error=str(exc),
                    raw=raw[:200],
                )
                continue
            if not isinstance(parsed, dict):
                last_error = ValueError(
                    f"Expected JSON object, got {type(parsed).__name__}"
                )
                logger.warning(
                    "llm_json_not_object",
                    attempt=attempt + 1,
                    type=type(parsed).__name__,
                )
                continue
            return parsed

        raise ValueError(
            f"Failed to parse JSON from model after 3 attempts: {last_error}"
        )

    @staticmethod
    def _strip_json_fence(text: str) -> str:
        """Remove ```json ... ``` fences if present, leaving raw JSON."""
        match = _JSON_FENCE_RE.match(text)
        if match:
            return match.group(1).strip()
        return text.strip()


# ─── Provider registry ────────────────────────────────────────────────────
_PROVIDER_REGISTRY: dict[str, type[LLMProvider]] = {
    "lmstudio": LMStudioProvider,
}


def register_provider(name: str, provider_cls: type[LLMProvider]) -> None:
    """Register a new provider class under a short name used in settings.yaml."""
    _PROVIDER_REGISTRY[name] = provider_cls


# ─── Manager ──────────────────────────────────────────────────────────────
class LLMManager:
    """Creates and caches one :class:`LLMProvider` per agent.

    Each provider is initialised with the generation parameters (``temperature``,
    ``max_tokens``) declared for that agent in ``settings.yaml``.
    """

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._cache: dict[str, LLMProvider] = {}

    def get_provider(self, agent_name: str) -> LLMProvider:
        """Return (lazily creating) the provider configured for ``agent_name``."""
        if agent_name in self._cache:
            return self._cache[agent_name]

        llm_cfg = self._settings.llm
        provider_cls = _PROVIDER_REGISTRY.get(llm_cfg.provider)
        if provider_cls is None:
            raise ValueError(
                f"Unknown LLM provider '{llm_cfg.provider}'. "
                f"Registered: {list(_PROVIDER_REGISTRY)}"
            )

        params = self._settings.get_agent_params(agent_name)
        provider = provider_cls(
            model=llm_cfg.model,
            base_url=llm_cfg.base_url,
            api_key=llm_cfg.api_key,
            default_temperature=params.temperature,
            default_max_tokens=params.max_tokens,
        )
        self._cache[agent_name] = provider
        return provider


# ─── Smoke test ───────────────────────────────────────────────────────────
if __name__ == "__main__":
    import asyncio

    async def _main() -> None:
        settings = get_settings()
        manager = LLMManager(settings)
        provider = manager.get_provider("qa")

        print(f"Provider : {type(provider).__name__}")
        print(f"Model    : {settings.llm.model}")
        print(f"Base URL : {settings.llm.base_url}")
        print("-" * 60)

        try:
            reply = await provider.generate(
                prompt="Say 'hello from LM Studio' in exactly four words.",
                system="You are a terse assistant.",
            )
        except ConnectionError as exc:
            print(f"[CONNECTION ERROR] {exc}")
            return
        except RuntimeError as exc:
            print(f"[RUNTIME ERROR] {exc}")
            return

        print("Response:")
        print(reply)

    asyncio.run(_main())
