"""Async wrappers around the existing check_lua / run_lua / auto_fix tools.

These wrappers run the synchronous tool functions in a thread pool so they
integrate cleanly with the async LangGraph pipeline. The actual tool logic
lives in the original files (check_lua.py, run_lua.py, auto_fix_lua.py)
and is NOT duplicated here.
"""

from __future__ import annotations

import asyncio
import os
import re
import tempfile
from functools import partial
from typing import Any

import structlog

# Import the proven, working tool implementations from root-level files.
from check_lua import check_lua_file
from run_lua import run_lua_file
from auto_fix_lua import (
    classify_failure_kind,
    contains_mojibake,
    infer_program_mode,
    is_tooling_problem,
    repair_mojibake,
    run_diagnostics as _sync_run_diagnostics,
)
from generate import analyze_lua_response, normalize_lua_code
from prompt_verifier import verify_prompt_requirements as _sync_verify

logger = structlog.get_logger(__name__)

DEFAULT_LUA_BIN = "lua"
DEFAULT_LUACHECK_BIN = "luacheck"
DEFAULT_STARTUP_TIMEOUT = 3.0


async def async_run_diagnostics(
    lua_code: str,
    lua_bin: str = DEFAULT_LUA_BIN,
    luacheck_bin: str = DEFAULT_LUACHECK_BIN,
    startup_timeout: float = DEFAULT_STARTUP_TIMEOUT,
) -> dict[str, Any]:
    """Validate + execute Lua code and return full diagnostics dict.

    Writes code to a temp file, runs lua interpreter + luacheck, returns
    the same diagnostics dict as auto_fix_lua.run_diagnostics().
    """
    # Write to temp file
    with tempfile.NamedTemporaryFile(
        suffix=".lua", delete=False, mode="w", encoding="utf-8"
    ) as f:
        f.write(lua_code)
        tmp_path = f.name

    try:
        loop = asyncio.get_running_loop()
        diagnostics = await loop.run_in_executor(
            None,
            partial(
                _sync_run_diagnostics,
                tmp_path,
                lua_bin,
                luacheck_bin,
                startup_timeout,
            ),
        )
        return diagnostics
    finally:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass


async def async_verify_requirements(
    prompt: str,
    code: str,
    run_output: str = "",
    luacheck_output: str = "",
    model: str = "local-model",
    url: str = "http://127.0.0.1:1234/v1/chat/completions",
    timeout: float = 600.0,
) -> dict[str, Any]:
    """Run prompt-requirements verification in a thread."""
    extra_context = (
        f"Runtime output:\n{run_output or 'none'}\n\n"
        f"Luacheck output:\n{luacheck_output or 'none'}"
    )
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(
        None,
        partial(
            _sync_verify,
            prompt=prompt,
            solution_content=code,
            model=model,
            url=url,
            timeout_seconds=timeout,
            extra_context=extra_context,
        ),
    )


def smart_normalize(raw_response: str) -> str:
    """Normalize LLM response: strip fences, preamble, zero-width chars.

    Uses the battle-tested logic from generate.py.
    """
    return normalize_lua_code(raw_response)


def validate_lua_response(raw_response: str) -> dict[str, Any]:
    """Check if LLM response looks like valid Lua code.

    Returns dict with keys: valid, reason, normalized, excerpt.
    """
    return analyze_lua_response(raw_response)


# ── Function preservation (ported from Dimentiy branch) ──────────────

_LUA_FUNC_NAME_RE = re.compile(
    r"(?:local\s+)?function\s+"
    r"([A-Za-z_][A-Za-z_0-9]*(?:[.:][A-Za-z_][A-Za-z_0-9]*)?)"
)

_LUA_FUNC_DEF_RE = re.compile(
    r"(?:local\s+)?function\s+"
    r"([A-Za-z_][A-Za-z_0-9]*(?:[.:][A-Za-z_][A-Za-z_0-9]*)?)"
    r"\s*\([^)]*\)[\s\S]*?\bend\b",
    re.MULTILINE,
)


def extract_function_names(code: str) -> list[str]:
    """Return ordered list of Lua function names defined in *code*."""
    if not code:
        return []
    seen: set[str] = set()
    ordered: list[str] = []
    for m in _LUA_FUNC_NAME_RE.finditer(code):
        name = m.group(1)
        if name not in seen:
            seen.add(name)
            ordered.append(name)
    return ordered


def extract_function_bodies(code: str) -> dict[str, str]:
    """Return mapping function_name → full source for each function in *code*."""
    if not code:
        return {}
    bodies: dict[str, str] = {}
    for m in _LUA_FUNC_DEF_RE.finditer(code):
        nm = _LUA_FUNC_NAME_RE.match(m.group(0))
        if nm:
            bodies.setdefault(nm.group(1), m.group(0))
    return bodies


_DELETE_KEYWORDS = (
    "remove", "delete", "drop",
    "убери", "удали", "удалить", "уберите", "выкинь",
)


def restore_lost_functions(
    original_code: str,
    refined_code: str,
    user_message: str,
) -> tuple[str, list[str]]:
    """Ensure *refined_code* didn't silently drop functions from *original_code*.

    Returns (final_code, list_of_restored_names).
    """
    original_names = extract_function_names(original_code)
    original_bodies = extract_function_bodies(original_code)
    refined_names = set(extract_function_names(refined_code))
    user_low = user_message.lower()

    def _explicitly_removed(name: str) -> bool:
        bare = name.split(".")[-1].split(":")[-1].lower()
        if bare not in user_low:
            return False
        return any(kw in user_low for kw in _DELETE_KEYWORDS)

    missing = [
        n for n in original_names
        if n not in refined_names and not _explicitly_removed(n)
    ]
    if not missing:
        return refined_code, []

    logger.warning("refine_lost_functions", missing=missing)

    tail_blocks = [original_bodies[n] for n in missing if n in original_bodies]
    if not tail_blocks:
        return refined_code, []

    separator = "\n\n-- Restored by preservation guard --\n"
    repaired = refined_code.rstrip()

    return_m = re.search(r"\n\s*return\s+([A-Za-z_]\w*)\s*$", repaired)
    if return_m:
        mod = return_m.group(1)
        head = repaired[: return_m.start()].rstrip()
        body = separator + "\n\n".join(tail_blocks)
        exports = ""
        for name in missing:
            if "." not in name and ":" not in name:
                exports += f"\n{mod}.{name} = {name}"
        repaired = f"{head}{body}{exports}\n\nreturn {mod}\n"
    else:
        repaired = repaired + separator + "\n\n".join(tail_blocks) + "\n"

    return repaired, missing
