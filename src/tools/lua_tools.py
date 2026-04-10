"""Lua-centric helpers for validation, verification, and refinement guards."""

from __future__ import annotations

import asyncio
import json
import os
import re
import tempfile
from functools import partial
from typing import Any

import structlog

from src.core.llm import LLMProvider
from src.tools.local_runtime import check_lua_file, run_lua_file

logger = structlog.get_logger(__name__)

DEFAULT_LUA_BIN = "lua"
DEFAULT_LUACHECK_BIN = "luacheck"
DEFAULT_STARTUP_TIMEOUT = 3.0
DEFAULT_VERIFICATION_TEMPERATURE = 0.0
DEFAULT_VERIFICATION_SYSTEM_PROMPT = (
    "You review whether a Lua solution fully satisfies the user's request. "
    "Return strict JSON only with the keys: passed, score, summary, missing_requirements, warnings. "
    "Use passed=true only if all important requirements are satisfied."
)

ZERO_WIDTH_PATTERN = re.compile(r"[\u200b\u200c\u200d\ufeff]")
PROBABLE_LUA_LINE_PATTERN = re.compile(
    r"^(--|local\b|function\b|if\b|for\b|while\b|repeat\b|return\b|break\b|goto\b|do\b|"
    r"print\s*\(|io\.|os\.|table\.|math\.|string\.|package\.|require\s*\(|"
    r"[A-Za-z_][A-Za-z0-9_:.]*\s*(?:=|\())"
)
LUA_SIGNAL_PATTERN = re.compile(
    r"\b(local|function|if|then|elseif|end|for|while|repeat|until|return|break|goto|do|"
    r"print|io|os|table|math|string|package|require)\b|--"
)
PROSE_PREFIXES = (
    "да",
    "вот",
    "конечно",
    "исправленный",
    "обновленный",
    "данный",
    "этот",
    "ниже",
    "sure",
    "here",
    "the following",
    "updated",
    "corrected",
    "this lua",
    "lua script",
    "code:",
)
CODE_SUFFIX_MARKERS = ("code:", "код:")
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
_DELETE_KEYWORDS = (
    "remove", "delete", "drop",
    "убери", "удали", "удалить", "уберите", "выкинь",
)


def merge_process_output(stdout: str, stderr: str) -> str:
    """Combine stdout and stderr into one readable string."""
    parts: list[str] = []
    if stdout.strip():
        parts.append(stdout.rstrip())
    if stderr.strip():
        parts.append(stderr.rstrip())
    return "\n".join(parts)


def repair_mojibake(text: str) -> str:
    """Best-effort repair for garbled Windows console output."""
    if not text.strip():
        return text

    candidates = [text]
    for from_encoding, to_encoding in (
        ("cp1251", "cp866"),
        ("cp866", "cp1251"),
        ("latin1", "cp1251"),
        ("latin1", "cp866"),
    ):
        try:
            candidates.append(text.encode(from_encoding).decode(to_encoding))
        except UnicodeError:
            continue

    keywords = (
        "не является",
        "внутренней",
        "внешней",
        "командой",
        "программой",
        "файлом",
        "unexpected symbol",
        "syntax error",
        "warning",
        "error",
        "ошибка",
        "module",
        "not found",
    )

    def score(candidate: str) -> float:
        lower = candidate.lower()
        keyword_score = sum(12 for keyword in keywords if keyword in lower)
        cyrillic_score = sum(1 for char in candidate.lower() if "а" <= char <= "я")
        replacement_penalty = candidate.count("\ufffd") * 20
        mojibake_penalty = sum(candidate.count(char) for char in "¤¦©®ўҐђ‘’")
        return keyword_score + cyrillic_score - replacement_penalty - mojibake_penalty

    return max(candidates, key=score).strip()


def contains_mojibake(text: str) -> bool:
    """Detect probable Windows mojibake in process output."""
    if not text.strip():
        return False

    markers = ("╨", "╤", "Ð", "Ñ", "�")
    if any(marker in text for marker in markers):
        return True

    rs_count = text.count("Р") + text.count("С")
    if rs_count >= 4 and rs_count * 3 >= len(text):
        return True

    pair_count = 0
    for index in range(len(text) - 1):
        current = text[index]
        next_char = text[index + 1]
        if current in ("Р", "С") and (("А" <= next_char <= "я") or next_char in "Ёё"):
            pair_count += 1

    return pair_count >= 4


def infer_program_mode(lua_code: str) -> str:
    """Treat `io.read`-driven scripts as interactive console apps."""
    interactive_patterns = (
        r"\bio\.read\s*\(",
        r"\bio\.stdin\s*:\s*read\s*\(",
        r"\bio\.stdin:read\s*\(",
    )
    for pattern in interactive_patterns:
        if re.search(pattern, lua_code):
            return "interactive"
    return "batch"


def is_tooling_problem(diagnostics: dict[str, Any]) -> bool:
    """Detect environment/tooling failures that are not fixable in Lua."""
    combined = f"{diagnostics.get('run_error', '')}\n{diagnostics.get('luacheck_error', '')}".lower()
    tooling_markers = (
        "not found",
        "не является внутренней",
        "lua interpreter",
        "luacheck exited with code 9009",
        "module 'luacheck.main' not found",
        "missing argument 'files'",
        "usage: luacheck",
        "unavailable in the current environment",
    )
    return any(marker in combined for marker in tooling_markers) and "unexpected symbol" not in combined


def classify_failure_kind(diagnostics: dict[str, Any]) -> str:
    """Classify the dominant failure type for the fix loop prompt."""
    explicit_kind = str(diagnostics.get("failure_kind", "")).strip().lower()
    if explicit_kind:
        return explicit_kind

    combined = "\n".join(
        part for part in (
            diagnostics.get("run_error", ""),
            diagnostics.get("luacheck_error", ""),
            diagnostics.get("verification_summary", ""),
        )
        if part
    ).lower()

    if is_tooling_problem(diagnostics):
        return "tooling"
    if "unexpected symbol" in combined or "expected statement" in combined:
        return "syntax"
    if diagnostics.get("verification_checked") and not diagnostics.get("verification_passed"):
        return "requirements"
    if diagnostics.get("run_error"):
        return "runtime"
    if diagnostics.get("luacheck_error"):
        return "lint"
    return "unknown"


def _sync_run_diagnostics(
    lua_file: str,
    lua_bin: str = DEFAULT_LUA_BIN,
    luacheck_bin: str = DEFAULT_LUACHECK_BIN,
    startup_timeout: float = DEFAULT_STARTUP_TIMEOUT,
) -> dict[str, Any]:
    """Run Lua + luacheck and return a diagnostics dict for the graph."""
    run_error = ""
    run_warning = ""
    luacheck_error = ""
    luacheck_warning = ""
    run_output = ""
    luacheck_output = ""
    started_ok = False
    timed_out = False
    program_mode = "batch"

    try:
        with open(lua_file, "r", encoding="utf-8", errors="replace") as file:
            program_mode = infer_program_mode(file.read())
    except OSError:
        program_mode = "batch"

    try:
        run_result = run_lua_file(
            lua_file,
            lua_bin,
            startup_timeout,
            stdin_mode="inherit" if program_mode == "interactive" else "devnull",
        )
        raw_run_output = merge_process_output(run_result["stdout"], run_result["stderr"])
        run_output = repair_mojibake(raw_run_output)
        timed_out = run_result["timed_out"]
        no_runtime_stderr = not run_result["stderr"].strip()
        if program_mode == "interactive":
            started_ok = no_runtime_stderr and (run_result["success"] or timed_out)
        else:
            started_ok = run_result["success"] and no_runtime_stderr and not timed_out

        if contains_mojibake(raw_run_output) or contains_mojibake(run_output):
            run_warning = (
                "Console output looks garbled in Windows cmd. "
                "Prefer ASCII UI text or configure UTF-8 explicitly.\n"
                f"{run_output}"
            )
        if not run_result["success"] and not timed_out:
            run_error = run_output or f"Lua process exited with code {run_result['returncode']}."
        elif program_mode == "batch" and timed_out:
            run_error = (
                "Batch Lua script did not finish during the startup timeout. "
                "If the program is intentionally interactive, keep input-driven behavior explicit."
            )
    except (FileNotFoundError, RuntimeError) as exc:
        run_error = repair_mojibake(str(exc))

    try:
        luacheck_result = check_lua_file(lua_file, luacheck_bin)
        raw_luacheck_output = merge_process_output(
            luacheck_result["stdout"],
            luacheck_result["stderr"],
        )
        luacheck_output = repair_mojibake(raw_luacheck_output)
        if not luacheck_result["success"]:
            luacheck_error = (
                luacheck_output or f"luacheck exited with code {luacheck_result['returncode']}."
            )
        elif contains_mojibake(raw_luacheck_output) or contains_mojibake(luacheck_output):
            luacheck_warning = (
                "Luacheck output looks garbled in Windows cmd. "
                "Review console encoding if this keeps happening.\n"
                f"{luacheck_output}"
            )
    except (FileNotFoundError, RuntimeError) as exc:
        luacheck_error = repair_mojibake(str(exc))

    diagnostics = {
        "success": started_ok or (not run_error and not luacheck_error),
        "started_ok": started_ok,
        "timed_out": timed_out,
        "program_mode": program_mode,
        "run_output": run_output,
        "run_error": run_error,
        "run_warning": run_warning,
        "luacheck_output": luacheck_output,
        "luacheck_error": luacheck_error,
        "luacheck_warning": luacheck_warning,
    }
    diagnostics["failure_kind"] = classify_failure_kind(diagnostics)
    return diagnostics


async def async_run_diagnostics(
    lua_code: str,
    lua_bin: str = DEFAULT_LUA_BIN,
    luacheck_bin: str = DEFAULT_LUACHECK_BIN,
    startup_timeout: float = DEFAULT_STARTUP_TIMEOUT,
) -> dict[str, Any]:
    """Validate + execute Lua code and return the full diagnostics dict."""
    with tempfile.NamedTemporaryFile(
        suffix=".lua",
        delete=False,
        mode="w",
        encoding="utf-8",
    ) as file:
        file.write(lua_code)
        temp_path = file.name

    try:
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(
            None,
            partial(
                _sync_run_diagnostics,
                temp_path,
                lua_bin,
                luacheck_bin,
                startup_timeout,
            ),
        )
    finally:
        try:
            os.unlink(temp_path)
        except OSError:
            pass


def strip_explanatory_preamble(cleaned: str) -> str:
    """Remove common prose prefixes before the actual Lua source."""
    lines = cleaned.split("\n")
    start_index = 0

    for index, line in enumerate(lines):
        stripped = line.strip()
        if not stripped:
            continue
        lower = stripped.lower().lstrip("> -*").strip()
        if PROBABLE_LUA_LINE_PATTERN.match(stripped):
            start_index = index
            break
        if lower.endswith(CODE_SUFFIX_MARKERS) or any(lower.startswith(prefix) for prefix in PROSE_PREFIXES):
            continue
        if index >= 5:
            break

    trimmed = "\n".join(lines[start_index:]).strip()
    while trimmed.endswith("```"):
        trimmed = trimmed[:-3].rstrip()
    return trimmed


def normalize_lua_code(text: str) -> str:
    """Normalize model output into a standalone Lua file."""
    cleaned = ZERO_WIDTH_PATTERN.sub("", text).replace("\r\n", "\n").replace("\r", "\n").strip()
    fenced = re.search(r"```(?:lua)?\s*(.*?)```", cleaned, flags=re.IGNORECASE | re.DOTALL)
    if fenced:
        cleaned = fenced.group(1).strip()
    else:
        cleaned = strip_explanatory_preamble(cleaned)
    return cleaned.strip()


def analyze_lua_response(text: str) -> dict[str, Any]:
    """Check whether the model returned a plausible standalone Lua file."""
    normalized = normalize_lua_code(text)
    if not normalized:
        return {
            "valid": False,
            "reason": "Model returned an empty response instead of a Lua file.",
            "normalized": "",
            "excerpt": "",
        }

    non_empty_lines = [line.strip() for line in normalized.splitlines() if line.strip()]
    first_line = non_empty_lines[0] if non_empty_lines else ""
    first_line_lower = first_line.lower()
    starts_like_lua = bool(PROBABLE_LUA_LINE_PATTERN.match(first_line))
    lua_signal_count = len(LUA_SIGNAL_PATTERN.findall(normalized[:2000]))
    cyrillic_count = sum(1 for char in normalized[:400] if "\u0400" <= char <= "\u04ff")
    prose_prefix = any(first_line_lower.startswith(prefix) for prefix in PROSE_PREFIXES)

    if prose_prefix and not starts_like_lua:
        reason = "Model prefixed the response with explanatory text instead of starting with Lua code."
    elif cyrillic_count >= 8 and lua_signal_count == 0:
        reason = "Model returned natural-language text instead of Lua code."
    elif not starts_like_lua and lua_signal_count == 0:
        reason = "Response does not look like a standalone Lua file."
    else:
        reason = ""

    return {
        "valid": not reason,
        "reason": reason,
        "normalized": normalized,
        "excerpt": normalized[:500].strip(),
    }


def smart_normalize(raw_response: str) -> str:
    """Normalize LLM output into a clean Lua source string."""
    return normalize_lua_code(raw_response)


def validate_lua_response(raw_response: str) -> dict[str, Any]:
    """Return validation metadata for a raw LLM response."""
    return analyze_lua_response(raw_response)


def _extract_json_block(text: str) -> dict[str, Any]:
    cleaned = text.strip()
    fence = re.search(r"```(?:json)?\s*(.*?)```", cleaned, re.DOTALL)
    if fence:
        cleaned = fence.group(1).strip()
    try:
        data = json.loads(cleaned)
        if isinstance(data, dict):
            return data
    except Exception:
        pass

    start = cleaned.find("{")
    end = cleaned.rfind("}")
    if start != -1 and end > start:
        try:
            data = json.loads(cleaned[start:end + 1])
            if isinstance(data, dict):
                return data
        except Exception:
            pass

    raise RuntimeError(f"Unexpected verification response: {text}")


def _ensure_string_list(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item).strip() for item in value if str(item).strip()]


def _normalize_verification_result(data: dict[str, Any]) -> dict[str, Any]:
    score = data.get("score", 0)
    try:
        score = int(score)
    except (TypeError, ValueError):
        score = 0
    score = max(0, min(100, score))

    return {
        "passed": bool(data.get("passed", False)),
        "score": score,
        "summary": str(data.get("summary", "")).strip(),
        "missing_requirements": _ensure_string_list(data.get("missing_requirements")),
        "warnings": _ensure_string_list(data.get("warnings")),
    }


async def async_verify_requirements(
    llm: LLMProvider,
    prompt: str,
    code: str,
    run_output: str = "",
    luacheck_output: str = "",
) -> dict[str, Any]:
    """Run requirement verification using the same local LLM provider as the graph."""
    messages = [
        {"role": "system", "content": DEFAULT_VERIFICATION_SYSTEM_PROMPT},
        {"role": "user", "content": f"User request:\n{prompt}"},
    ]
    if run_output.strip() or luacheck_output.strip():
        messages.append(
            {
                "role": "user",
                "content": (
                    f"Runtime output:\n{run_output or 'none'}\n\n"
                    f"Luacheck output:\n{luacheck_output or 'none'}"
                ),
            }
        )
    messages.extend(
        [
            {"role": "assistant", "content": code},
            {
                "role": "user",
                "content": (
                    "Check whether the Lua solution above fully satisfies the user request. "
                    "Return strict JSON only in this shape:\n"
                    '{'
                    '"passed": true, '
                    '"score": 100, '
                    '"summary": "short summary", '
                    '"missing_requirements": [], '
                    '"warnings": []'
                    '}'
                ),
            },
        ]
    )

    raw = await llm.chat(messages, temperature=DEFAULT_VERIFICATION_TEMPERATURE)
    normalized = _normalize_verification_result(_extract_json_block(raw))
    if not normalized["summary"]:
        normalized["summary"] = "Verification completed."
    return normalized


def extract_function_names(code: str) -> list[str]:
    """Return ordered Lua function names defined in the file."""
    if not code:
        return []
    seen: set[str] = set()
    ordered: list[str] = []
    for match in _LUA_FUNC_NAME_RE.finditer(code):
        name = match.group(1)
        if name not in seen:
            seen.add(name)
            ordered.append(name)
    return ordered


def extract_function_bodies(code: str) -> dict[str, str]:
    """Return the original source for each named Lua function."""
    if not code:
        return {}
    bodies: dict[str, str] = {}
    for match in _LUA_FUNC_DEF_RE.finditer(code):
        name_match = _LUA_FUNC_NAME_RE.match(match.group(0))
        if name_match:
            bodies.setdefault(name_match.group(1), match.group(0))
    return bodies


def restore_lost_functions(
    original_code: str,
    refined_code: str,
    user_message: str,
) -> tuple[str, list[str]]:
    """Restore silently dropped functions unless the user explicitly removed them."""
    original_names = extract_function_names(original_code)
    original_bodies = extract_function_bodies(original_code)
    refined_names = set(extract_function_names(refined_code))
    user_lower = user_message.lower()

    def explicitly_removed(name: str) -> bool:
        bare = name.split(".")[-1].split(":")[-1].lower()
        if bare not in user_lower:
            return False
        return any(keyword in user_lower for keyword in _DELETE_KEYWORDS)

    missing = [
        name for name in original_names
        if name not in refined_names and not explicitly_removed(name)
    ]
    if not missing:
        return refined_code, []

    logger.warning("refine_lost_functions", missing=missing)
    tail_blocks = [original_bodies[name] for name in missing if name in original_bodies]
    if not tail_blocks:
        return refined_code, []

    separator = "\n\n-- Restored by preservation guard --\n"
    repaired = refined_code.rstrip()
    return_match = re.search(r"\n\s*return\s+([A-Za-z_]\w*)\s*$", repaired)
    if return_match:
        module_name = return_match.group(1)
        head = repaired[: return_match.start()].rstrip()
        body = separator + "\n\n".join(tail_blocks)
        exports = ""
        for name in missing:
            if "." not in name and ":" not in name:
                exports += f"\n{module_name}.{name} = {name}"
        repaired = f"{head}{body}{exports}\n\nreturn {module_name}\n"
    else:
        repaired = repaired + separator + "\n\n".join(tail_blocks) + "\n"

    return repaired, missing
