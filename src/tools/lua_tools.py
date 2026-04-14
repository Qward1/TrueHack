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
    to_cmd_path,
)

logger = structlog.get_logger(__name__)

DEFAULT_LUA_BIN = os.getenv("LUA_BIN", "lua55")
DEFAULT_STARTUP_TIMEOUT = 3.0
DEFAULT_VERIFICATION_TEMPERATURE = 0.0
LOWCODE_LUA_VERSION = "Lua 5.5"
LOWCODE_JSONSTRING_OPEN = "lua{"
LOWCODE_JSONSTRING_CLOSE = "}lua"
LOWCODE_CONTRACT_TEXT = (
    f"Target contract:\n"
    f"- {LOWCODE_LUA_VERSION} syntax.\n"
    f"- Output format: {LOWCODE_JSONSTRING_OPEN}<code>{LOWCODE_JSONSTRING_CLOSE}.\n"
    "- This is a workflow Lua script, NOT a console app or CLI tool.\n"
    "- Read input from wf.vars.X or wf.initVariables.X and access fields directly by key.\n"
    "- If the request or pasted context mentions wf.vars.* or wf.initVariables.*, use those exact paths directly.\n"
    "- Never recreate the provided workflow input as demo tables like local data = {...} or local emails = {...}.\n"
    "- Output: use `return <value>` and/or explicit wf.vars updates; never print(), io.write(), io.read().\n"
    "- For new arrays: call `_utils.array.new()` with no inline arguments, populate items explicitly, then call `_utils.array.markAsArray(arr)` before return/store.\n"
    "- For shape-sensitive array tasks, an array is a table whose keys are exactly numeric 1..n without gaps. A table with string keys like `name` or `phone` is an object, not an array. Treat an empty table as an array.\n"
    "- When normalizing shape-sensitive data, distinguish scalar vs object-like table vs array-like table; do not rely only on `type(x) == 'table'`, `next(x)`, or emptiness-only tests.\n"
    "- Keep the script focused on the task and avoid unrelated wrappers, classes, or boilerplate.\n"
    "- Start with the task logic immediately, unless helper functions are needed for correctness or reuse.\n"
    "- Allowed constructs: if/then/else, while/do/end, for/do/end, repeat/until.\n"
)
LOWCODE_RESPONSE_FORMAT_REQUIREMENT = (
    "MANDATORY RESPONSE FORMAT:\n"
    "- The response MUST start with the literal three characters `lua{` and end with the literal four characters `}lua`.\n"
    "- Do NOT begin the response with three backticks (```), do NOT end it with three backticks, do NOT place the word 'lua' immediately after backticks.\n"
    "- Do NOT wrap the response in quotes, XML tags, or any other delimiters.\n"
    "- Emit nothing before `lua{` and nothing after `}lua` — no prose, no explanation, no blank lines before the opening tag.\n"
    "Exact shape of a correct response (replace the body with your script):\n"
    "lua{\n"
    "...\n"
    "return ...\n"
    "}lua"
)
DEFAULT_VERIFICATION_SYSTEM_PROMPT = ("""
   You are a strict verifier for LowCode Lua 5.5 workflow solutions.

Goal:
Decide whether the Lua solution satisfies the user's request using only the provided evidence.

Decision rules:
1. Default to passed=false.
2. Set passed=true only if every required check is pass and missing_requirements is empty.
3. If any critical check is fail -> passed=false.
4. If any critical check is unclear -> passed=false.
5. score=100 only if passed=true.

Evidence:
- user request
- workflow context
- updated workflow state, if provided
- runtime result, if provided
- Lua solution under review

Critical checks:
- workflow_path_usage
- target_shape_satisfied
- logic_correctness

Other checks:
- source_shape_understood
- helper_api_usage
- edge_case_handling

Rules:
- Judge against the actual request and concrete evidence.
- Do not invent hidden tests or missing runtime outputs.
- If runtime result is missing but updated workflow state is provided, inspect workflow changes.
- If the task allows return or workflow update, accept only what matches the request.
- Fail if the code uses the wrong workflow path.
- Fail if the code performs the wrong operation.
- Fail if the code uses hardcoded demo data.
- Fail if the output shape/type does not match the request.
- Fail if the code is invalid or unreliable in Lua.
- Fail if the required raw output contract is violated.

Concrete-input rule:
- First infer the expected result for the provided input.
- Then judge whether the code would produce that result for that input.
- Missing handling of other edge cases is not a failure unless the task explicitly requires it.

Return strict JSON only with:
- passed
- score
- summary
- missing_requirements
- warnings
- checks"""
)

ZERO_WIDTH_PATTERN = re.compile(r"[\u200b\u200c\u200d\ufeff]")
LOWCODE_JSONSTRING_PATTERN = re.compile(r"lua\{\s*([\s\S]*?)\s*\}lua", re.IGNORECASE)
JSON_LUA_PAYLOAD_KEYS = ("lua", "code", "script", "source", "content")
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
WORKFLOW_CONTEXT_HINT_RE = re.compile(r"(?i)(?:\bwf\b|\"wf\"|'wf')")
WORKFLOW_ROOT_HINT_RE = re.compile(
    r"(?i)(?:\bvars\b|\binitVariables\b|\"vars\"|'vars'|\"initVariables\"|'initVariables')"
)
TASK_TOKEN_RE = re.compile(r"[A-Za-zА-Яа-яЁё0-9_]+")
SEMANTIC_STEM_GROUPS: dict[str, tuple[str, ...]] = {
    "cart": ("cart", "basket", "корзин"),
    "item": ("item", "items", "товар", "product", "products", "goods", "sku", "позиц", "position"),
    "wishlist": ("wishlist", "wish", "избран"),
    "email": ("email", "emails", "mail", "почт", "емайл", "емейл"),
    "count": ("count", "qty", "quantity", "колич", "сколько", "числ"),
    "first": ("first", "перв", "начал"),
    "last": ("last", "послед", "конеч"),
    "return": ("return", "get", "extract", "take", "верн", "получ", "возьми", "достань"),
    "increment": ("increment", "increase", "add", "plus", "увелич", "прибав", "инкрем"),
    "decrement": ("decrement", "decrease", "subtract", "minus", "уменьш", "убав", "выч"),
    "length": ("length", "len", "длин"),
    "retry": ("retry", "try", "tries", "attempt", "attempts", "попыт", "счетчик", "счётчик"),
    "string": ("string", "text", "строк", "текст"),
    "name": ("name", "title", "назв", "имя"),
    "total": ("total", "sum", "итог", "сумм"),
    "array": ("array", "arrays", "массив", "массивом", "массиве", "список", "list", "lists"),
    "normalize": ("normalize", "normalized", "normalization", "wrap", "wrapped", "ensure", "arrayify", "нормализ", "оберни", "обернуть", "приведи", "гарант"),
    "date": ("date", "datum", "дата", "дат", "day", "month", "year"),
    "time": ("time", "timestamp", "время", "тайм", "hour", "minute", "second"),
    "iso": ("iso", "8601"),
    "epoch": ("epoch", "unix", "юникс", "unixtime"),
    "remove_keys": ("remove", "delete", "drop", "clear", "clean", "очист", "удал", "убери", "выкин"),
}
STRUCTURED_OPERATION_ALLOWED_TYPES: dict[str, set[str]] = {
    "count": {"array_scalar", "array_object"},
    "first": {"array_scalar", "array_object"},
    "last": {"array_scalar", "array_object"},
    "return": {"scalar", "object", "array_scalar", "array_object"},
    "increment": {"scalar"},
    "decrement": {"scalar"},
    "string_length": {"scalar"},
}
NUMERIC_PATH_HINTS = ("count", "qty", "quantity", "total", "sum", "amount", "num", "number", "index", "retry", "try")
STRING_PATH_HINTS = ("name", "title", "text", "message", "label", "email", "mail", "description", "desc")
LOWCODE_CONSOLE_INPUT_PATTERNS = (
    (re.compile(r"\bio\.read\s*\("), "io.read"),
    (re.compile(r"\bio\.stdin\s*:\s*read\s*\("), "io.stdin:read"),
    (re.compile(r"\bio\.stdin:read\s*\("), "io.stdin:read"),
    (re.compile(r"\bprint\s*\("), "print"),
    (re.compile(r"\bio\.write\s*\("), "io.write"),
    (re.compile(r"\bio\.stdout\s*:\s*write\s*\("), "io.stdout:write"),
    (re.compile(r"\bio\.stdout:write\s*\("), "io.stdout:write"),
)
LOWCODE_CONSOLE_OUTPUT_PATTERNS = ()
_DELETE_KEYWORDS = (
    "remove", "delete", "drop", "clear", "clean",
    "убери", "удали", "удалить", "уберите", "выкинь", "очисти", "очистить", "очистка",
)
LUA_BAD_ARGUMENT_RE = re.compile(
    r"bad argument #(?P<arg>\d+) to ['`](?P<func>[^'`]+)['`] \((?P<expected>[^)]+?) expected, got (?P<got>[^)]+?)\)",
    re.IGNORECASE,
)
LUA_ATTEMPT_INDEX_NIL_RE = re.compile(r"attempt to index a nil value", re.IGNORECASE)
LUA_ATTEMPT_CALL_NIL_RE = re.compile(r"attempt to call a nil value", re.IGNORECASE)
LUA_ATTEMPT_ARITHMETIC_RE = re.compile(r"attempt to perform arithmetic on a (?P<got>[^ ]+) value", re.IGNORECASE)
LUA_ATTEMPT_COMPARE_RE = re.compile(r"attempt to compare (?P<left>[^ ]+) with (?P<right>[^ ]+)", re.IGNORECASE)
LUA_ATTEMPT_CONCAT_RE = re.compile(r"attempt to concatenate a (?P<got>[^ ]+) value", re.IGNORECASE)
LUA_UNEXPECTED_SYMBOL_RE = re.compile(r"unexpected symbol near ['`](?P<token>[^'`]+)['`]", re.IGNORECASE)
LUA_UNFINISHED_RE = re.compile(r"unfinished (?:string|long string|comment|long comment)", re.IGNORECASE)
RUNTIME_CONTEXT_START = "__TRUEHACK_CONTEXT_START__"
RUNTIME_CONTEXT_END = "__TRUEHACK_CONTEXT_END__"
RUNTIME_RESULT_START = "__TRUEHACK_RESULT_START__"
RUNTIME_RESULT_END = "__TRUEHACK_RESULT_END__"
RUNTIME_WORKFLOW_START = "__TRUEHACK_WORKFLOW_START__"
RUNTIME_WORKFLOW_END = "__TRUEHACK_WORKFLOW_END__"


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


def infer_runtime_fix_hints(run_error: str) -> list[str]:
    """Extract general-purpose repair hints from common Lua runtime errors."""
    text = str(run_error or "").strip()
    if not text:
        return []

    hints: list[str] = []

    bad_argument_match = LUA_BAD_ARGUMENT_RE.search(text)
    if bad_argument_match:
        func_name = bad_argument_match.group("func")
        arg_index = bad_argument_match.group("arg")
        expected = bad_argument_match.group("expected")
        got = bad_argument_match.group("got")
        hints.append(
            f"Function `{func_name}` expects argument #{arg_index} of type `{expected}`, but the code passes `{got}`."
        )
        hints.append(
            f"Before calling `{func_name}`, validate or convert the workflow value to the expected `{expected}` type instead of passing it through unchanged."
        )

    if LUA_ATTEMPT_INDEX_NIL_RE.search(text):
        hints.append(
            "A field/table access hit nil. Guard missing workflow fields before indexing them."
        )
    if LUA_ATTEMPT_CALL_NIL_RE.search(text):
        hints.append(
            "The code tries to call a nil value. Check that the function exists and was not overwritten by a variable."
        )

    unexpected_symbol_match = LUA_UNEXPECTED_SYMBOL_RE.search(text)
    if unexpected_symbol_match:
        token = unexpected_symbol_match.group("token")
        hints.append(
            f"Lua found an unexpected symbol near `{token}`. Check the tokens immediately before that point for leftover wrappers, missing keywords, separators, or malformed expressions."
        )
        hints.append(
            "If the file still contains `lua{...}lua`, markdown fences, JSON fragments, or other response-format wrappers, remove them so the file is plain standalone Lua."
        )

    if LUA_UNFINISHED_RE.search(text):
        hints.append(
            "The Lua source is syntactically incomplete. Check for unclosed strings, comments, tables, functions, or control-flow blocks."
        )

    arithmetic_match = LUA_ATTEMPT_ARITHMETIC_RE.search(text)
    if arithmetic_match:
        got = arithmetic_match.group("got")
        hints.append(
            f"Arithmetic is applied to `{got}`. Convert inputs with `tonumber(...)` or guard nil/non-numeric values before the operation."
        )

    compare_match = LUA_ATTEMPT_COMPARE_RE.search(text)
    if compare_match:
        left = compare_match.group("left")
        right = compare_match.group("right")
        hints.append(
            f"Comparison uses incompatible values (`{left}` vs `{right}`). Normalize types before comparing."
        )

    concat_match = LUA_ATTEMPT_CONCAT_RE.search(text)
    if concat_match:
        got = concat_match.group("got")
        hints.append(
            f"String concatenation uses `{got}`. Convert values with `tostring(...)` or guard nil before concatenation."
        )

    if "stack overflow" in text.lower():
        hints.append("The script recurses indefinitely or grows the call stack without a stop condition.")

    deduped: list[str] = []
    seen: set[str] = set()
    for hint in hints:
        if hint not in seen:
            seen.add(hint)
            deduped.append(hint)
    return deduped


def build_mock_init_value(name: str) -> str:
    """Build a conservative Lua literal for an inferred init variable name."""
    lowered = name.strip().lower()
    if not lowered:
        return '"sample"'

    if lowered == "datum":
        return '"20260410"'
    if lowered == "time":
        return '"123045"'
    if lowered == "date":
        return '"2026-04-10"'
    if any(token in lowered for token in ("timestamp", "datetime", "recall")):
        return '"2026-04-10T12:00:00+00:00"'
    if "time" in lowered:
        return '"2026-04-10T12:00:00+00:00"'
    if "date" in lowered:
        return '"2026-04-10"'
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


def extract_workflow_paths_from_text(text: str) -> dict[str, list[list[str]]]:
    """Collect explicit wf.vars/wf.initVariables paths mentioned in free-form text."""
    discovered: dict[str, list[list[str]]] = {"vars": [], "initVariables": []}
    seen: set[tuple[str, ...]] = set()

    for match in WF_ROOT_ACCESS_RE.finditer(str(text or "")):
        parsed = _parse_wf_expression(match.group(0))
        if not parsed:
            continue
        root, segments = parsed
        key = (root, *segments)
        if key in seen:
            continue
        seen.add(key)
        discovered[root].append(segments)

    return discovered


def flatten_lowcode_paths(paths: dict[str, list[list[str]]]) -> list[str]:
    """Render workflow paths into stable dotted strings."""
    flattened: list[str] = []
    seen: set[str] = set()
    for root in ("vars", "initVariables"):
        for segments in paths.get(root, []):
            dotted = ".".join([f"wf.{root}", *segments])
            if dotted in seen:
                continue
            seen.add(dotted)
            flattened.append(dotted)
    return flattened


def _extract_json_like_value(text: str) -> Any | None:
    cleaned = str(text or "").strip()
    if not cleaned:
        return None

    candidates = [cleaned]
    fenced = _strip_markdown_fence(cleaned)
    if fenced and fenced not in candidates:
        candidates.append(fenced)
    start = min(
        [index for index in (cleaned.find("{"), cleaned.find("[")) if index != -1],
        default=-1,
    )
    end_object = cleaned.rfind("}")
    end_array = cleaned.rfind("]")
    end = max(end_object, end_array)
    if start != -1 and end > start:
        candidates.append(cleaned[start : end + 1])

    wf_wrapped = _wrap_loose_workflow_fragment(cleaned)
    if wf_wrapped and wf_wrapped not in candidates:
        candidates.append(wf_wrapped)
    if fenced:
        wf_wrapped_fenced = _wrap_loose_workflow_fragment(fenced)
        if wf_wrapped_fenced and wf_wrapped_fenced not in candidates:
            candidates.append(wf_wrapped_fenced)

    for candidate in candidates:
        try:
            return json.loads(candidate)
        except Exception:
            continue
    return None


def _extract_balanced_json_block(text: str, start_index: int) -> str:
    if start_index < 0 or start_index >= len(text):
        return ""

    opening = text[start_index]
    closing = "}" if opening == "{" else "]" if opening == "[" else ""
    if not closing:
        return ""

    depth = 0
    in_string = False
    escaped = False
    for index in range(start_index, len(text)):
        char = text[index]
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue

        if char == '"':
            in_string = True
            continue
        if char == opening:
            depth += 1
        elif char == closing:
            depth -= 1
            if depth == 0:
                return text[start_index : index + 1]
    return ""


def _wrap_loose_workflow_fragment(text: str) -> str:
    cleaned = str(text or "").strip()
    if not cleaned:
        return ""

    wf_match = re.search(r'(?P<key>["\']?wf["\']?)\s*:', cleaned)
    if wf_match:
        brace_index = cleaned.find("{", wf_match.end())
        block = _extract_balanced_json_block(cleaned, brace_index)
        if block:
            return '{"wf": ' + block + "}"

    root_match = re.search(r'(?P<key>["\']?(?:vars|initVariables)["\']?)\s*:', cleaned)
    if root_match:
        brace_index = cleaned.find("{", root_match.end())
        block = _extract_balanced_json_block(cleaned, brace_index)
        if block:
            key_name = root_match.group("key").strip("\"'")
            return '{"wf": {"' + key_name + '": ' + block + "}}"

    return ""


def _compact_sample_value(value: Any, limit: int = 120) -> str:
    try:
        serialized = json.dumps(value, ensure_ascii=False)
    except Exception:
        serialized = repr(value)
    if len(serialized) <= limit:
        return serialized
    return f"{serialized[: limit - 3].rstrip()}..."


def _detect_inventory_value_type(value: Any) -> str:
    if isinstance(value, dict):
        return "object"
    if isinstance(value, list):
        first_non_null = next((item for item in value if item is not None), None)
        if isinstance(first_non_null, dict):
            return "array_object"
        return "array_scalar"
    return "scalar"


def _collect_array_item_keys(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    keys: set[str] = set()
    for item in value:
        if isinstance(item, dict):
            keys.update(str(key) for key in item.keys())
    return sorted(keys)


def build_workflow_path_inventory(context_value: Any) -> list[dict[str, Any]]:
    """Flatten parseable workflow JSON into a path inventory with types and samples."""
    entries: list[dict[str, Any]] = []

    def walk(root_name: str, value: Any, segments: list[str]) -> None:
        if not segments:
            if isinstance(value, dict):
                for child_key, child_value in value.items():
                    walk(root_name, child_value, [str(child_key)])
            return

        path = ".".join([f"wf.{root_name}", *segments])
        value_type = _detect_inventory_value_type(value)
        child_keys = sorted(str(key) for key in value.keys()) if isinstance(value, dict) else []
        entry = {
            "path": path,
            "root": root_name,
            "segments": list(segments),
            "type": value_type,
            "child_keys": child_keys,
            "item_keys": _collect_array_item_keys(value),
            "sample_value": value,
            "sample_preview": _compact_sample_value(value),
        }
        entries.append(entry)

        if isinstance(value, dict):
            for child_key, child_value in value.items():
                walk(root_name, child_value, [*segments, str(child_key)])

    if not isinstance(context_value, dict):
        return entries

    wf_section = context_value.get("wf") if isinstance(context_value.get("wf"), dict) else context_value
    if not isinstance(wf_section, dict):
        return entries

    for root_name in ("vars", "initVariables"):
        root_value = wf_section.get(root_name)
        if root_value is None:
            continue
        walk(root_name, root_value, [])

    return entries


def parse_lowcode_workflow_context(raw_context: str) -> dict[str, Any]:
    """Parse pasted workflow JSON-like context into an inventory the compiler can use."""
    parsed_value = _extract_json_like_value(raw_context)
    inventory = build_workflow_path_inventory(parsed_value)
    path_types = {entry["path"]: entry["type"] for entry in inventory}
    sample_values = {entry["path"]: entry["sample_preview"] for entry in inventory}
    return {
        "has_parseable_context": bool(inventory),
        "parsed_context": parsed_value,
        "workflow_path_inventory": inventory,
        "path_types": path_types,
        "sample_values": sample_values,
    }


def _extract_task_tokens(text: str) -> list[str]:
    return [token.lower() for token in TASK_TOKEN_RE.findall(str(text or ""))]


def _canonicalize_tokens(tokens: list[str]) -> set[str]:
    canonical: set[str] = set()
    for token in tokens:
        canonical.add(token)
        for name, stems in SEMANTIC_STEM_GROUPS.items():
            if any(stem in token for stem in stems):
                canonical.add(name)
    return canonical


def _entry_semantic_tokens(entry: dict[str, Any]) -> dict[str, set[str]]:
    segment_tokens = _extract_task_tokens(" ".join(entry.get("segments", [])))
    item_tokens = _extract_task_tokens(" ".join(entry.get("item_keys", [])))
    child_tokens = _extract_task_tokens(" ".join(entry.get("child_keys", [])))
    return {
        "segment_tokens": _canonicalize_tokens(segment_tokens),
        "item_tokens": _canonicalize_tokens(item_tokens),
        "child_tokens": _canonicalize_tokens(child_tokens),
    }


def extract_requested_item_keys(task_text: str, available_keys: list[str]) -> list[str]:
    """Infer object keys explicitly mentioned by the user from known available keys."""
    if not available_keys:
        return []

    normalized_text = str(task_text or "")
    task_tokens = {token.lower() for token in _extract_task_tokens(normalized_text)}
    requested: list[str] = []
    seen: set[str] = set()

    for key in available_keys:
        key_text = str(key).strip()
        if not key_text:
            continue
        key_lower = key_text.lower()
        key_tokens = {token.lower() for token in _extract_task_tokens(key_text)}
        if key_lower in normalized_text.lower() or (key_tokens and key_tokens.issubset(task_tokens)):
            if key_text not in seen:
                seen.add(key_text)
                requested.append(key_text)
    return requested


def infer_explicit_paths_from_bare_field_names(task_text: str, inventory: list[dict[str, Any]]) -> list[str]:
    """Map uniquely mentioned bare field names from the task to workflow paths."""
    if not inventory:
        return []

    task_tokens = {token.lower() for token in _extract_task_tokens(task_text)}
    if not task_tokens:
        return []

    field_to_paths: dict[str, set[str]] = {}
    for entry in inventory:
        path = str(entry.get("path", "")).strip()
        segments = [str(segment).strip() for segment in entry.get("segments", []) if str(segment).strip()]
        if not path or not segments:
            continue
        leaf = segments[-1].lower()
        field_to_paths.setdefault(leaf, set()).add(path)

    inferred: list[str] = []
    seen_paths: set[str] = set()
    for token in sorted(task_tokens):
        matches = field_to_paths.get(token, set())
        if len(matches) != 1:
            continue
        path = next(iter(matches))
        if path not in seen_paths:
            seen_paths.add(path)
            inferred.append(path)
    return inferred


def detect_lowcode_operation(task_text: str) -> dict[str, Any]:
    """Detect the dominant simple operation requested by the user."""
    tokens = _canonicalize_tokens(_extract_task_tokens(task_text))
    numbers = [int(match) for match in re.findall(r"\b\d+\b", str(task_text or ""))]

    if "remove_keys" in tokens:
        return {"operation": "remove_keys", "argument": None}
    if "increment" in tokens:
        return {"operation": "increment", "argument": numbers[0] if numbers else 1}
    if "decrement" in tokens:
        return {"operation": "decrement", "argument": numbers[0] if numbers else 1}
    if "count" in tokens:
        return {"operation": "count", "argument": None}
    if "first" in tokens:
        return {"operation": "first", "argument": None}
    if "last" in tokens:
        return {"operation": "last", "argument": None}
    if "length" in tokens and "string" in tokens:
        return {"operation": "string_length", "argument": None}
    if "return" in tokens:
        return {"operation": "return", "argument": None}
    return {"operation": "llm", "argument": None}


def infer_semantic_expectations(
    task_text: str,
    *,
    selected_primary_type: str = "",
) -> list[str]:
    """Infer high-level semantic expectations for verification and prompting."""
    tokens = _canonicalize_tokens(_extract_task_tokens(task_text))
    expectations: list[str] = []

    if "array" in tokens and "normalize" in tokens:
        expectations.append("array_normalization")

    if ("date" in tokens or "time" in tokens) and "iso" in tokens:
        expectations.append("datetime_to_iso")
    if ("date" in tokens or "time" in tokens) and "epoch" in tokens:
        expectations.append("datetime_to_epoch")
    if ("total" in tokens or "sum" in tokens or "increment" in tokens) and selected_primary_type in {"array_object", "array_scalar"}:
        expectations.append("numeric_aggregation")

    # If the task explicitly talks about arrays but the selected value is not clearly an array already,
    # preserve the expectation for downstream guards even when operation remains generic `llm`.
    if (
        "array" in tokens
        and "array_normalization" not in expectations
        and selected_primary_type in {"scalar", "object"}
    ):
        expectations.append("array_normalization")

    deduped: list[str] = []
    seen: set[str] = set()
    for expectation in expectations:
        if expectation not in seen:
            seen.add(expectation)
            deduped.append(expectation)
    return deduped


def _path_looks_numeric(entry: dict[str, Any]) -> bool:
    sample_value = entry.get("sample_value")
    if isinstance(sample_value, (int, float)) and not isinstance(sample_value, bool):
        return True
    path_lower = str(entry.get("path", "")).lower()
    return any(hint in path_lower for hint in NUMERIC_PATH_HINTS)


def _path_looks_string(entry: dict[str, Any]) -> bool:
    sample_value = entry.get("sample_value")
    if isinstance(sample_value, str):
        return True
    path_lower = str(entry.get("path", "")).lower()
    return any(hint in path_lower for hint in STRING_PATH_HINTS)


def rank_workflow_paths(
    task_text: str,
    inventory: list[dict[str, Any]],
    operation: str,
    explicit_paths: list[str] | None = None,
) -> list[dict[str, Any]]:
    """Rank context-derived workflow paths against the current task."""
    task_tokens_raw = _extract_task_tokens(task_text)
    task_tokens = _canonicalize_tokens(task_tokens_raw)
    explicit = set(explicit_paths or [])
    ranked: list[dict[str, Any]] = []

    for entry in inventory:
        path = str(entry.get("path", ""))
        if not path:
            continue
        path_type = str(entry.get("type", ""))
        tokens = _entry_semantic_tokens(entry)
        segment_overlap = len(task_tokens & tokens["segment_tokens"])
        item_overlap = len(task_tokens & tokens["item_tokens"])
        child_overlap = len(task_tokens & tokens["child_tokens"])
        score = (segment_overlap * 4) + (item_overlap * 3) + (child_overlap * 2)

        if path in explicit:
            score += 100
        elif any(path.startswith(f"{candidate}.") for candidate in explicit):
            score += 20

        if operation in STRUCTURED_OPERATION_ALLOWED_TYPES:
            allowed_types = STRUCTURED_OPERATION_ALLOWED_TYPES[operation]
            if path_type not in allowed_types:
                continue
            score += 10
            if operation in {"increment", "decrement"} and not _path_looks_numeric(entry):
                score -= 6
            if operation == "string_length" and not _path_looks_string(entry):
                score -= 6

        if operation == "remove_keys":
            if path_type not in {"object", "array_object"}:
                continue
            if item_overlap or child_overlap:
                score += 10

        if operation == "return" and path_type == "scalar":
            score += 4
        if operation == "count" and path.endswith(".items"):
            score += 3

        ranked.append(
            {
                "path": path,
                "score": score,
                "type": path_type,
                "sample_preview": entry.get("sample_preview", ""),
            }
        )

    ranked.sort(key=lambda item: (-float(item["score"]), item["path"]))
    return ranked


def compile_lowcode_request(
    *,
    task_text: str,
    raw_context: str = "",
    clarification_text: str = "",
) -> dict[str, Any]:
    """Compile task text plus pasted workflow context into a structured request."""
    parsed_context = parse_lowcode_workflow_context(raw_context)
    inventory = parsed_context["workflow_path_inventory"]
    merged_text = "\n".join(part for part in (task_text.strip(), clarification_text.strip()) if part.strip())
    operation_info = detect_lowcode_operation(merged_text)
    operation = str(operation_info.get("operation", "llm"))
    explicit_paths_raw = flatten_lowcode_paths(extract_workflow_paths_from_text(merged_text))
    inferred_explicit_paths = infer_explicit_paths_from_bare_field_names(merged_text, inventory)
    explicit_paths = list(dict.fromkeys([*explicit_paths_raw, *inferred_explicit_paths]))
    ranked_candidates = rank_workflow_paths(merged_text, inventory, operation, explicit_paths)

    selected_primary_path = ""
    selected_save_path = ""
    selected_primary_type = ""
    requested_item_keys: list[str] = []
    needs_clarification = False
    clarification_candidates = ranked_candidates[:3]
    confidence = 0.0

    inventory_paths = {entry["path"] for entry in inventory}
    explicit_exact_raw = [path for path in explicit_paths_raw if path in inventory_paths]
    explicit_exact = [path for path in explicit_paths if path in inventory_paths]
    if len(explicit_exact_raw) == 1:
        selected_primary_path = explicit_exact_raw[0]
        confidence = 1.0
    elif len(explicit_exact_raw) > 1:
        needs_clarification = True
    elif len(explicit_exact) == 1:
        selected_primary_path = explicit_exact[0]
        confidence = 1.0
    elif len(explicit_exact) > 1:
        needs_clarification = True
    elif ranked_candidates:
        top = ranked_candidates[0]
        second = ranked_candidates[1] if len(ranked_candidates) > 1 else None
        confidence = min(1.0, float(top["score"]) / 20.0)
        if float(top["score"]) < 10:
            needs_clarification = operation in STRUCTURED_OPERATION_ALLOWED_TYPES and parsed_context["has_parseable_context"]
        elif second and float(top["score"]) - float(second["score"]) < 3:
            needs_clarification = operation in STRUCTURED_OPERATION_ALLOWED_TYPES and parsed_context["has_parseable_context"]
        else:
            selected_primary_path = str(top["path"])
            selected_primary_type = str(top.get("type", "") or "")

    if selected_primary_path:
        selected_entry = next((entry for entry in inventory if entry.get("path") == selected_primary_path), None)
        if isinstance(selected_entry, dict):
            if not selected_primary_type:
                selected_primary_type = str(selected_entry.get("type", "") or "")
            candidate_keys = []
            if selected_primary_type == "array_object":
                candidate_keys = [str(key) for key in selected_entry.get("item_keys", [])]
            elif selected_primary_type == "object":
                candidate_keys = [str(key) for key in selected_entry.get("child_keys", [])]
            requested_item_keys = extract_requested_item_keys(merged_text, candidate_keys)

    if request_explicitly_saves_to_workflow(merged_text):
        save_candidates = [
            path for path in explicit_paths
            if path.startswith("wf.vars.") and path not in inventory_paths and path != selected_primary_path
        ]
        if len(save_candidates) == 1:
            selected_save_path = save_candidates[0]

    semantic_expectations = infer_semantic_expectations(
        merged_text,
        selected_primary_type=selected_primary_type,
    )

    clarifying_question = ""
    if needs_clarification:
        option_text = ", ".join(candidate["path"] for candidate in clarification_candidates) or "wf.vars.<path>"
        clarifying_question = (
            "Уточни, с каким workflow-путём работать: "
            f"{option_text}. Ответь, например: `используй {clarification_candidates[0]['path']}`."
            if clarification_candidates
            else "Уточни, с каким workflow-путём работать. Ответь явным путём вида `используй wf.vars.some.path`."
        )

    return {
        "task_text": task_text.strip(),
        "raw_context": raw_context.strip(),
        "clarification_text": clarification_text.strip(),
        "parsed_context": parsed_context["parsed_context"],
        "workflow_path_inventory": inventory,
        "path_types": parsed_context["path_types"],
        "sample_values": parsed_context["sample_values"],
        "selected_operation": operation,
        "operation_argument": operation_info.get("argument"),
        "selected_primary_path": selected_primary_path,
        "selected_primary_type": selected_primary_type,
        "selected_save_path": selected_save_path,
        "requested_item_keys": requested_item_keys,
        "candidate_paths_ranked": clarification_candidates,
        "confidence": confidence,
        "needs_clarification": needs_clarification,
        "clarifying_question": clarifying_question,
        "has_parseable_context": parsed_context["has_parseable_context"],
        "semantic_expectations": semantic_expectations,
        "explicit_paths": explicit_paths,
        "explicit_paths_raw": explicit_paths_raw,
        "inferred_explicit_paths": inferred_explicit_paths,
    }


def has_workflow_context(text: str) -> bool:
    """Detect whether the prompt likely contains workflow context."""
    cleaned = str(text or "")
    return bool(WORKFLOW_CONTEXT_HINT_RE.search(cleaned) and WORKFLOW_ROOT_HINT_RE.search(cleaned))


def _workflow_path_matches(expected: str, actual: str) -> bool:
    return actual == expected or actual.startswith(f"{expected}.")


def _tail_non_comment_lines(lua_code: str, limit: int = 6) -> list[str]:
    lines = [
        line.strip()
        for line in str(lua_code or "").splitlines()
        if line.strip() and not line.strip().startswith("--")
    ]
    return lines[-limit:]


def has_direct_return(lua_code: str) -> bool:
    """Check whether the last executable lines contain a direct return."""
    return any(line.startswith("return") for line in _tail_non_comment_lines(lua_code))


def request_explicitly_saves_to_workflow(prompt: str) -> bool:
    """Heuristic: detect prompts that explicitly ask to update workflow state."""
    lowered = str(prompt or "").lower()
    save_markers = ("save", "update", "set", "assign", "store", "write", "сохрани", "запиши", "обнови", "установи", "присвой")
    return "wf.vars" in lowered and any(marker in lowered for marker in save_markers)


def build_mock_leaf_value(path_segments: list[str]) -> str:
    """Choose a conservative Lua literal for the leaf of a mocked path."""
    if not path_segments:
        return "{}"
    return build_mock_init_value(path_segments[-1])


def _extract_workflow_root_tables(workflow_context: Any | None) -> tuple[dict[str, Any], dict[str, Any]]:
    """Extract wf.vars / wf.initVariables tables from parsed workflow context."""
    if not isinstance(workflow_context, dict):
        return {}, {}

    wf_section = workflow_context.get("wf")
    root = wf_section if isinstance(wf_section, dict) else workflow_context
    if not isinstance(root, dict):
        return {}, {}

    vars_value = root.get("vars")
    init_value = root.get("initVariables")
    return (
        vars_value if isinstance(vars_value, dict) else {},
        init_value if isinstance(init_value, dict) else {},
    )


def _python_to_lua_literal(value: Any) -> str:
    """Serialize a JSON-like Python value into a Lua literal."""
    if value is None:
        return "nil"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return repr(value)
    if isinstance(value, str):
        return json.dumps(value, ensure_ascii=False)
    if isinstance(value, list):
        items = ", ".join(_python_to_lua_literal(item) for item in value)
        return f"{{{items}}}"
    if isinstance(value, dict):
        parts: list[str] = []
        for key in sorted(value.keys(), key=lambda item: str(item)):
            rendered_key = _python_to_lua_literal(str(key))
            rendered_value = _python_to_lua_literal(value[key])
            parts.append(f"[{rendered_key}] = {rendered_value}")
        return "{" + ", ".join(parts) + "}"
    return json.dumps(str(value), ensure_ascii=False)


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


def _extract_marked_block(text: str, start_marker: str, end_marker: str) -> tuple[str, str]:
    pattern = re.compile(
        re.escape(start_marker) + r"\r?\n(?P<body>.*?)\r?\n" + re.escape(end_marker),
        re.DOTALL,
    )
    source = str(text or "")
    match = pattern.search(source)
    if not match:
        return source, ""
    cleaned_output = (source[: match.start()] + source[match.end() :]).strip()
    return cleaned_output, str(match.group("body") or "")


def _extract_runtime_context(run_output: str) -> tuple[str, dict[str, Any]]:
    """Strip structured runtime context markers from stderr and parse them."""
    cleaned_output, raw_block = _extract_marked_block(
        run_output,
        RUNTIME_CONTEXT_START,
        RUNTIME_CONTEXT_END,
    )
    if not raw_block:
        return str(run_output or ""), {}
    runtime_context: dict[str, Any] = {"locals": []}

    for raw_line in raw_block.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if line.startswith("__TRUEHACK_FRAME__\t"):
            parts = line.split("\t", 3)
            if len(parts) >= 4:
                try:
                    runtime_context["line"] = int(parts[1])
                except (TypeError, ValueError):
                    runtime_context["line"] = 0
                runtime_context["function"] = parts[2]
                runtime_context["source"] = parts[3]
            continue
        if line.startswith("__TRUEHACK_LOCAL__\t"):
            parts = line.split("\t", 3)
            if len(parts) >= 4:
                runtime_context.setdefault("locals", []).append(
                    {
                        "name": parts[1],
                        "type": parts[2],
                        "value": parts[3],
                    }
                )

    if not runtime_context.get("locals") and not runtime_context.get("line"):
        return cleaned_output, {}
    return cleaned_output, runtime_context


def _extract_runtime_result(run_output: str) -> tuple[str, Any | None, str]:
    """Strip structured runtime-result markers and parse the serialized return value."""
    cleaned_output, raw_block = _extract_marked_block(
        run_output,
        RUNTIME_RESULT_START,
        RUNTIME_RESULT_END,
    )
    if not raw_block:
        return str(run_output or ""), None, ""
    raw_block = raw_block.strip()
    parsed_value = _extract_json_like_value(raw_block)
    if parsed_value is None and raw_block:
        try:
            parsed_value = json.loads(raw_block)
        except Exception:
            parsed_value = raw_block
    return cleaned_output, parsed_value, raw_block


def _extract_runtime_workflow_state(run_output: str) -> tuple[str, Any | None, str]:
    """Strip serialized workflow-state markers and parse the wf snapshot."""
    cleaned_output, raw_block = _extract_marked_block(
        run_output,
        RUNTIME_WORKFLOW_START,
        RUNTIME_WORKFLOW_END,
    )
    if not raw_block:
        return str(run_output or ""), None, ""
    raw_block = raw_block.strip()
    parsed_value = _extract_json_like_value(raw_block)
    if parsed_value is None and raw_block:
        try:
            parsed_value = json.loads(raw_block)
        except Exception:
            parsed_value = raw_block
    return cleaned_output, parsed_value, raw_block


def build_lowcode_validation_harness(
    lua_file: str,
    lua_code: str,
    workflow_context: Any | None = None,
) -> tuple[str, dict[str, list[str]]]:
    """Build a temporary harness that injects workflow context plus LowCode mocks around the user file."""
    access_paths = collect_lowcode_access_paths(lua_code)
    provided_vars, provided_init_variables = _extract_workflow_root_tables(workflow_context)
    assignment_lines = [
        *build_mock_assignment_lines("vars", access_paths["vars"]),
        *build_mock_assignment_lines("initVariables", access_paths["initVariables"]),
    ]
    assignments_block = "\n".join(assignment_lines)
    if assignments_block:
        assignments_block = f"{assignments_block}\n"

    user_path = to_cmd_path(lua_file)
    user_path_literal = json.dumps(user_path)
    provided_vars_literal = _python_to_lua_literal(provided_vars)
    provided_init_literal = _python_to_lua_literal(provided_init_variables)
    harness = (
        "wf = wf or {}\n"
        "wf.vars = wf.vars or {}\n"
        "wf.initVariables = wf.initVariables or {}\n"
        f"wf.vars = {provided_vars_literal}\n"
        f"wf.initVariables = {provided_init_literal}\n"
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
        "local function _json_escape_runtime(value)\n"
        "    local escaped = tostring(value)\n"
        "    escaped = escaped:gsub('\\\\', '\\\\\\\\')\n"
        "    escaped = escaped:gsub('\"', '\\\\\"')\n"
        "    escaped = escaped:gsub('\\n', '\\\\n')\n"
        "    escaped = escaped:gsub('\\r', '\\\\r')\n"
        "    escaped = escaped:gsub('\\t', '\\\\t')\n"
        "    return escaped\n"
        "end\n"
        "local function _is_runtime_array(value)\n"
        "    if type(value) ~= 'table' then\n"
        "        return false\n"
        "    end\n"
        "    local count = 0\n"
        "    for key, _ in pairs(value) do\n"
        "        if type(key) ~= 'number' or key < 1 or key ~= math.floor(key) then\n"
        "            return false\n"
        "        end\n"
        "        count = count + 1\n"
        "    end\n"
        "    for index = 1, count do\n"
        "        if value[index] == nil then\n"
        "            return false\n"
        "        end\n"
        "    end\n"
        "    return true\n"
        "end\n"
        "local function _serialize_runtime_result(value, depth, seen)\n"
        "    depth = depth or 0\n"
        "    seen = seen or {}\n"
        "    local value_type = type(value)\n"
        "    if value_type == 'nil' then\n"
        "        return 'null'\n"
        "    end\n"
        "    if value_type == 'boolean' then\n"
        "        return value and 'true' or 'false'\n"
        "    end\n"
        "    if value_type == 'number' then\n"
        "        return tostring(value)\n"
        "    end\n"
        "    if value_type == 'string' then\n"
        "        return '\"' .. _json_escape_runtime(value) .. '\"'\n"
        "    end\n"
        "    if value_type ~= 'table' then\n"
        "        return '\"<' .. value_type .. '>\"'\n"
        "    end\n"
        "    if seen[value] then\n"
        "        return '\"<cycle>\"'\n"
        "    end\n"
        "    if depth >= 5 then\n"
        "        return '\"<max_depth>\"'\n"
        "    end\n"
        "    seen[value] = true\n"
        "    if _is_runtime_array(value) then\n"
        "        local parts = {}\n"
        "        for index = 1, #value do\n"
        "            parts[#parts + 1] = _serialize_runtime_result(value[index], depth + 1, seen)\n"
        "        end\n"
        "        seen[value] = nil\n"
        "        return '[' .. table.concat(parts, ',') .. ']'\n"
        "    end\n"
        "    local keys = {}\n"
        "    for key, _ in pairs(value) do\n"
        "        keys[#keys + 1] = tostring(key)\n"
        "    end\n"
        "    table.sort(keys)\n"
        "    local parts = {}\n"
        "    for _, key in ipairs(keys) do\n"
        "        parts[#parts + 1] = '\"' .. _json_escape_runtime(key) .. '\":' .. _serialize_runtime_result(value[key], depth + 1, seen)\n"
        "    end\n"
        "    seen[value] = nil\n"
        "    return '{' .. table.concat(parts, ',') .. '}'\n"
        "end\n"
        f"{assignments_block}"
        "local function _safe_runtime_value(value)\n"
        "    local value_type = type(value)\n"
        "    if value_type == 'nil' then\n"
        "        return 'nil'\n"
        "    end\n"
        "    if value_type == 'table' then\n"
        "        local parts = {}\n"
        "        local count = 0\n"
        "        for key, item in pairs(value) do\n"
        "            count = count + 1\n"
        "            if count > 3 then\n"
        "                break\n"
        "            end\n"
        "            parts[#parts + 1] = tostring(key) .. '=' .. tostring(item)\n"
        "        end\n"
        "        if count == 0 then\n"
        "            return '{}'\n"
        "        end\n"
        "        if count > 3 then\n"
        "            parts[#parts + 1] = '...'\n"
        "        end\n"
        "        return '{' .. table.concat(parts, ', ') .. '}'\n"
        "    end\n"
        "    local ok, rendered = pcall(function()\n"
        "        return tostring(value)\n"
        "    end)\n"
        "    if ok then\n"
        "        return rendered\n"
        "    end\n"
        "    return '<' .. value_type .. '>'\n"
        "end\n"
        "local function _sanitize_runtime_value(value)\n"
        "    local text = _safe_runtime_value(value)\n"
        "    text = string.gsub(text, '\\r', ' ')\n"
        "    text = string.gsub(text, '\\n', ' ')\n"
        "    text = string.gsub(text, '\\t', ' ')\n"
        "    return text\n"
        "end\n"
        "local function _emit_runtime_context()\n"
        "    if not debug then\n"
        "        return\n"
        "    end\n"
        "    local target_level = nil\n"
        "    local target_info = nil\n"
        "    local level = 2\n"
        "    while true do\n"
        "        local info = debug.getinfo(level, 'Sln')\n"
        "        if info == nil then\n"
        "            break\n"
        "        end\n"
        f"        if info.source == '@' .. {user_path_literal} then\n"
        "            target_level = level\n"
        "            target_info = info\n"
        "            break\n"
        "        end\n"
        "        level = level + 1\n"
        "    end\n"
        "    if target_level == nil or target_info == nil then\n"
        "        return\n"
        "    end\n"
        f"    io.stderr:write('{RUNTIME_CONTEXT_START}\\n')\n"
        "    io.stderr:write('__TRUEHACK_FRAME__\\t' .. tostring(target_info.currentline or 0) .. '\\t' .. tostring(target_info.name or '') .. '\\t' .. tostring(target_info.source or '') .. '\\n')\n"
        "    local index = 1\n"
        "    while true do\n"
        "        local name, value = debug.getlocal(target_level, index)\n"
        "        if name == nil then\n"
        "            break\n"
        "        end\n"
        "        if name ~= '(*temporary)' then\n"
        "            io.stderr:write('__TRUEHACK_LOCAL__\\t' .. tostring(name) .. '\\t' .. type(value) .. '\\t' .. _sanitize_runtime_value(value) .. '\\n')\n"
        "        end\n"
        "        index = index + 1\n"
        "    end\n"
        f"    io.stderr:write('{RUNTIME_CONTEXT_END}\\n')\n"
        "end\n"
        "local function _traceback(err)\n"
        "    _emit_runtime_context()\n"
        "    if debug and debug.traceback then\n"
        "        return debug.traceback(err, 2)\n"
        "    end\n"
        "    return tostring(err)\n"
        "end\n"
        "local ok, result = xpcall(function()\n"
        f"    return dofile({user_path_literal})\n"
        "end, _traceback)\n"
        "if not ok then\n"
        "    io.stderr:write(tostring(result))\n"
        "    io.stderr:write('\\n')\n"
        "    os.exit(1)\n"
        "end\n"
        f"io.stderr:write('{RUNTIME_RESULT_START}\\n')\n"
        "io.stderr:write(_serialize_runtime_result(result, 0, {}))\n"
        "io.stderr:write('\\n')\n"
        f"io.stderr:write('{RUNTIME_RESULT_END}\\n')\n"
        f"io.stderr:write('{RUNTIME_WORKFLOW_START}\\n')\n"
        "io.stderr:write(_serialize_runtime_result(wf, 0, {}))\n"
        "io.stderr:write('\\n')\n"
        f"io.stderr:write('{RUNTIME_WORKFLOW_END}\\n')\n"
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
    workflow_context: Any | None = None,
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
    runtime_fix_hints: list[str] = []
    runtime_context: dict[str, Any] = {}
    result_value: Any | None = None
    result_preview = ""
    workflow_state: Any | None = None
    workflow_state_preview = ""

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
            "runtime_fix_hints": [],
            "runtime_context": {},
            "result_value": None,
            "result_preview": "",
            "workflow_state": None,
            "workflow_state_preview": "",
            "luacheck_output": "",
            "luacheck_error": "",
            "luacheck_warning": "",
            "failure_kind": "contract",
        }
        return diagnostics

    try:
        harness_code, mocked_paths = build_lowcode_validation_harness(
            lua_file,
            lua_code,
            workflow_context=workflow_context,
        )
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
        run_output, result_value, result_preview = _extract_runtime_result(run_output)
        run_output, workflow_state, workflow_state_preview = _extract_runtime_workflow_state(run_output)
        run_output, runtime_context = _extract_runtime_context(run_output)
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

    runtime_fix_hints = infer_runtime_fix_hints(run_error)

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
        "runtime_fix_hints": runtime_fix_hints,
        "runtime_context": runtime_context,
        "result_value": result_value,
        "result_preview": result_preview,
        "workflow_state": workflow_state,
        "workflow_state_preview": workflow_state_preview,
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
    workflow_context: Any | None = None,
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
                workflow_context,
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


def _strip_markdown_fence(text: str) -> str:
    """Remove a surrounding markdown fence while preserving malformed wrapper payloads."""
    cleaned = str(text or "").strip()
    if not cleaned.startswith("```") or not cleaned.endswith("```"):
        return cleaned

    inner = cleaned[3:-3].strip()
    if not inner:
        return ""

    info_match = re.match(r"^(?P<info>[A-Za-z0-9_-]+)[ \t]*\r?\n(?P<body>[\s\S]*)$", inner)
    if info_match:
        return str(info_match.group("body") or "").strip()

    if inner.startswith("\n") or inner.startswith("\r"):
        return inner.lstrip("\r\n").strip()

    return inner


def unwrap_lowcode_jsonstring(text: str) -> str:
    """Extract Lua body from the LowCode JsonString wrapper when present."""
    cleaned = str(text or "").strip()
    previous = None
    while cleaned and cleaned != previous:
        previous = cleaned
        match = LOWCODE_JSONSTRING_PATTERN.fullmatch(cleaned)
        if not match:
            break
        cleaned = match.group(1).strip()
    return cleaned or str(text or "")


def format_lowcode_jsonstring(lua_code: str) -> str:
    """Render plain Lua code into the LowCode JsonString wrapper."""
    return f"{LOWCODE_JSONSTRING_OPEN}\n{lua_code.strip()}\n{LOWCODE_JSONSTRING_CLOSE}"


def _normalize_payload_field_name(name: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9_]+", "_", str(name or "").strip())
    cleaned = re.sub(r"_+", "_", cleaned).strip("_")
    if not cleaned:
        return "script"
    if cleaned[0].isdigit():
        cleaned = f"field_{cleaned}"
    return cleaned[:64] or "script"


def _workflow_path_leaf(path: str) -> str:
    parts = [part.strip() for part in str(path or "").split(".") if part.strip()]
    if len(parts) <= 2:
        return ""
    return parts[-1]


def suggest_json_payload_field_name(
    *,
    compiled_request: dict[str, Any] | None = None,
    target_path: str = "",
) -> str:
    """Choose a stable JSON field name for the exported JsonString payload."""
    request = compiled_request if isinstance(compiled_request, dict) else {}

    save_leaf = _workflow_path_leaf(str(request.get("selected_save_path", "") or ""))
    if save_leaf:
        return _normalize_payload_field_name(save_leaf)

    primary_leaf = _workflow_path_leaf(str(request.get("selected_primary_path", "") or ""))
    if primary_leaf:
        return _normalize_payload_field_name(primary_leaf)

    explicit_paths = [
        str(path).strip()
        for path in request.get("explicit_paths_raw", [])
        if str(path).strip()
    ]
    if len(explicit_paths) == 1:
        explicit_leaf = _workflow_path_leaf(explicit_paths[0])
        if explicit_leaf:
            return _normalize_payload_field_name(explicit_leaf)

    if target_path:
        stem = os.path.splitext(os.path.basename(target_path))[0]
        if stem:
            return _normalize_payload_field_name(stem)

    return "script"


def format_lowcode_json_payload(
    lua_code: str,
    *,
    compiled_request: dict[str, Any] | None = None,
    target_path: str = "",
) -> str:
    """Render the user-facing JsonString artifact as a JSON object with one code-bearing field."""
    field_name = suggest_json_payload_field_name(
        compiled_request=compiled_request,
        target_path=target_path,
    )
    wrapped = format_lowcode_jsonstring(lua_code).replace("\n", "\r\n")
    return json.dumps({field_name: wrapped}, ensure_ascii=False)


def _looks_like_lua_source(text: str) -> bool:
    cleaned = ZERO_WIDTH_PATTERN.sub("", str(text or "")).replace("\r\n", "\n").replace("\r", "\n").strip()
    if not cleaned:
        return False
    non_empty_lines = [line.strip() for line in cleaned.splitlines() if line.strip()]
    first_line = non_empty_lines[0] if non_empty_lines else ""
    return bool(PROBABLE_LUA_LINE_PATTERN.match(first_line) or LUA_SIGNAL_PATTERN.search(cleaned[:2000]))


def _extract_lua_payload_from_json_like(value: Any, depth: int = 0) -> str:
    """Extract embedded Lua source from JSON-like model responses."""
    if depth > 5 or value is None:
        return ""

    if isinstance(value, str):
        normalized_value = (
            str(value)
            .replace("\r\n", "\n")
            .replace("\r", "\n")
            .replace("\\r\\n", "\n")
            .replace("\\n", "\n")
            .replace("\\r", "\n")
        )
        cleaned = unwrap_lowcode_jsonstring(normalized_value).strip()
        if not cleaned:
            return ""
        nested = _extract_json_like_value(cleaned)
        if nested is not None and nested != value:
            extracted = _extract_lua_payload_from_json_like(nested, depth + 1)
            if extracted:
                return extracted
        return cleaned if _looks_like_lua_source(cleaned) else ""

    if isinstance(value, dict):
        for key in JSON_LUA_PAYLOAD_KEYS:
            if key in value:
                extracted = _extract_lua_payload_from_json_like(value.get(key), depth + 1)
                if extracted:
                    return extracted
        for nested_value in value.values():
            extracted = _extract_lua_payload_from_json_like(nested_value, depth + 1)
            if extracted:
                return extracted
        return ""

    if isinstance(value, list):
        for item in value:
            extracted = _extract_lua_payload_from_json_like(item, depth + 1)
            if extracted:
                return extracted
        return ""

    return ""


def extract_embedded_lua_payload(text: str) -> str:
    """Extract Lua from JSON/object envelopes if the model wraps code in structured data."""
    candidates = [str(text or "").strip()]
    unwrapped = unwrap_lowcode_jsonstring(str(text or "").strip())
    if unwrapped and unwrapped not in candidates:
        candidates.append(unwrapped)

    for candidate in candidates:
        parsed = _extract_json_like_value(candidate)
        if parsed is None:
            continue
        extracted = _extract_lua_payload_from_json_like(parsed)
        if extracted:
            return extracted
    return ""


def _unwrap_malformed_lowcode_wrapper(text: str) -> str:
    """Recover Lua body from common wrapper fragments left by malformed fenced output."""
    cleaned = str(text or "").strip()
    if not cleaned:
        return ""

    malformed_patterns = (
        (r"^\{\s*([\s\S]*?)\s*\}lua$", 1),
        (r"^lua\{\s*([\s\S]*?)\s*\}$", 1),
    )
    for pattern, group_index in malformed_patterns:
        match = re.match(pattern, cleaned, re.IGNORECASE)
        if not match:
            continue
        candidate = str(match.group(group_index) or "").strip()
        if candidate and _looks_like_lua_source(candidate):
            return candidate
    return cleaned


def normalize_lua_code(text: str) -> str:
    """Normalize model output into a standalone Lua file."""
    cleaned = ZERO_WIDTH_PATTERN.sub("", text).replace("\r\n", "\n").replace("\r", "\n").strip()
    previous = None
    for _ in range(8):
        if not cleaned or cleaned == previous:
            break
        previous = cleaned

        fenced = _strip_markdown_fence(cleaned)
        if fenced != cleaned:
            cleaned = fenced
            continue

        embedded_payload = extract_embedded_lua_payload(cleaned)
        if embedded_payload and embedded_payload != cleaned:
            cleaned = embedded_payload.strip()
            continue

        unwrapped = unwrap_lowcode_jsonstring(cleaned)
        if unwrapped != cleaned:
            cleaned = unwrapped.strip()
            continue

        malformed = _unwrap_malformed_lowcode_wrapper(cleaned)
        if malformed != cleaned:
            cleaned = malformed.strip()
            continue

        stripped = strip_explanatory_preamble(cleaned)
        if stripped != cleaned:
            cleaned = stripped.strip()
            continue

    return cleaned.strip()


def is_truncated_lowcode_response(raw: str) -> bool:
    """Return True when the model started the lua{...}lua wrapper but was cut off before }lua.

    Distinguishes truncation from other format failures (fenced, prose, etc.).
    """
    cleaned = ZERO_WIDTH_PATTERN.sub("", str(raw or "")).replace("\r\n", "\n").replace("\r", "\n").strip()
    lower = cleaned.lower()
    return lower.startswith(LOWCODE_JSONSTRING_OPEN.lower()) and not lower.endswith(LOWCODE_JSONSTRING_CLOSE.lower())


def validate_lowcode_llm_output(raw_response: str) -> dict[str, Any]:
    """Validate raw model output against the strict LowCode wrapper contract."""
    analysis = analyze_lua_response(raw_response)
    raw_cleaned = ZERO_WIDTH_PATTERN.sub("", str(raw_response or "")).replace("\r\n", "\n").replace("\r", "\n").strip()
    normalized = str(analysis.get("normalized", "") or "").strip()

    if not raw_cleaned:
        analysis["valid"] = False
        analysis["reason"] = "Model returned an empty response instead of Lua code."
        return analysis

    if raw_cleaned.startswith("```"):
        if normalized.startswith(LOWCODE_JSONSTRING_OPEN) and normalized.endswith(LOWCODE_JSONSTRING_CLOSE):
            analysis["valid"] = True
            analysis["reason"] = ""
            analysis["normalized"] = normalized
            return analysis
        analysis["valid"] = False
        analysis["reason"] = "Response must start with lua{ and end with }lua without markdown code fences."
        return analysis

    if (
        (raw_cleaned.startswith('"') and raw_cleaned.endswith('"'))
        or (raw_cleaned.startswith("'") and raw_cleaned.endswith("'"))
    ):
        if normalized.startswith(LOWCODE_JSONSTRING_OPEN) and normalized.endswith(LOWCODE_JSONSTRING_CLOSE):
            analysis["valid"] = True
            analysis["reason"] = ""
            analysis["normalized"] = normalized
            return analysis
        analysis["valid"] = False
        analysis["reason"] = "Response must start with lua{ and end with }lua without surrounding quotes."
        return analysis

    if not raw_cleaned.startswith(LOWCODE_JSONSTRING_OPEN) or not raw_cleaned.endswith(LOWCODE_JSONSTRING_CLOSE):
        analysis["valid"] = False
        analysis["reason"] = "Response must start with lua{ and end with }lua without extra wrappers."
        return analysis

    return analysis


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
    elif first_line.startswith("{") or first_line.startswith("["):
        reason = "Response still contains wrapper or JSON/object syntax instead of standalone Lua code."
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


def smart_normalize(text: str) -> str:
    """Backward-compatible alias used by the newer generation branch."""
    return normalize_lua_code(text)


def validate_lua_response(text: str) -> dict[str, Any]:
    """Backward-compatible alias used by the newer generation branch."""
    return analyze_lua_response(text)

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


_VERIFICATION_CHECK_KEYS = (
    "workflow_path_usage",
    "source_shape_understood",
    "target_shape_satisfied",
    "logic_correctness",
    "helper_api_usage",
    "edge_case_handling",
)


def _normalize_verification_checks(value: object) -> dict[str, dict[str, str]]:
    raw_checks = value if isinstance(value, dict) else {}
    normalized: dict[str, dict[str, str]] = {}
    for key in _VERIFICATION_CHECK_KEYS:
        item = raw_checks.get(key, {}) if isinstance(raw_checks, dict) else {}
        if not isinstance(item, dict):
            item = {}
        status = str(item.get("status", "unclear") or "unclear").strip().lower()
        if status not in {"pass", "fail", "unclear"}:
            status = "unclear"
        reason = str(item.get("reason", "") or "").strip()
        normalized[key] = {"status": status, "reason": reason}
    return normalized


def _normalize_verification_result(data: dict[str, Any]) -> dict[str, Any]:
    passed = bool(data.get("passed", False))
    try:
        score = int(data.get("score", 100 if passed else 0))
    except (TypeError, ValueError):
        score = 100 if passed else 0
    score = max(0, min(100, score))
    return {
        "passed": passed,
        "score": score,
        "summary": str(data.get("summary", "")).strip(),
        "missing_requirements": _ensure_string_list(data.get("missing_requirements")),
        "warnings": _ensure_string_list(data.get("warnings")),
        "checks": _normalize_verification_checks(data.get("checks")),
    }


async def async_verify_requirements(
    llm: LLMProvider,
    prompt: str,
    code: str,
    run_output: str = "",
    extra_context: str = "",
) -> dict[str, Any]:
    """Run requirement verification using the same local LLM provider as the graph."""
    messages = [
        {"role": "system", "content": DEFAULT_VERIFICATION_SYSTEM_PROMPT},
        {"role": "user", "content": f"User request:\n{prompt}\n\n{LOWCODE_CONTRACT_TEXT}"},
    ]
    extra_context_text = str(extra_context or "").strip()
    if extra_context_text:
        messages.append(
            {
                "role": "user",
                "content": extra_context_text,
            }
        )
    if run_output.strip():
        messages.append(
            {
                "role": "user",
                "content": f"Runtime output:\n{run_output or 'none'}",
            }
        )
    messages.extend(
        [
            {"role": "user", "content": f"Lua solution under review:\n```lua\n{code}\n```"},
            {
                "role": "user",
                "content": (
                    "Check whether the Lua solution above fully satisfies the user request. "
                    "Return strict JSON only in this shape:\n"
                    '{"passed": true, "score": 100, "summary": "short summary", '
                    '"missing_requirements": [], "warnings": [], "checks": {'
                    '"workflow_path_usage": {"status": "pass", "reason": ""}, '
                    '"source_shape_understood": {"status": "pass", "reason": ""}, '
                    '"target_shape_satisfied": {"status": "pass", "reason": ""}, '
                    '"logic_correctness": {"status": "pass", "reason": ""}, '
                    '"helper_api_usage": {"status": "pass", "reason": ""}, '
                    '"edge_case_handling": {"status": "pass", "reason": ""}}}'
                ),
            },
        ]
    )

    raw = await llm.chat(
        messages,
        temperature=DEFAULT_VERIFICATION_TEMPERATURE,
        agent_name="RequirementsVerifier",
    )
    normalized = _normalize_verification_result(_extract_json_block(raw))
    if not normalized["summary"]:
        normalized["summary"] = "Verification completed."
    if normalized["passed"]:
        normalized["score"] = 100
    elif normalized["score"] >= 100:
        normalized["score"] = 99
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
