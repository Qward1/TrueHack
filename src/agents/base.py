"""Abstract base class for all agents."""

from __future__ import annotations

import re
from abc import ABC, abstractmethod
from pathlib import Path

from src.core.config import Settings
from src.core.llm import LLMProvider
from src.core.state import AgentState

_PROMPTS_DIR = Path(__file__).parents[2] / "config" / "prompts"

# Matches {placeholder} — a single brace around an identifier.
# Double-brace escapes {{ / }} are handled separately.
_PLACEHOLDER_RE = re.compile(r"\{([a-zA-Z_][a-zA-Z0-9_]*)\}")


class BaseAgent(ABC):
    """Every agent holds a reference to an LLM provider and settings."""

    def __init__(self, llm: LLMProvider, settings: Settings) -> None:
        self._llm = llm
        self._settings = settings

    @abstractmethod
    async def run(self, state: AgentState) -> AgentState:
        """Process *state* and return an updated copy."""

    def _load_prompt(self, template_name: str) -> str:
        """Read ``config/prompts/{template_name}.txt`` and return its content."""
        path = _PROMPTS_DIR / f"{template_name}.txt"
        return path.read_text(encoding="utf-8")

    def _render_prompt(self, template: str, **kwargs: object) -> str:
        """Replace ``{placeholder}`` tokens with *kwargs* values.

        - ``{key}`` is replaced with ``str(kwargs[key])`` or ``""`` if missing.
        - ``{{`` / ``}}`` escape sequences are collapsed to literal ``{`` / ``}``
          **after** placeholder substitution so that JSON examples in prompts
          survive intact.
        """
        def _replace(match: re.Match) -> str:
            key = match.group(1)
            value = kwargs.get(key)
            return "" if value is None else str(value)

        result = _PLACEHOLDER_RE.sub(_replace, template)
        # Collapse {{ → { and }} → } (escaped braces in prompt examples)
        result = result.replace("{{", "{").replace("}}", "}")
        return result
