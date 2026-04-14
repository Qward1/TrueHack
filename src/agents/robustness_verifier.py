"""Standalone RobustnessVerifier agent for edge-case checks."""

from __future__ import annotations

import json
import re
from typing import Any, Callable, TypedDict

import structlog

from src.core.llm import LLMProvider

logger = structlog.get_logger(__name__)

_AGENT_NAME = "RobustnessVerifier"

_SYSTEM_PROMPT = """You are RobustnessVerifier.
Check only robustness and edge-case errors in a Lua workflow solution.
Do not judge full business logic.
Check:
- missing fields
- nil handling
- empty arrays or empty tables
- short or partial strings
- unsafe operations like ipairs(nil), string.sub(nil, ...), tonumber(nil)
- code that works only for the perfect sample
Prefer concrete evidence when present, otherwise use cautious heuristics.
Use only the data explicitly provided in the input.
Do not invent workflow paths, variables, fields, runtime evidence, or edge cases that are not grounded in the input.
Any path or variable not listed in allowed_workflow_paths, available_code_variables, or the focused evidence sections must be treated as unavailable.
Fail only when you can point to exact evidence from the input.
If exact evidence is missing, do not guess and do not fabricate a mismatch.
Return JSON only.
If passed=false, describe the risky edge case and a minimal safe patch."""

_WORKFLOW_PATH_RE = re.compile(r"wf\.(?:vars|initVariables)(?:\.[A-Za-z_][A-Za-z0-9_]*)+")
_LOCAL_ALIAS_RE = re.compile(
    r"\blocal\s+([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(wf\.(?:vars|initVariables)(?:\.[A-Za-z_][A-Za-z0-9_]*)+)"
)
_IPAIRS_CALL_RE = re.compile(r"\bipairs\s*\(\s*([A-Za-z_][A-Za-z0-9_\.]*)\s*\)")
_STRING_SUB_CALL_RE = re.compile(
    r"\bstring\.sub\s*\(\s*([A-Za-z_][A-Za-z0-9_\.]*)\s*,\s*(\d+)(?:\s*,\s*(\d+))?"
)
_METHOD_SUB_CALL_RE = re.compile(r"\b([A-Za-z_][A-Za-z0-9_]*)\s*:\s*sub\s*\(\s*(\d+)(?:\s*,\s*(\d+))?")
_TONUMBER_CALL_RE = re.compile(r"\btonumber\s*\(\s*([A-Za-z_][A-Za-z0-9_\.]*)\s*\)")
_INDEX_ACCESS_RE = re.compile(r"\b([A-Za-z_][A-Za-z0-9_\.]*)\s*\[\s*(1|#\s*[A-Za-z_][A-Za-z0-9_]*)\s*\]")

_VALID_SEVERITIES = frozenset({"low", "medium", "high", "critical"})
_VALID_PATCH_SCOPES = frozenset({"none", "local", "function_level", "multi_block", "rewrite"})
_NIL_RUNTIME_MARKERS = (
    "nil value",
    "got nil",
    "attempt to index",
    "attempt to get length",
    "attempt to perform arithmetic",
    "attempt to compare",
    "attempt to concatenate",
    "bad argument",
)
_IDENTITY_KEYS = ("id", "ID", "sku", "SKU", "email", "name", "key", "uuid", "code")


class RobustnessVerifierInput(TypedDict, total=False):
    task: str
    code: str
    source_field_path: str | None
    output_field_path: str | None
    expected_workflow_paths: list[str]
    selected_operation: str | None
    semantic_expectations: list[str]
    parsed_context: object
    runtime_result: object
    before_state: object
    after_state: object
    run_output: str
    run_error: str
    failure_kind: str
    allowed_workflow_paths: list[str]
    available_code_variables: list[str]
    available_runtime_evidence: dict[str, bool]


class FixerBrief(TypedDict):
    goal: str
    must_change: list[str]
    must_preserve: list[str]
    forbidden_fixes: list[str]
    suggested_patch: str
    patch_scope: str


class RobustnessVerifierResult(TypedDict):
    verifier_name: str
    passed: bool
    error_family: str | None
    error_code: str | None
    severity: str
    summary: str
    field_path: str | None
    evidence: list[str]
    expected: dict[str, Any]
    actual: dict[str, Any]
    fixer_brief: FixerBrief
    confidence: float


class RobustnessVerifierNodeOutput(TypedDict, total=False):
    robustness_verifier_result: RobustnessVerifierResult
    verification: dict[str, Any]
    verification_passed: bool
    failure_stage: str


_OUTPUT_SCHEMA_TEXT = """Return JSON only with these keys:
{
  "verifier_name": "RobustnessVerifier",
  "passed": true,
  "error_family": null,
  "error_code": null,
  "severity": "low|medium|high|critical",
  "summary": "string",
  "field_path": null,
  "evidence": [],
  "expected": {},
  "actual": {},
  "fixer_brief": {
    "goal": "string",
    "must_change": [],
    "must_preserve": [],
    "forbidden_fixes": [],
    "suggested_patch": "string",
    "patch_scope": "none|local|function_level|multi_block|rewrite"
  },
  "confidence": 0.0
}

Output rules:
- Keep expected, actual, evidence, and fixer_brief minimal.
- field_path must be null or an exact path proven by the input.
- expected.edge_case and actual.behavior must stay short and concrete.
- Do not add any path, variable, or edge case that is not explicitly available."""


def _ensure_string_list(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item).strip() for item in value if str(item).strip()]


def _unique_strings(items: list[str]) -> list[str]:
    seen: set[str] = set()
    ordered: list[str] = []
    for item in items:
        normalized = str(item).strip()
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        ordered.append(normalized)
    return ordered


def _ensure_object(value: object) -> dict[str, Any]:
    return dict(value) if isinstance(value, dict) else {}


def _normalize_nullable_string(value: object) -> str | None:
    text = str(value or "").strip()
    return text or None


def _clamp_confidence(value: object) -> float:
    try:
        numeric = float(value)
    except (TypeError, ValueError):
        return 0.0
    return max(0.0, min(1.0, numeric))


def _compact_json(value: object, limit: int = 2500) -> str:
    try:
        rendered = json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True)
    except Exception:
        rendered = str(value)
    if len(rendered) <= limit:
        return rendered
    return rendered[: limit - 3].rstrip() + "..."


def _build_prompt_sections(*sections: str) -> str:
    return "\n\n".join(section for section in sections if section and section.strip())


def _extract_workflow_paths(text: str) -> list[str]:
    if not text:
        return []
    return _unique_strings(_WORKFLOW_PATH_RE.findall(text))


def _extract_inventory_paths(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    paths: list[str] = []
    for item in value:
        if not isinstance(item, dict):
            continue
        path = str(item.get("path", "") or "").strip()
        if path:
            paths.append(path)
    return _unique_strings(paths)


def _extract_code_variables(code: str) -> list[str]:
    patterns = (
        re.compile(r"(?im)^\s*local\s+([A-Za-z_][A-Za-z0-9_]*)\b"),
        re.compile(r"(?im)^\s*local\s+function\s+([A-Za-z_][A-Za-z0-9_]*)\b"),
        re.compile(r"(?im)^\s*function\s+([A-Za-z_][A-Za-z0-9_]*)\b"),
        re.compile(r"(?im)\bfor\s+([A-Za-z_][A-Za-z0-9_]*)\s*,\s*([A-Za-z_][A-Za-z0-9_]*)\s+in\b"),
        re.compile(r"(?im)\bfor\s+([A-Za-z_][A-Za-z0-9_]*)\s+in\b"),
    )
    reserved = {
        "and", "break", "do", "else", "elseif", "end", "false", "for", "function", "if",
        "in", "local", "nil", "not", "or", "repeat", "return", "then", "true", "until",
        "while", "wf", "vars", "initVariables",
    }
    found: list[str] = []
    for pattern in patterns:
        for match in pattern.finditer(code or ""):
            for group in match.groups():
                name = str(group or "").strip()
                if name and name not in reserved:
                    found.append(name)
    return _unique_strings(found)


def _format_named_list(title: str, items: list[str]) -> str:
    normalized = _unique_strings([str(item).strip() for item in items if str(item).strip()])
    if not normalized:
        return f"{title}:\n[]"
    return title + ":\n" + "\n".join(f"- {item}" for item in normalized)


def _render_presence_map(title: str, value: object) -> str:
    data = value if isinstance(value, dict) else {}
    lines = [f"{title}:"]
    for key in sorted(data.keys()):
        lines.append(f"- {key}: {'present' if bool(data[key]) else 'missing'}")
    return "\n".join(lines)


def _normalize_fixer_brief(raw: object, *, passed: bool) -> FixerBrief:
    data = raw if isinstance(raw, dict) else {}
    patch_scope = str(data.get("patch_scope", "") or "").strip()
    if patch_scope not in _VALID_PATCH_SCOPES:
        patch_scope = "none" if passed else "local"
    goal = str(data.get("goal", "") or "").strip()
    suggested_patch = str(data.get("suggested_patch", "") or "").strip()
    must_change = _ensure_string_list(data.get("must_change"))
    must_preserve = _ensure_string_list(data.get("must_preserve"))
    forbidden_fixes = _ensure_string_list(data.get("forbidden_fixes"))
    if passed:
        return {
            "goal": "",
            "must_change": [],
            "must_preserve": [],
            "forbidden_fixes": [],
            "suggested_patch": "",
            "patch_scope": "none",
        }
    return {
        "goal": goal or "Make the code safe for missing or empty input.",
        "must_change": must_change,
        "must_preserve": must_preserve,
        "forbidden_fixes": forbidden_fixes,
        "suggested_patch": suggested_patch,
        "patch_scope": patch_scope or "local",
    }


def _jsonish_signature(value: object) -> str:
    try:
        return json.dumps(value, ensure_ascii=False, sort_keys=True)
    except Exception:
        return repr(value)


def _jsonish_equal(left: object, right: object) -> bool:
    return _jsonish_signature(left) == _jsonish_signature(right)


def _compact_counterexample(value: object) -> object:
    if isinstance(value, dict):
        compact: dict[str, Any] = {}
        for key in _IDENTITY_KEYS:
            if key in value:
                compact[key] = value[key]
        if compact:
            return compact
        return value
    return value


def _resolve_workflow_path_value(root: object, path: str) -> tuple[bool, object]:
    if root is None or not path:
        return False, None
    parts = [segment for segment in str(path).split(".") if segment]
    current = root
    for index, segment in enumerate(parts):
        if index == 0 and segment == "wf":
            if isinstance(current, dict) and "wf" in current:
                current = current["wf"]
            else:
                continue
            continue
        if isinstance(current, dict):
            if segment in current:
                current = current[segment]
                continue
            return False, None
        if isinstance(current, list) and segment.isdigit():
            position = int(segment) - 1
            if 0 <= position < len(current):
                current = current[position]
                continue
        return False, None
    return True, current


def _normalize_workflow_snapshot(snapshot: object) -> object:
    if not isinstance(snapshot, dict):
        return snapshot
    if isinstance(snapshot.get("wf"), dict):
        return snapshot
    if "vars" in snapshot or "initVariables" in snapshot:
        return {"wf": dict(snapshot)}
    return snapshot


def _derive_source_field_path(payload: RobustnessVerifierInput) -> str | None:
    explicit = _normalize_nullable_string(payload.get("source_field_path"))
    if explicit:
        return explicit
    expected_paths = _unique_strings(_ensure_string_list(payload.get("expected_workflow_paths")))
    if expected_paths:
        return expected_paths[0]
    extracted = _extract_workflow_paths(str(payload.get("task", "") or ""))
    if extracted:
        return extracted[0]
    return None


def _derive_output_field_path(payload: RobustnessVerifierInput) -> str | None:
    explicit = _normalize_nullable_string(payload.get("output_field_path"))
    if explicit:
        return explicit
    return None


def _derive_selected_operation(payload: RobustnessVerifierInput) -> str:
    selected = str(payload.get("selected_operation", "") or "").strip().lower()
    if selected and selected != "llm":
        return selected
    task = str(payload.get("task", "") or "").lower()
    if any(marker in task for marker in ("count", "сколько", "колич")):
        return "count"
    if any(marker in task for marker in ("first", "перв")):
        return "first"
    if any(marker in task for marker in ("last", "послед")):
        return "last"
    if any(marker in task for marker in ("length", "длина")):
        return "string_length"
    if any(marker in task for marker in ("sum", "total", "сумм", "итог")):
        return "sum"
    if any(marker in task for marker in ("return", "верни")):
        return "return"
    return selected or "llm"


def _extract_path_aliases(code: str) -> dict[str, str]:
    aliases: dict[str, str] = {}
    for match in _LOCAL_ALIAS_RE.finditer(code or ""):
        alias = str(match.group(1) or "").strip()
        path = str(match.group(2) or "").strip()
        if alias and path and alias not in aliases:
            aliases[alias] = path
    return aliases


def _source_expr_candidates(payload: RobustnessVerifierInput, aliases: dict[str, str]) -> list[str]:
    source_path = _derive_source_field_path(payload)
    if not source_path:
        return []
    candidates = [source_path]
    for alias, path in aliases.items():
        if path == source_path:
            candidates.append(alias)
    return candidates


def _resolve_source_value(payload: RobustnessVerifierInput) -> tuple[str | None, object | None]:
    source_path = _derive_source_field_path(payload)
    if not source_path:
        return None, None
    roots = [payload.get("before_state"), payload.get("parsed_context")]
    for root in roots:
        if root is None:
            continue
        found, value = _resolve_workflow_path_value(root, source_path)
        if found:
            return source_path, value
    return source_path, None


def _expr_references_source(expr: str, candidates: list[str]) -> bool:
    normalized = str(expr or "").strip()
    return any(normalized == candidate for candidate in candidates)


def _expr_has_collection_guard(code: str, expr: str) -> bool:
    escaped = re.escape(str(expr).strip())
    patterns = (
        rf"{escaped}\s+or\s+\{{\s*\}}",
        rf"if\s+{escaped}\s+then",
        rf"if\s+{escaped}\s*~=\s*nil",
        rf"type\s*\(\s*{escaped}\s*\)\s*==\s*[\"']table[\"']",
    )
    return any(re.search(pattern, code or "") for pattern in patterns)


def _expr_has_string_guard(code: str, expr: str) -> bool:
    escaped = re.escape(str(expr).strip())
    patterns = (
        rf"{escaped}\s+or\s+[\"'][\"']",
        rf"if\s+{escaped}\s+then",
        rf"if\s+{escaped}\s*~=\s*nil",
        rf"type\s*\(\s*{escaped}\s*\)\s*==\s*[\"']string[\"']",
        rf"#\s*{escaped}\s*>",
    )
    return any(re.search(pattern, code or "") for pattern in patterns)


def _expr_has_number_guard(code: str, expr: str, line: str) -> bool:
    stripped = str(line or "").strip()
    if re.search(r"tonumber\s*\([^)]*\)\s*or\s*[-+]?\d+", stripped):
        return True
    escaped = re.escape(str(expr).strip())
    patterns = (
        rf"if\s+{escaped}\s+then",
        rf"if\s+{escaped}\s*~=\s*nil",
        rf"{escaped}\s+or\s*[-+]?\d+",
    )
    return any(re.search(pattern, code or "") for pattern in patterns)


def _expr_has_index_guard(code: str, expr: str) -> bool:
    escaped = re.escape(str(expr).strip())
    patterns = (
        rf"#\s*{escaped}\s*>",
        rf"if\s+{escaped}\s*\[\s*1\s*\]",
        rf"if\s+next\s*\(\s*{escaped}\s*\)",
        rf"if\s+{escaped}\s+then",
    )
    return any(re.search(pattern, code or "") for pattern in patterns)


def _build_passed_result(
    *,
    summary: str,
    evidence: list[str] | None = None,
    confidence: float = 1.0,
) -> RobustnessVerifierResult:
    return {
        "verifier_name": _AGENT_NAME,
        "passed": True,
        "error_family": None,
        "error_code": None,
        "severity": "low",
        "summary": summary,
        "field_path": None,
        "evidence": _ensure_string_list(evidence or []),
        "expected": {},
        "actual": {},
        "fixer_brief": _normalize_fixer_brief({}, passed=True),
        "confidence": _clamp_confidence(confidence),
    }


def _build_failed_result(
    *,
    error_family: str,
    error_code: str,
    summary: str,
    field_path: str | None,
    edge_case: str,
    expected_behavior: str,
    actual_behavior: str,
    evidence: list[str] | None = None,
    actual_extra: dict[str, Any] | None = None,
    must_change: list[str] | None = None,
    suggested_patch: str = "",
    patch_scope: str = "local",
    confidence: float = 1.0,
    severity: str = "high",
) -> RobustnessVerifierResult:
    expected = {
        "edge_case": edge_case,
        "expected_behavior": expected_behavior,
    }
    actual = {
        "behavior": actual_behavior,
    }
    if field_path:
        expected["field_path"] = field_path
        actual["field_path"] = field_path
    if actual_extra:
        actual.update(actual_extra)
    return {
        "verifier_name": _AGENT_NAME,
        "passed": False,
        "error_family": error_family,
        "error_code": error_code,
        "severity": severity if severity in _VALID_SEVERITIES else "high",
        "summary": summary.strip() or "Robustness verification failed.",
        "field_path": field_path,
        "evidence": _ensure_string_list(evidence or []),
        "expected": expected,
        "actual": actual,
        "fixer_brief": _normalize_fixer_brief(
            {
                "goal": "Make the code safe for missing or empty input.",
                "must_change": must_change or [expected_behavior],
                "must_preserve": ["Keep the intended business behavior for valid input."],
                "forbidden_fixes": ["Do not hardcode a perfect sample."],
                "suggested_patch": suggested_patch or actual_behavior,
                "patch_scope": patch_scope,
            },
            passed=False,
        ),
        "confidence": _clamp_confidence(confidence),
    }


def _classify_runtime_error(payload: RobustnessVerifierInput) -> RobustnessVerifierResult | None:
    run_error = str(payload.get("run_error", "") or "").strip()
    if not run_error:
        return None

    lowered = run_error.lower()
    failure_kind = str(payload.get("failure_kind", "") or "").strip().lower()
    source_path, source_value = _resolve_source_value(payload)
    field_path = source_path or _derive_output_field_path(payload)

    if not any(marker in lowered for marker in _NIL_RUNTIME_MARKERS) and failure_kind not in {"runtime", "contract"}:
        return None

    if "ipairs" in lowered:
        error_code = "unsafe_ipairs_runtime_error"
        edge_case = f"`{field_path or 'collection'}` is nil."
        expected_behavior = "Guard the collection before iterating with `ipairs`."
        actual_behavior = "Execution crashed because `ipairs` received a nil value."
    elif "sub" in lowered:
        error_code = "unsafe_string_sub_runtime_error"
        edge_case = f"`{field_path or 'string value'}` is nil or shorter than expected."
        expected_behavior = "Guard string slicing for nil or short input."
        actual_behavior = "Execution crashed during `string.sub` on unsafe input."
    elif "tonumber" in lowered:
        error_code = "unsafe_tonumber_runtime_error"
        edge_case = f"`{field_path or 'numeric input'}` is nil or not convertible."
        expected_behavior = "Handle failed numeric conversion safely before using the result."
        actual_behavior = "Execution failed after an unsafe numeric conversion path."
    elif source_path and source_value is None:
        error_code = "missing_field_runtime_error"
        edge_case = f"`{source_path}` is missing."
        expected_behavior = "Handle a missing workflow field safely."
        actual_behavior = "Execution failed when the field was missing."
    else:
        error_code = "nil_handling_runtime_error"
        edge_case = f"`{field_path or 'input value'}` is nil or malformed."
        expected_behavior = "Handle nil or malformed input safely."
        actual_behavior = "Execution failed on a nil-sensitive code path."

    return _build_failed_result(
        error_family="robustness",
        error_code=error_code,
        summary=f"Concrete runtime evidence shows an unsafe edge case: {run_error}",
        field_path=field_path,
        edge_case=edge_case,
        expected_behavior=expected_behavior,
        actual_behavior=actual_behavior,
        evidence=["Primary evidence source: run_error.", f"Failure kind: {failure_kind or 'unknown'}."],
        actual_extra={"run_error": run_error, "failure_kind": failure_kind},
        must_change=[expected_behavior],
        suggested_patch="Add a nil-safe or empty-safe guard before the risky operation.",
        patch_scope="local",
        confidence=1.0,
        severity="critical",
    )


def _build_concrete_success(payload: RobustnessVerifierInput) -> RobustnessVerifierResult | None:
    run_error = str(payload.get("run_error", "") or "").strip()
    if run_error:
        return None
    if payload.get("runtime_result") is None and payload.get("after_state") is None:
        return None
    return _build_passed_result(
        summary="Concrete execution evidence does not show a robustness failure.",
        evidence=[
            "Primary evidence source: runtime_result/after_state.",
            "Execution succeeded without a concrete runtime edge-case failure.",
        ],
        confidence=0.95,
    )


def _detect_missing_field_risk(payload: RobustnessVerifierInput, code: str, candidates: list[str]) -> RobustnessVerifierResult | None:
    source_path, source_value = _resolve_source_value(payload)
    if not source_path or source_value is not None:
        return None
    if not any(candidate in code for candidate in candidates):
        return None
    if any(_expr_has_collection_guard(code, candidate) or _expr_has_string_guard(code, candidate) for candidate in candidates):
        return None
    return _build_failed_result(
        error_family="robustness",
        error_code="missing_field_unhandled",
        summary=f"`{source_path}` may be missing, but the code assumes it is always present.",
        field_path=source_path,
        edge_case=f"`{source_path}` is absent from the workflow input.",
        expected_behavior="Handle a missing workflow field safely and return a safe fallback.",
        actual_behavior="The code dereferences the field without a missing-field guard.",
        evidence=["No concrete success evidence was provided.", f"Resolved `{source_path}` as missing in before_state/parsed context."],
        actual_extra={"risky_operation": source_path},
        must_change=[f"Guard `{source_path}` before using it."],
        suggested_patch=f"Return early or default `{source_path}` to a safe fallback when it is missing.",
        confidence=0.97,
    )


def _detect_ipairs_risk(payload: RobustnessVerifierInput, code: str, candidates: list[str]) -> RobustnessVerifierResult | None:
    source_path, source_value = _resolve_source_value(payload)
    for match in _IPAIRS_CALL_RE.finditer(code or ""):
        expr = str(match.group(1) or "").strip()
        if not _expr_references_source(expr, candidates):
            continue
        if _expr_has_collection_guard(code, expr):
            continue
        if source_value is None:
            edge_case = f"`{source_path or expr}` is nil."
            error_code = "unsafe_ipairs"
            summary = f"`{expr}` may be nil, but the code calls `ipairs` on it without a guard."
        elif isinstance(source_value, (list, dict)):
            continue
        else:
            edge_case = f"`{source_path or expr}` is not a table."
            error_code = "fragile_collection_handling"
            summary = f"`{expr}` is iterated with `ipairs` without proving it is a collection."
        return _build_failed_result(
            error_family="robustness",
            error_code=error_code,
            summary=summary,
            field_path=source_path or expr,
            edge_case=edge_case,
            expected_behavior="Guard the collection before iterating and default to an empty result when needed.",
            actual_behavior="The loop assumes the collection always exists and is iterable.",
            evidence=["No concrete success evidence was provided.", f"Detected `ipairs({expr})` without a nil-safe fallback."],
            actual_extra={"risky_operation": f"ipairs({expr})"},
            must_change=[f"Guard `{expr}` before iterating with `ipairs`."],
            suggested_patch="Default the collection to `{}` or return early when it is nil.",
            confidence=0.98,
        )
    return None


def _detect_string_sub_risk(payload: RobustnessVerifierInput, code: str, candidates: list[str]) -> RobustnessVerifierResult | None:
    source_path, source_value = _resolve_source_value(payload)

    for match in _STRING_SUB_CALL_RE.finditer(code or ""):
        expr = str(match.group(1) or "").strip()
        start_index = int(match.group(2) or "0")
        end_group = match.group(3)
        end_index = int(end_group) if end_group else start_index
        if not _expr_references_source(expr, candidates):
            continue
        if _expr_has_string_guard(code, expr):
            continue
        if source_value is None:
            return _build_failed_result(
                error_family="robustness",
                error_code="unsafe_string_sub",
                summary=f"`{expr}` may be nil, but the code slices it with `string.sub` without a guard.",
                field_path=source_path or expr,
                edge_case=f"`{source_path or expr}` is nil.",
                expected_behavior="Guard string slicing for nil input and use a safe fallback.",
                actual_behavior="The code assumes the string is always present before slicing it.",
                evidence=["No concrete success evidence was provided.", f"Detected `string.sub({expr}, ...)` without a nil-safe fallback."],
                actual_extra={"risky_operation": f"string.sub({expr}, {start_index}, {end_index})"},
                must_change=[f"Guard `{expr}` before calling `string.sub`."],
                suggested_patch="Use `expr or \"\"` or return early when the string is nil.",
                confidence=0.98,
            )
        if isinstance(source_value, str) and len(source_value) < end_index:
            return _build_failed_result(
                error_family="robustness",
                error_code="short_string_unhandled",
                summary=f"`{source_path or expr}` can be shorter than the requested substring range.",
                field_path=source_path or expr,
                edge_case=f"`{source_path or expr}` is shorter than `{end_index}` characters.",
                expected_behavior="Handle short or partial strings safely before slicing them.",
                actual_behavior="The code slices a fixed range without checking string length.",
                evidence=["No concrete success evidence was provided.", f"Observed sample length `{len(source_value)}` for a substring ending at `{end_index}`."],
                actual_extra={"risky_operation": f"string.sub({expr}, {start_index}, {end_index})"},
                must_change=[f"Check the length of `{expr}` before taking a fixed substring."],
                suggested_patch="Add a length check or use a safe fallback for partial strings.",
                confidence=0.96,
            )

    for match in _METHOD_SUB_CALL_RE.finditer(code or ""):
        expr = str(match.group(1) or "").strip()
        start_index = int(match.group(2) or "0")
        end_group = match.group(3)
        end_index = int(end_group) if end_group else start_index
        if not _expr_references_source(expr, candidates):
            continue
        if _expr_has_string_guard(code, expr):
            continue
        if source_value is None:
            return _build_failed_result(
                error_family="robustness",
                error_code="unsafe_string_sub",
                summary=f"`{expr}` may be nil, but the code calls `:sub()` on it without a guard.",
                field_path=source_path or expr,
                edge_case=f"`{source_path or expr}` is nil.",
                expected_behavior="Guard string slicing for nil input and use a safe fallback.",
                actual_behavior="The code assumes the string is always present before slicing it.",
                evidence=["No concrete success evidence was provided.", f"Detected `{expr}:sub(...)` without a nil-safe fallback."],
                actual_extra={"risky_operation": f"{expr}:sub({start_index}, {end_index})"},
                must_change=[f"Guard `{expr}` before calling `:sub()`."],
                suggested_patch="Use `expr and expr:sub(...)` or return early when the string is nil.",
                confidence=0.98,
            )
        if isinstance(source_value, str) and len(source_value) < end_index:
            return _build_failed_result(
                error_family="robustness",
                error_code="short_string_unhandled",
                summary=f"`{source_path or expr}` can be shorter than the requested substring range.",
                field_path=source_path or expr,
                edge_case=f"`{source_path or expr}` is shorter than `{end_index}` characters.",
                expected_behavior="Handle short or partial strings safely before slicing them.",
                actual_behavior="The code slices a fixed range without checking string length.",
                evidence=["No concrete success evidence was provided.", f"Observed sample length `{len(source_value)}` for a substring ending at `{end_index}`."],
                actual_extra={"risky_operation": f"{expr}:sub({start_index}, {end_index})"},
                must_change=[f"Check the length of `{expr}` before taking a fixed substring."],
                suggested_patch="Add a length check or use a safe fallback for partial strings.",
                confidence=0.96,
            )

    return None


def _detect_tonumber_risk(payload: RobustnessVerifierInput, code: str, candidates: list[str]) -> RobustnessVerifierResult | None:
    source_path, source_value = _resolve_source_value(payload)
    for match in _TONUMBER_CALL_RE.finditer(code or ""):
        expr = str(match.group(1) or "").strip()
        if not _expr_references_source(expr, candidates):
            continue
        line_start = code.rfind("\n", 0, match.start()) + 1
        line_end = code.find("\n", match.end())
        if line_end < 0:
            line_end = len(code)
        line = code[line_start:line_end]
        if _expr_has_number_guard(code, expr, line):
            continue
        edge_case = f"`{source_path or expr}` is nil or not numeric."
        if source_value is None or source_value == "":
            summary = f"`tonumber({expr})` is used without checking for nil or missing numeric input."
            error_code = "unsafe_tonumber"
        else:
            summary = f"`tonumber({expr})` is used without checking whether the conversion succeeded."
            error_code = "fragile_numeric_conversion"
        return _build_failed_result(
            error_family="robustness",
            error_code=error_code,
            summary=summary,
            field_path=source_path or expr,
            edge_case=edge_case,
            expected_behavior="Handle failed numeric conversion safely before using the result.",
            actual_behavior="The code assumes numeric conversion always succeeds.",
            evidence=["No concrete success evidence was provided.", f"Detected `tonumber({expr})` without a fallback or nil check."],
            actual_extra={"risky_operation": f"tonumber({expr})"},
            must_change=[f"Check the result of `tonumber({expr})` or provide a safe numeric fallback."],
            suggested_patch="Use `tonumber(...) or 0` or guard the converted value before further use.",
            confidence=0.97,
        )
    return None


def _detect_empty_collection_risk(payload: RobustnessVerifierInput, code: str, candidates: list[str]) -> RobustnessVerifierResult | None:
    source_path, source_value = _resolve_source_value(payload)
    for match in _INDEX_ACCESS_RE.finditer(code or ""):
        expr = str(match.group(1) or "").strip()
        index_expr = str(match.group(2) or "").strip()
        if not _expr_references_source(expr, candidates):
            continue
        if _expr_has_index_guard(code, expr):
            continue
        if isinstance(source_value, (list, dict)) and len(source_value) == 0:
            return _build_failed_result(
                error_family="robustness",
                error_code="empty_collection_unhandled",
                summary=f"`{source_path or expr}` can be empty, but the code accesses `{expr}[...]` without a guard.",
                field_path=source_path or expr,
                edge_case=f"`{source_path or expr}` is empty.",
                expected_behavior="Handle empty arrays or empty tables safely before indexing into them.",
                actual_behavior="The code assumes an element is always present.",
                evidence=["No concrete success evidence was provided.", f"Observed an empty sample for `{source_path or expr}`."],
                actual_extra={"risky_operation": f"{expr}[{index_expr}]"},
                must_change=[f"Check whether `{expr}` contains an element before indexing into it."],
                suggested_patch="Guard empty collections and return a safe fallback when no item exists.",
                confidence=0.97,
            )
        if source_value is None:
            return _build_failed_result(
                error_family="robustness",
                error_code="empty_collection_unhandled",
                summary=f"`{source_path or expr}` may be nil or empty, but the code indexes into it without a guard.",
                field_path=source_path or expr,
                edge_case=f"`{source_path or expr}` is nil or empty.",
                expected_behavior="Guard empty or missing collections before indexing into them.",
                actual_behavior="The code assumes an element is always present.",
                evidence=["No concrete success evidence was provided.", f"Detected direct indexed access `{expr}[{index_expr}]` without an emptiness guard."],
                actual_extra={"risky_operation": f"{expr}[{index_expr}]"},
                must_change=[f"Check whether `{expr}` is present and non-empty before indexing into it."],
                suggested_patch="Return early or default to a safe fallback when the collection is empty.",
                confidence=0.95,
            )
    return None


def _detect_local_robustness_failure(payload: RobustnessVerifierInput) -> RobustnessVerifierResult | None:
    code = str(payload.get("code", "") or "")
    aliases = _extract_path_aliases(code)
    candidates = _source_expr_candidates(payload, aliases)

    ipairs_risk = _detect_ipairs_risk(payload, code, candidates)
    if ipairs_risk is not None:
        return ipairs_risk

    string_risk = _detect_string_sub_risk(payload, code, candidates)
    if string_risk is not None:
        return string_risk

    tonumber_risk = _detect_tonumber_risk(payload, code, candidates)
    if tonumber_risk is not None:
        return tonumber_risk

    empty_risk = _detect_empty_collection_risk(payload, code, candidates)
    if empty_risk is not None:
        return empty_risk

    missing_field = _detect_missing_field_risk(payload, code, candidates)
    if missing_field is not None:
        return missing_field

    return None


def _normalize_robustness_verifier_result(raw: object) -> RobustnessVerifierResult:
    data = raw if isinstance(raw, dict) else {}
    passed = bool(data.get("passed", False))
    severity = str(data.get("severity", "") or "").strip().lower()
    if severity not in _VALID_SEVERITIES:
        severity = "low" if passed else "high"

    summary = str(data.get("summary", "") or "").strip()
    if not summary:
        summary = "Robustness check passed." if passed else "Robustness verification failed."

    error_family = _normalize_nullable_string(data.get("error_family"))
    error_code = _normalize_nullable_string(data.get("error_code"))
    field_path = _normalize_nullable_string(data.get("field_path"))
    if passed:
        error_family = None
        error_code = None
        field_path = None

    return {
        "verifier_name": _AGENT_NAME,
        "passed": passed,
        "error_family": error_family,
        "error_code": error_code,
        "severity": severity,
        "summary": summary,
        "field_path": field_path,
        "evidence": _ensure_string_list(data.get("evidence")),
        "expected": _ensure_object(data.get("expected")),
        "actual": _ensure_object(data.get("actual")),
        "fixer_brief": _normalize_fixer_brief(data.get("fixer_brief"), passed=passed),
        "confidence": _clamp_confidence(data.get("confidence")),
    }


def _build_robustness_context_lines(payload: RobustnessVerifierInput) -> list[str]:
    lines: list[str] = []
    operation = _derive_selected_operation(payload)
    if operation:
        lines.append("- selected operation: " + operation)
    source_path = _derive_source_field_path(payload)
    if source_path:
        lines.append("- source field path: " + source_path)
    output_path = _derive_output_field_path(payload)
    if output_path:
        lines.append("- output field path: " + output_path)
    semantic_expectations = _ensure_string_list(payload.get("semantic_expectations"))
    if semantic_expectations:
        lines.append("- semantic expectations: " + ", ".join(semantic_expectations))
    return lines


def _build_robustness_verifier_prompt(payload: RobustnessVerifierInput) -> str:
    task = str(payload.get("task", "") or "").strip()
    code = str(payload.get("code", "") or "").strip()
    context_lines = _build_robustness_context_lines(payload)
    source_path, source_value = _resolve_source_value(payload)
    allowed_workflow_paths = _unique_strings(
        _ensure_string_list(payload.get("allowed_workflow_paths")) or _ensure_string_list(payload.get("expected_workflow_paths"))
    )
    available_code_variables = _unique_strings(
        _ensure_string_list(payload.get("available_code_variables")) or _extract_code_variables(code)
    )
    available_runtime_evidence = payload.get("available_runtime_evidence")
    if not isinstance(available_runtime_evidence, dict):
        available_runtime_evidence = {
            "runtime_result": payload.get("runtime_result") is not None,
            "before_state": payload.get("before_state") is not None,
            "after_state": payload.get("after_state") is not None,
            "run_error": bool(str(payload.get("run_error", "") or "").strip()),
        }

    sections = [
        f"Task:\n{task}" if task else "",
        "Scope:\nCheck only robustness and edge-case issues. Ignore full business-logic and workflow-contract judgments.",
        "Strict rules:\n"
        "- Use only explicit input data.\n"
        "- Never invent workflow paths, variables, runtime evidence, or synthetic edge cases.\n"
        "- You may reference only names from allowed_workflow_paths, available_code_variables, or focused evidence sections.\n"
        "- Do not fail on suspicion alone. Cite exact evidence or keep the verdict conservative.",
        "Execution context:\n" + "\n".join(context_lines) if context_lines else "",
        _format_named_list("allowed_workflow_paths", allowed_workflow_paths),
        _format_named_list("available_code_variables", available_code_variables),
        _render_presence_map("available_runtime_evidence", available_runtime_evidence),
        (
            "runtime_result:\n" + _compact_json(payload.get("runtime_result"))
            if payload.get("runtime_result") is not None
            else ""
        ),
        (
            f"Resolved source value at {source_path}:\n" + _compact_json(source_value)
            if source_path and source_value is not None
            else ""
        ),
        (
            "run_error:\n" + str(payload.get("run_error", "") or "").strip()
            if str(payload.get("run_error", "") or "").strip()
            else ""
        ),
        (
            "run_output:\n" + str(payload.get("run_output", "") or "").strip()
            if str(payload.get("run_output", "") or "").strip()
            else ""
        ),
        f"Lua solution under review:\n```lua\n{code}\n```",
        _OUTPUT_SCHEMA_TEXT,
    ]
    return _build_prompt_sections(*sections)


def build_robustness_verifier_input_from_state(state: dict[str, Any]) -> RobustnessVerifierInput:
    compiled_request = state.get("compiled_request")
    if not isinstance(compiled_request, dict):
        compiled_request = {}

    diagnostics = state.get("diagnostics")
    if not isinstance(diagnostics, dict):
        diagnostics = {}

    task = (
        str(compiled_request.get("verification_prompt", "") or "").strip()
        or str(compiled_request.get("task_text", "") or "").strip()
        or str(compiled_request.get("original_task", "") or "").strip()
        or str(state.get("user_input", "") or "").strip()
    )

    source_field_path = str(compiled_request.get("selected_primary_path", "") or "").strip()
    output_field_path = str(
        state.get("robustness_output_field_path")
        or compiled_request.get("robustness_output_field_path")
        or compiled_request.get("selected_save_path")
        or ""
    ).strip()

    expected_workflow_paths = _unique_strings(
        _ensure_string_list(compiled_request.get("expected_workflow_paths"))
        + ([source_field_path] if source_field_path else [])
        + ([output_field_path] if output_field_path else [])
        + _extract_workflow_paths(task)
    )
    code = str(state.get("generated_code", "") or state.get("current_code", "") or "")
    allowed_workflow_paths = _unique_strings(
        _extract_inventory_paths(compiled_request.get("workflow_path_inventory"))
        + _extract_workflow_paths(code)
        + ([source_field_path] if source_field_path else [])
        + ([output_field_path] if output_field_path else [])
        + expected_workflow_paths
    )

    runtime_result: object = diagnostics.get("result_value")
    if runtime_result is None:
        preview = str(diagnostics.get("result_preview", "") or "").strip()
        if preview:
            runtime_result = preview

    parsed_context = compiled_request.get("parsed_context")
    before_state = (
        _normalize_workflow_snapshot(parsed_context)
        if compiled_request.get("has_parseable_context")
        else None
    )
    after_state = _normalize_workflow_snapshot(diagnostics.get("workflow_state"))

    return {
        "task": task,
        "code": code,
        "source_field_path": source_field_path or None,
        "output_field_path": output_field_path or None,
        "expected_workflow_paths": expected_workflow_paths,
        "selected_operation": _normalize_nullable_string(compiled_request.get("selected_operation")),
        "semantic_expectations": _ensure_string_list(compiled_request.get("semantic_expectations")),
        "parsed_context": parsed_context,
        "runtime_result": runtime_result,
        "before_state": before_state,
        "after_state": after_state,
        "run_output": str(diagnostics.get("run_output", "") or ""),
        "run_error": str(diagnostics.get("run_error", "") or ""),
        "failure_kind": str(diagnostics.get("failure_kind", "") or ""),
        "allowed_workflow_paths": allowed_workflow_paths,
        "available_code_variables": _extract_code_variables(code),
        "available_runtime_evidence": {
            "runtime_result": runtime_result is not None,
            "before_state": before_state is not None,
            "after_state": after_state is not None,
            "run_error": bool(str(diagnostics.get("run_error", "") or "").strip()),
        },
    }


def to_aggregate_verification_result(result: RobustnessVerifierResult) -> dict[str, Any]:
    missing_requirements = _ensure_string_list(result.get("fixer_brief", {}).get("must_change"))
    if not result["passed"] and not missing_requirements:
        missing_requirements = [result["summary"]]
    return {
        "passed": bool(result["passed"]),
        "summary": str(result["summary"] or "").strip(),
        "missing_requirements": missing_requirements,
        "warnings": [],
        "error": False,
        "verifier_name": result["verifier_name"],
        "error_family": result["error_family"],
        "error_code": result["error_code"],
        "severity": result["severity"],
        "field_path": result["field_path"],
        "evidence": list(result["evidence"]),
        "expected": dict(result["expected"]),
        "actual": dict(result["actual"]),
        "fixer_brief": dict(result["fixer_brief"]),
        "confidence": result["confidence"],
    }


class RobustnessVerifierAgent:
    """LLM-backed robustness verifier with concrete-evidence priority."""

    def __init__(self, llm: LLMProvider) -> None:
        self._llm = llm

    @property
    def name(self) -> str:
        return _AGENT_NAME

    async def verify(self, payload: RobustnessVerifierInput) -> RobustnessVerifierResult:
        code = str(payload.get("code", "") or "")
        source_field_path = _derive_source_field_path(payload)
        logger.info(
            f"[{_AGENT_NAME}] started",
            code_len=len(code),
            source_field_path=source_field_path or "none",
            has_before_state=payload.get("before_state") is not None,
            has_after_state=payload.get("after_state") is not None,
            has_runtime_result=payload.get("runtime_result") is not None,
            has_run_error=bool(str(payload.get("run_error", "") or "").strip()),
        )

        runtime_failure = _classify_runtime_error(payload)
        if runtime_failure is not None:
            logger.info(
                f"[{_AGENT_NAME}] runtime_failure",
                error_code=runtime_failure["error_code"],
                field_path=runtime_failure["field_path"] or "none",
            )
            return runtime_failure

        concrete_success = _build_concrete_success(payload)
        if concrete_success is not None:
            logger.info(f"[{_AGENT_NAME}] concrete_success")
            return concrete_success

        local_failure = _detect_local_robustness_failure(payload)
        if local_failure is not None:
            logger.info(
                f"[{_AGENT_NAME}] local_failure",
                error_code=local_failure["error_code"],
                field_path=local_failure["field_path"] or "none",
            )
            return local_failure

        prompt = _build_robustness_verifier_prompt(payload)
        logger.info(
            f"[{_AGENT_NAME}/llm.generate_json] calling",
            prompt_len=len(prompt),
            system_len=len(_SYSTEM_PROMPT),
        )
        try:
            raw = await self._llm.generate_json(
                prompt,
                system=_SYSTEM_PROMPT,
                agent_name=_AGENT_NAME,
            )
        except Exception as exc:
            logger.warning(f"[{_AGENT_NAME}/llm.generate_json] failed", error=str(exc))
            return _build_failed_result(
                error_family="agent_runtime",
                error_code="llm_unavailable",
                summary=f"RobustnessVerifier could not produce a valid verdict: {exc}",
                field_path=source_field_path,
                edge_case="No structured robustness verdict was produced.",
                expected_behavior="Produce a robustness verdict for the current solution.",
                actual_behavior="LLM verification failed before producing a structured result.",
                evidence=["LLM call failed before a structured verifier verdict was produced."],
                actual_extra={"error": str(exc)},
                must_change=[],
                suggested_patch="",
                patch_scope="none",
                confidence=0.0,
            )

        logger.info(
            f"[{_AGENT_NAME}/llm.generate_json] done",
            raw_keys=list(raw.keys()) if isinstance(raw, dict) else [],
        )
        result = _normalize_robustness_verifier_result(raw)
        logger.info(
            f"[{_AGENT_NAME}] completed",
            passed=result["passed"],
            error_code=result["error_code"] or "none",
            field_path=result["field_path"] or "none",
            confidence=result["confidence"],
        )
        return result


def create_robustness_verifier_node(llm: LLMProvider) -> Callable:
    agent = RobustnessVerifierAgent(llm)

    async def verify_robustness(state: dict[str, Any]) -> RobustnessVerifierNodeOutput:
        payload = build_robustness_verifier_input_from_state(state)
        result = await agent.verify(payload)
        aggregate = to_aggregate_verification_result(result)
        return {
            "robustness_verifier_result": result,
            "verification": aggregate,
            "verification_passed": bool(result["passed"]),
            "failure_stage": "" if result["passed"] else "robustness_verification",
        }

    return verify_robustness
