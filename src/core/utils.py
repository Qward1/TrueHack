"""Shared parsing utilities for LLM responses."""

from __future__ import annotations

import json
import re

import structlog

logger = structlog.get_logger(__name__)

_FENCE_RE = re.compile(r"```(?:json|lua)?\s*(.*?)\s*```", re.DOTALL)


def parse_llm_json(response: str, fallback: dict) -> dict:
    """Robustly parse a JSON dict from an LLM response.

    Steps:
    1. Strip ```json ... ``` / ``` ... ``` fences.
    2. Trim everything before the first ``{`` and after the last ``}``.
    3. ``json.loads``.
    On any error, log and return *fallback*.
    """
    # Step 1 — strip fences
    fence_match = _FENCE_RE.search(response)
    text = fence_match.group(1) if fence_match else response

    # Step 2 — extract outermost {}
    brace_start = text.find("{")
    brace_end = text.rfind("}")
    if brace_start != -1 and brace_end != -1 and brace_end >= brace_start:
        text = text[brace_start : brace_end + 1]

    # Step 3 — parse
    try:
        parsed = json.loads(text)
        if not isinstance(parsed, dict):
            raise ValueError(f"Expected dict, got {type(parsed).__name__}")
        logger.debug("parse_llm_json_ok", keys=list(parsed.keys()))
        return parsed
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "parse_llm_json_failed",
            error=str(exc),
            original_response=response[:300],
        )
        return fallback


def extract_lua_code(response: str) -> str:
    """Return Lua code from an LLM response.

    Looks for a ```lua ... ``` fence first; falls back to the whole response.
    Leading/trailing blank lines are stripped.
    """
    lua_fence = re.search(r"```lua\s*(.*?)\s*```", response, re.DOTALL)
    if lua_fence:
        return lua_fence.group(1).strip()

    # Generic fence fallback (``` ... ```)
    generic_fence = re.search(r"```\s*(.*?)\s*```", response, re.DOTALL)
    if generic_fence:
        return generic_fence.group(1).strip()

    return response.strip()
