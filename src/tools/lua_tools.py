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
from src.tools.local_runtime import (
    run_lua_file,
    run_lua_file_with_input,
    to_cmd_path,
)

logger = structlog.get_logger(__name__)

DEFAULT_LUA_BIN = os.getenv("LUA_BIN", "lua55")
DEFAULT_STARTUP_TIMEOUT = 3.0
DEFAULT_E2E_TIMEOUT = 8.0
DEFAULT_MAX_E2E_CASES = 3
DEFAULT_VERIFICATION_TEMPERATURE = 0.0
LOWCODE_LUA_VERSION = "Lua 5.5"
LOWCODE_JSONSTRING_OPEN = "lua{"
LOWCODE_JSONSTRING_CLOSE = "}lua"
LOWCODE_CONTRACT_TEXT = (
    f"Target contract:\n"
    f"- Use {LOWCODE_LUA_VERSION} syntax and conventions.\n"
    f"- The script is described in JsonString format: {LOWCODE_JSONSTRING_OPEN}<code>{LOWCODE_JSONSTRING_CLOSE}.\n"
    "- This is a workflow/LUS script, not a console or CLI application.\n"
    "- Never use JsonPath to access variables or fields.\n"
    "- Access data directly by field/key.\n"
    "- Declared LowCode variables are stored in wf.vars.\n"
    "- Variables received at startup from variables are stored in wf.initVariables.\n"
    "- The script should transform workflow data, return a value, and/or update wf.vars.\n"
    "- Avoid console input/output flows, prompts, menus, io.read, io.stdin:read, print, and io.write.\n"
    "- Allowed primitive types: nil, boolean, number, string, array, table, function.\n"
    "- To create/mark arrays use _utils.array.new() and _utils.array.markAsArray(arr).\n"
    "- Allowed basic constructs: if/then/else, while/do/end, for/do/end, repeat/until.\n"
)
DEFAULT_VERIFICATION_SYSTEM_PROMPT = (
    "You review whether a Lua solution fully satisfies the user's request. "
    "Return strict JSON only with the keys: passed, score, summary, missing_requirements, warnings. "
    "Use passed=true only if all important requirements are satisfied."
)
DEFAULT_E2E_SYSTEM_PROMPT = (
    "You design deterministic end-to-end tests for a single Lua workflow script. "
    "Return strict JSON only with key: tests. "
    "Each test must contain: name, stdin, expected_stdout_contains, expected_stdout_not_contains, expected_exit_code. "
    "Use concise assertions and keep at most 3 tests."
)

ZERO_WIDTH_PATTERN = re.compile(r"[\u200b\u200c\u200d\ufeff]")
LOWCODE_JSONSTRING_PATTERN = re.compile(r"lua\{\s*([\s\S]*?)\s*\}lua", re.IGNORECASE)
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
WF_PATH_SUFFIX_RE = re.compile(
    r"(?:\s*\.\s*([A-Za-z_][A-Za-z0-9_]*)|\s*\[\s*['\"]([^'\"]+)['\"]\s*\])"
)
WF_ROOT_ACCESS_RE = re.compile(
    r"\bwf\.(vars|initVariables)"
    r"((?:\s*\.\s*[A-Za-z_][A-Za-z0-9_]*|\s*\[\s*['\"][^'\"]+['\"]\s*\])+)"
)
WF_ALIAS_ASSIGN_RE = re.compile(
    r"(?:local\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=\s*"
    r"(wf\.(?:vars|initVariables)(?:\s*\.\s*[A-Za-z_][A-Za-z0-9_]*|\s*\[\s*['\"][^'\"]+['\"]\s*\])+)"
)
LOWCODE_CONSOLE_INPUT_PATTERNS = (
    (re.compile(r"\bio\.read\s*\("), "io.read"),
    (re.compile(r"\bio\.stdin\s*:\s*read\s*\("), "io.stdin:read"),
    (re.compile(r"\bio\.stdin:read\s*\("), "io.stdin:read"),
)
LOWCODE_CONSOLE_OUTPUT_PATTERNS = (
    (re.compile(r"\bprint\s*\("), "print"),
    (re.compile(r"\bio\.write\s*\("), "io.write"),
    (re.compile(r"\bio\.stdout\s*:\s*write\s*\("), "io.stdout:write"),
    (re.compile(r"\bio\.stdout:write\s*\("), "io.stdout:write"),
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
    """Classify validation mode for the generated Lua chunk."""
    interactive_patterns = (
        r"\bio\.read\s*\(",
        r"\bio\.stdin\s*:\s*read\s*\(",
        r"\bio\.stdin:read\s*\(",
    )
    for pattern in interactive_patterns:
        if re.search(pattern, lua_code):
            return "interactive"
    return "workflow"


def inspect_lowcode_script_contract(lua_code: str) -> dict[str, list[str]]:
    """Detect console-style APIs that do not fit the workflow/LUS script contract."""
    blockers: list[str] = []
    warnings: list[str] = []

    for pattern, label in LOWCODE_CONSOLE_INPUT_PATTERNS:
        if pattern.search(lua_code):
            blockers.append(label)

    for pattern, label in LOWCODE_CONSOLE_OUTPUT_PATTERNS:
        if pattern.search(lua_code):
            warnings.append(label)

    return {"blockers": blockers, "warnings": warnings}


def is_tooling_problem(diagnostics: dict[str, Any]) -> bool:
    """Detect environment/tooling failures that are not fixable in Lua."""
    combined = str(diagnostics.get("run_error", "")).lower()
    tooling_markers = (
        "not found",
        "не является внутренней",
        "lua interpreter",
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
    return "unknown"


def build_mock_init_value(name: str) -> str:
    """Build a conservative Lua literal for an inferred init variable name."""
    lowered = name.strip().lower()
    if not lowered:
        return '"sample"'

    if lowered == "datum":
        return '"20260410"'
    if lowered == "time":
        return '"123045"'
    if any(token in lowered for token in ("time", "date", "timestamp", "datetime", "recall")):
        return '"2026-04-10T12:00:00"'
    if any(token in lowered for token in ("email", "emails")):
        return '{"user@example.com"}'
    if lowered.startswith(("is", "has", "can", "should")) or any(
        token in lowered for token in ("enabled", "active", "valid", "success", "available")
    ):
        return "true"
    if any(token in lowered for token in ("count", "amount", "sum", "total", "price", "age", "index", "num", "id")):
        return "1"
    if any(token in lowered for token in ("list", "items", "array", "values", "result")):
        return "{}"
    if lowered in {"json", "body", "head", "data", "payload", "idoc"}:
        return "{}"
    if lowered in {"packages"}:
        return "{{ items = {} }}"
    return '"sample"'


def _parse_wf_expression(expression: str) -> tuple[str, list[str]] | None:
    match = re.match(r"\s*wf\.(vars|initVariables)(.*)", expression)
    if not match:
        return None
    root = match.group(1)
    suffix = match.group(2)
    segments: list[str] = []
    for segment_match in WF_PATH_SUFFIX_RE.finditer(suffix):
        segment = str(segment_match.group(1) or segment_match.group(2) or "").strip()
        if segment:
            segments.append(segment)
    if not segments:
        return None
    return root, segments


def collect_lowcode_access_paths(lua_code: str) -> dict[str, list[list[str]]]:
    """Collect direct and aliased wf.vars/wf.initVariables access paths."""
    discovered: dict[str, list[list[str]]] = {"vars": [], "initVariables": []}
    seen: set[tuple[str, ...]] = set()
    aliases: dict[str, tuple[str, list[str]]] = {}

    def add_path(root: str, segments: list[str]) -> None:
        key = (root, *segments)
        if not segments or key in seen:
            return
        seen.add(key)
        discovered[root].append(segments)

    for match in WF_ROOT_ACCESS_RE.finditer(lua_code):
        parsed = _parse_wf_expression(match.group(0))
        if parsed:
            add_path(*parsed)

    for match in WF_ALIAS_ASSIGN_RE.finditer(lua_code):
        alias = str(match.group(1) or "").strip()
        parsed = _parse_wf_expression(str(match.group(2) or ""))
        if not alias or not parsed:
            continue
        aliases[alias] = parsed
        add_path(*parsed)

    for alias, (root, base_segments) in aliases.items():
        alias_pattern = re.compile(
            rf"\b{re.escape(alias)}"
            r"((?:\s*\.\s*[A-Za-z_][A-Za-z0-9_]*|\s*\[\s*['\"][^'\"]+['\"]\s*\])+)"
        )
        for match in alias_pattern.finditer(lua_code):
            suffix_segments = [
                str(segment_match.group(1) or segment_match.group(2) or "").strip()
                for segment_match in WF_PATH_SUFFIX_RE.finditer(str(match.group(1) or ""))
                if str(segment_match.group(1) or segment_match.group(2) or "").strip()
            ]
            if suffix_segments:
                add_path(root, [*base_segments, *suffix_segments])

    return discovered


def build_mock_leaf_value(path_segments: list[str]) -> str:
    """Choose a conservative Lua literal for the leaf of a mocked path."""
    if not path_segments:
        return "{}"
    return build_mock_init_value(path_segments[-1])


def _lua_path_expression(root: str, segments: list[str]) -> str:
    expression = f"wf.{root}"
    for segment in segments:
        expression += f"[{json.dumps(segment)}]"
    return expression


def build_mock_assignment_lines(root: str, paths: list[list[str]]) -> list[str]:
    """Build Lua lines that materialize nested mock tables for the given wf root."""
    lines: list[str] = []
    ensured_tables: set[tuple[str, ...]] = set()

    for path in sorted(paths, key=lambda item: (len(item), item)):
        for depth in range(1, len(path)):
            prefix = tuple(path[:depth])
            if prefix in ensured_tables:
                continue
            expression = _lua_path_expression(root, list(prefix))
            lines.append(f'if {expression} == nil or type({expression}) ~= "table" then {expression} = {{}} end')
            ensured_tables.add(prefix)

        leaf_expression = _lua_path_expression(root, path)
        leaf_value = build_mock_leaf_value(path)
        lines.append(f"if {leaf_expression} == nil then {leaf_expression} = {leaf_value} end")
    return lines


def build_lowcode_validation_harness(lua_file: str, lua_code: str) -> tuple[str, dict[str, list[str]]]:
    """Build a temporary harness that injects a mock LowCode runtime around the user file."""
    access_paths = collect_lowcode_access_paths(lua_code)
    assignment_lines = [
        *build_mock_assignment_lines("vars", access_paths["vars"]),
        *build_mock_assignment_lines("initVariables", access_paths["initVariables"]),
    ]
    assignments_block = "\n".join(assignment_lines)
    if assignments_block:
        assignments_block = f"{assignments_block}\n"

    user_path_literal = json.dumps(to_cmd_path(lua_file))
    harness = (
        "wf = wf or {}\n"
        "wf.vars = wf.vars or {}\n"
        "wf.initVariables = wf.initVariables or {}\n"
        "_utils = _utils or {}\n"
        "_utils.array = _utils.array or {}\n"
        "if _utils.array.new == nil then\n"
        "    _utils.array.new = function()\n"
        "        return {}\n"
        "    end\n"
        "end\n"
        "if _utils.array.markAsArray == nil then\n"
        "    _utils.array.markAsArray = function(arr)\n"
        "        return arr\n"
        "    end\n"
        "end\n"
        f"{assignments_block}"
        "local function _traceback(err)\n"
        "    if debug and debug.traceback then\n"
        "        return debug.traceback(err, 2)\n"
        "    end\n"
        "    return tostring(err)\n"
        "end\n"
        "local ok, err = xpcall(function()\n"
        f"    dofile({user_path_literal})\n"
        "end, _traceback)\n"
        "if not ok then\n"
        "    io.stderr:write(tostring(err))\n"
        "    io.stderr:write('\\n')\n"
        "    os.exit(1)\n"
        "end\n"
    )
    mocked_paths = {
        "vars": [".".join(path) for path in access_paths["vars"]],
        "initVariables": [".".join(path) for path in access_paths["initVariables"]],
    }
    return harness, mocked_paths


def _sync_run_diagnostics(
    lua_file: str,
    lua_bin: str = DEFAULT_LUA_BIN,
    startup_timeout: float = DEFAULT_STARTUP_TIMEOUT,
) -> dict[str, Any]:
    """Run Lua runtime validation and return a diagnostics dict for the graph."""
    run_error = ""
    run_warning = ""
    run_output = ""
    started_ok = False
    timed_out = False
    program_mode = "workflow"
    harness_path = ""
    mocked_init_variables: list[str] = []
    mocked_var_paths: list[str] = []
    contract_blockers: list[str] = []
    contract_warnings: list[str] = []

    try:
        with open(lua_file, "r", encoding="utf-8", errors="replace") as file:
            lua_code = file.read()
            program_mode = infer_program_mode(lua_code)
    except OSError:
        lua_code = ""
        program_mode = "workflow"

    contract_analysis = inspect_lowcode_script_contract(lua_code)
    contract_blockers = contract_analysis["blockers"]
    contract_warnings = contract_analysis["warnings"]
    if contract_blockers:
        run_error = (
            "Workflow/LUS script must not use console input APIs "
            f"({', '.join(contract_blockers)}). Use wf.vars / wf.initVariables and return values instead."
        )
        diagnostics = {
            "success": False,
            "started_ok": False,
            "timed_out": False,
            "program_mode": "workflow",
            "validation_context": "lowcode_mock_harness",
            "mocked_init_variables": [],
            "mocked_var_paths": [],
            "contract_blockers": contract_blockers,
            "contract_warnings": contract_warnings,
            "run_output": "",
            "run_error": run_error,
            "run_warning": "",
            "luacheck_output": "",
            "luacheck_error": "",
            "luacheck_warning": "",
            "failure_kind": "contract",
        }
        return diagnostics

    try:
        harness_code, mocked_paths = build_lowcode_validation_harness(lua_file, lua_code)
        mocked_init_variables = mocked_paths["initVariables"]
        mocked_var_paths = mocked_paths["vars"]
        with tempfile.NamedTemporaryFile(
            suffix=".lua",
            delete=False,
            mode="w",
            encoding="utf-8",
        ) as harness_file:
            harness_file.write(harness_code)
            harness_path = harness_file.name

        run_result = run_lua_file(
            harness_path,
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

        if contract_warnings:
            run_warning = (
                "Workflow/LUS scripts should avoid console output APIs unless explicitly required: "
                f"{', '.join(contract_warnings)}."
            )
        if contains_mojibake(raw_run_output) or contains_mojibake(run_output):
            mojibake_warning = (
                "Console output looks garbled in Windows cmd. "
                "Prefer returning values or wf.vars updates instead of console output.\n"
                f"{run_output}"
            )
            run_warning = f"{run_warning}\n{mojibake_warning}".strip() if run_warning else mojibake_warning
        if not run_result["success"] and not timed_out:
            run_error = run_output or f"Lua process exited with code {run_result['returncode']}."
        elif program_mode == "workflow" and timed_out:
            run_error = (
                "Workflow/LUS script did not finish during the validation timeout. "
                "Avoid console waits and keep the script as a pure data transformation."
            )
    except (FileNotFoundError, RuntimeError) as exc:
        run_error = repair_mojibake(str(exc))

    diagnostics = {
        "success": started_ok or not run_error,
        "started_ok": started_ok,
        "timed_out": timed_out,
        "program_mode": program_mode,
        "validation_context": "lowcode_mock_harness",
        "mocked_init_variables": mocked_init_variables,
        "mocked_var_paths": mocked_var_paths,
        "contract_blockers": contract_blockers,
        "contract_warnings": contract_warnings,
        "run_output": run_output,
        "run_error": run_error,
        "run_warning": run_warning,
        "luacheck_output": "",
        "luacheck_error": "",
        "luacheck_warning": "",
    }
    diagnostics["failure_kind"] = classify_failure_kind(diagnostics)
    if harness_path:
        try:
            os.unlink(harness_path)
        except OSError:
            pass
    return diagnostics


async def async_run_diagnostics(
    lua_code: str,
    lua_bin: str = DEFAULT_LUA_BIN,
    startup_timeout: float = DEFAULT_STARTUP_TIMEOUT,
) -> dict[str, Any]:
    """Validate by running the Lua code and return the full diagnostics dict."""
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


def unwrap_lowcode_jsonstring(text: str) -> str:
    """Extract Lua body from the LowCode JsonString wrapper when present."""
    match = LOWCODE_JSONSTRING_PATTERN.search(text.strip())
    if not match:
        return text
    return match.group(1).strip()


def format_lowcode_jsonstring(lua_code: str) -> str:
    """Render plain Lua code into the LowCode JsonString wrapper."""
    return f"{LOWCODE_JSONSTRING_OPEN}\n{lua_code.strip()}\n{LOWCODE_JSONSTRING_CLOSE}"


def normalize_lua_code(text: str) -> str:
    """Normalize model output into a standalone Lua file."""
    cleaned = ZERO_WIDTH_PATTERN.sub("", text).replace("\r\n", "\n").replace("\r", "\n").strip()
    cleaned = unwrap_lowcode_jsonstring(cleaned)
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
) -> dict[str, Any]:
    """Run requirement verification using the same local LLM provider as the graph."""
    messages = [
        {"role": "system", "content": DEFAULT_VERIFICATION_SYSTEM_PROMPT},
        {"role": "user", "content": f"User request:\n{prompt}\n\n{LOWCODE_CONTRACT_TEXT}"},
    ]
    if run_output.strip():
        messages.append(
            {
                "role": "user",
                "content": f"Runtime output:\n{run_output or 'none'}",
            }
        )
    messages.extend(
        [
            {"role": "assistant", "content": format_lowcode_jsonstring(code)},
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


def _normalize_e2e_case(index: int, raw_case: object) -> dict[str, Any]:
    if not isinstance(raw_case, dict):
        return {}

    name = str(raw_case.get("name") or f"case_{index + 1}").strip() or f"case_{index + 1}"
    stdin_text = str(raw_case.get("stdin", "") or "")
    expected_contains = _ensure_string_list(raw_case.get("expected_stdout_contains"))
    expected_not_contains = _ensure_string_list(raw_case.get("expected_stdout_not_contains"))

    expected_exit_code = raw_case.get("expected_exit_code", 0)
    try:
        expected_exit_code = int(expected_exit_code)
    except (TypeError, ValueError):
        expected_exit_code = 0

    return {
        "name": name[:120],
        "stdin": stdin_text[:4000],
        "expected_stdout_contains": expected_contains[:6],
        "expected_stdout_not_contains": expected_not_contains[:6],
        "expected_exit_code": expected_exit_code,
    }


def _normalize_e2e_suite(data: dict[str, Any]) -> dict[str, Any]:
    raw_tests = data.get("tests")
    if not isinstance(raw_tests, list):
        raw_tests = []

    tests: list[dict[str, Any]] = []
    for index, raw_case in enumerate(raw_tests[:DEFAULT_MAX_E2E_CASES]):
        normalized = _normalize_e2e_case(index, raw_case)
        if normalized:
            tests.append(normalized)

    return {"tests": tests}


async def async_generate_e2e_suite(
    llm: LLMProvider,
    prompt: str,
    code: str,
    target_path: str = "",
) -> dict[str, Any]:
    """Generate a compact JSON e2e test suite for the Lua file."""
    location = target_path or "(not set)"
    messages = [
        {"role": "system", "content": DEFAULT_E2E_SYSTEM_PROMPT},
        {
            "role": "user",
            "content": (
                f"User request:\n{prompt}\n\n"
                f"Target path:\n{location}\n\n"
                "Produce tests for user-visible behavior only. "
                "Avoid brittle exact full-output matches."
            ),
        },
        {"role": "assistant", "content": code},
        {
            "role": "user",
            "content": (
                "Return strict JSON only in this shape:\n"
                '{'
                '"tests": ['
                '{'
                '"name": "test name", '
                '"stdin": "", '
                '"expected_stdout_contains": [], '
                '"expected_stdout_not_contains": [], '
                '"expected_exit_code": 0'
                '}'
                "]"
                "}"
            ),
        },
    ]

    raw = await llm.chat(messages, temperature=0.0)
    parsed = _extract_json_block(raw)
    normalized = _normalize_e2e_suite(parsed)
    if not normalized["tests"]:
        raise RuntimeError("LLM returned an empty e2e suite.")
    return normalized


def _sync_run_e2e_suite(
    lua_code: str,
    suite: dict[str, Any],
    lua_bin: str = DEFAULT_LUA_BIN,
    timeout_seconds: float = DEFAULT_E2E_TIMEOUT,
) -> dict[str, Any]:
    tests = suite.get("tests", []) if isinstance(suite, dict) else []
    if not tests:
        return {
            "passed": False,
            "summary": "E2E suite is empty.",
            "cases": [],
            "failed_cases": [],
            "retryable": False,
        }

    with tempfile.NamedTemporaryFile(
        suffix=".lua",
        delete=False,
        mode="w",
        encoding="utf-8",
    ) as file:
        file.write(lua_code)
        temp_path = file.name

    case_results: list[dict[str, Any]] = []
    retryable = True
    try:
        for index, case in enumerate(tests):
            case_name = str(case.get("name", f"case_{index + 1}"))
            stdin_text = str(case.get("stdin", "") or "")
            expect_contains = _ensure_string_list(case.get("expected_stdout_contains"))
            expect_not_contains = _ensure_string_list(case.get("expected_stdout_not_contains"))

            expected_exit_code = case.get("expected_exit_code", 0)
            try:
                expected_exit_code = int(expected_exit_code)
            except (TypeError, ValueError):
                expected_exit_code = 0

            try:
                run_result = run_lua_file_with_input(
                    temp_path,
                    stdin_text=stdin_text,
                    lua_bin=lua_bin,
                    timeout_seconds=timeout_seconds,
                )
            except (FileNotFoundError, RuntimeError) as exc:
                retryable = False
                case_results.append(
                    {
                        "name": case_name,
                        "passed": False,
                        "reason": str(exc),
                        "timed_out": False,
                        "returncode": -1,
                        "output": "",
                    }
                )
                break

            output = repair_mojibake(
                merge_process_output(run_result.get("stdout", ""), run_result.get("stderr", ""))
            )
            timed_out = bool(run_result.get("timed_out", False))
            returncode = int(run_result.get("returncode", 0))

            reason_parts: list[str] = []
            passed = True

            if timed_out:
                passed = False
                reason_parts.append("timed_out")
            if returncode != expected_exit_code:
                passed = False
                reason_parts.append(
                    f"unexpected_exit_code(expected={expected_exit_code}, actual={returncode})"
                )

            for expected in expect_contains:
                if expected not in output:
                    passed = False
                    reason_parts.append(f"missing_output_fragment: {expected}")

            for forbidden in expect_not_contains:
                if forbidden and forbidden in output:
                    passed = False
                    reason_parts.append(f"forbidden_output_fragment: {forbidden}")

            case_results.append(
                {
                    "name": case_name,
                    "passed": passed,
                    "reason": "; ".join(reason_parts),
                    "timed_out": timed_out,
                    "returncode": returncode,
                    "output": output[:3000],
                }
            )
    finally:
        try:
            os.unlink(temp_path)
        except OSError:
            pass

    failed_cases = [case for case in case_results if not case.get("passed")]
    passed = bool(case_results) and not failed_cases
    if passed:
        summary = f"E2E passed ({len(case_results)}/{len(case_results)})."
    else:
        summary = f"E2E failed ({len(case_results) - len(failed_cases)}/{len(case_results)})."

    return {
        "passed": passed,
        "summary": summary,
        "cases": case_results,
        "failed_cases": failed_cases,
        "retryable": retryable,
    }


async def async_run_e2e_suite(
    lua_code: str,
    suite: dict[str, Any],
    lua_bin: str = DEFAULT_LUA_BIN,
    timeout_seconds: float = DEFAULT_E2E_TIMEOUT,
) -> dict[str, Any]:
    """Run generated e2e test cases against the current Lua code."""
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(
        None,
        partial(
            _sync_run_e2e_suite,
            lua_code,
            suite,
            lua_bin,
            timeout_seconds,
        ),
    )


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
