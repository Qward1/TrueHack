"""Standalone RuntimeStateVerifier agent for execution-evidence checks."""

from __future__ import annotations

import json
import re
from collections import Counter
from typing import Any, Callable, TypedDict

import structlog

from src.core.llm import LLMProvider

logger = structlog.get_logger(__name__)

_AGENT_NAME = "RuntimeStateVerifier"

_SYSTEM_PROMPT = """You are RuntimeStateVerifier.
Check only execution evidence.
Use before_state, after_state, and runtime_result as the source of truth.
Do not rely on code when concrete execution evidence is enough.
Check:
- wrong value after execution
- wrong path updated
- required field unchanged
- extra unintended state change
- runtime_result contradicts the request
Use only the data explicitly provided in the input.
Do not invent workflow paths, variables, fields, results, runtime evidence, or state changes.
Any path or variable not listed in allowed_workflow_paths, available_code_variables, or the focused evidence sections must be treated as unavailable.
Fail only when you can point to exact evidence from the input.
If exact evidence is missing, do not guess and do not fabricate a mismatch.
Return JSON only.
If passed=false, report the exact mismatch at the exact field path and a minimal patch target."""

_WORKFLOW_PATH_RE = re.compile(r"wf\.(?:vars|initVariables)(?:\.[A-Za-z_][A-Za-z0-9_]*)+")
_VALID_SEVERITIES = frozenset({"low", "medium", "high", "critical"})
_VALID_PATCH_SCOPES = frozenset({"none", "local", "function_level", "multi_block", "rewrite"})
_SAVE_MARKERS = (
    "save",
    "update",
    "set",
    "assign",
    "store",
    "write",
    "replace",
    "change",
    "сохрани",
    "запиши",
    "обнови",
    "установи",
    "присвой",
    "измени",
    "замени",
)
_IDENTITY_KEYS = ("id", "ID", "sku", "SKU", "email", "name", "key", "uuid", "code")


class RuntimeStateVerifierInput(TypedDict, total=False):
    task: str
    code: str
    expected_result_action: str | None
    source_field_path: str | None
    output_field_path: str | None
    expected_workflow_paths: list[str]
    selected_operation: str | None
    operation_argument: object
    semantic_expectations: list[str]
    parsed_context: object
    runtime_result: object
    before_state: object
    after_state: object
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


class RuntimeStateVerifierResult(TypedDict):
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


class RuntimeStateVerifierNodeOutput(TypedDict, total=False):
    runtime_state_verifier_result: RuntimeStateVerifierResult
    verification: dict[str, Any]
    verification_passed: bool
    failure_stage: str


class _StateChange(TypedDict):
    path: str
    before: object
    after: object


class _ExpectedValueSpec(TypedDict, total=False):
    operation: str
    source_path: str | None
    value: object
    description: str
    field: str | None


_OUTPUT_SCHEMA_TEXT = """Return JSON only with these keys:
{
  "verifier_name": "RuntimeStateVerifier",
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
- changed_paths and relevant_diff must include only observed evidence.
- Do not add any path or variable that is not explicitly available."""


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
        "goal": goal or "Fix the execution target only.",
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


def _compact_change(change: _StateChange) -> dict[str, Any]:
    return {
        "path": change["path"],
        "before": change["before"],
        "after": change["after"],
    }


def _compact_changes(changes: list[_StateChange], *, limit: int = 8) -> list[dict[str, Any]]:
    return [_compact_change(change) for change in changes[:limit]]


def _summarize_paths(changes: list[_StateChange], *, limit: int = 6) -> list[str]:
    return [change["path"] for change in changes[:limit]]


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


def _derive_source_field_path(payload: RuntimeStateVerifierInput) -> str | None:
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


def _derive_output_field_path(payload: RuntimeStateVerifierInput) -> str | None:
    explicit = _normalize_nullable_string(payload.get("output_field_path"))
    if explicit:
        return explicit
    return None


def _derive_selected_operation(payload: RuntimeStateVerifierInput) -> str:
    selected = str(payload.get("selected_operation", "") or "").strip().lower()
    if selected and selected != "llm":
        return selected
    task = str(payload.get("task", "") or "").lower()
    if any(marker in task for marker in ("sum", "total", "сумм", "итог")):
        return "sum"
    if any(marker in task for marker in ("count", "сколько", "колич")):
        return "count"
    if any(marker in task for marker in ("first", "перв")):
        return "first"
    if any(marker in task for marker in ("last", "послед")):
        return "last"
    if any(marker in task for marker in ("length", "длина")):
        return "string_length"
    if any(marker in task for marker in ("increment", "increase", "add", "увелич", "прибав")):
        return "increment"
    if any(marker in task for marker in ("decrement", "decrease", "subtract", "уменьш", "выч")):
        return "decrement"
    if any(marker in task for marker in ("return", "верни")):
        return "return"
    return selected or "llm"


def _task_expects_state_update(task: str) -> bool:
    lowered = str(task or "").lower()
    return any(marker in lowered for marker in _SAVE_MARKERS)


def _derive_expected_result_action(payload: RuntimeStateVerifierInput) -> str:
    explicit = _normalize_nullable_string(payload.get("expected_result_action"))
    if explicit in {"return", "save_to_wf_vars"}:
        return explicit
    output_path = _derive_output_field_path(payload)
    if output_path:
        return "save_to_wf_vars"
    source_path = _derive_source_field_path(payload)
    if source_path and _task_expects_state_update(str(payload.get("task", "") or "")):
        return "save_to_wf_vars"
    return "return"


def _derive_expected_update_path(payload: RuntimeStateVerifierInput) -> str | None:
    expected_result_action = _derive_expected_result_action(payload)
    output_path = _derive_output_field_path(payload)
    source_path = _derive_source_field_path(payload)
    if expected_result_action == "save_to_wf_vars":
        return output_path or source_path
    if output_path and output_path != source_path:
        return output_path
    return None


def _resolve_source_value(payload: RuntimeStateVerifierInput) -> tuple[str | None, object | None]:
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


def _collect_state_changes(before: object, after: object, path: str = "") -> list[_StateChange]:
    if _jsonish_equal(before, after):
        return []

    if isinstance(before, dict) and isinstance(after, dict):
        changes: list[_StateChange] = []
        keys = sorted(set(before.keys()) | set(after.keys()), key=lambda item: str(item))
        for key in keys:
            child_path = f"{path}.{key}" if path else str(key)
            if key not in before:
                changes.append({"path": child_path, "before": None, "after": after[key]})
                continue
            if key not in after:
                changes.append({"path": child_path, "before": before[key], "after": None})
                continue
            changes.extend(_collect_state_changes(before[key], after[key], child_path))
        if changes:
            return changes
        return [{"path": path or "wf", "before": before, "after": after}]

    if isinstance(before, list) and isinstance(after, list):
        changes = []
        max_len = max(len(before), len(after))
        for index in range(max_len):
            child_path = f"{path}[{index + 1}]"
            if index >= len(before):
                changes.append({"path": child_path, "before": None, "after": after[index]})
                continue
            if index >= len(after):
                changes.append({"path": child_path, "before": before[index], "after": None})
                continue
            changes.extend(_collect_state_changes(before[index], after[index], child_path))
        if changes:
            return changes
        return [{"path": path or "wf", "before": before, "after": after}]

    return [{"path": path or "wf", "before": before, "after": after}]


def _paths_related(target: str, candidate: str) -> bool:
    if not target or not candidate:
        return False
    return (
        candidate == target
        or candidate.startswith(target + ".")
        or candidate.startswith(target + "[")
        or target.startswith(candidate + ".")
        or target.startswith(candidate + "[")
    )


def _is_empty_container_like(value: object) -> bool:
    return value in (None, [], {})


def _is_ignorable_root_artifact(change: _StateChange) -> bool:
    path = str(change["path"] or "")
    if path not in {"wf.vars", "wf.initVariables"}:
        return False
    return _is_empty_container_like(change["before"]) and _is_empty_container_like(change["after"])


def _filter_ignorable_root_artifacts(changes: list[_StateChange]) -> list[_StateChange]:
    return [change for change in changes if not _is_ignorable_root_artifact(change)]


def _related_changes(changes: list[_StateChange], target: str | None) -> list[_StateChange]:
    if not target:
        return []
    return [change for change in changes if _paths_related(target, change["path"])]


def _unexpected_changes(changes: list[_StateChange], target: str | None) -> list[_StateChange]:
    if not target:
        return changes
    return [change for change in changes if not _paths_related(target, change["path"])]


def _coerce_numeric_value(value: object) -> float | None:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    if isinstance(value, str):
        cleaned = value.strip()
        if re.fullmatch(r"-?\d+(?:\.\d+)?", cleaned):
            try:
                return float(cleaned)
            except ValueError:
                return None
    return None


def _coerce_numeric_result(value: object) -> float | None:
    direct = _coerce_numeric_value(value)
    if direct is not None:
        return direct
    if isinstance(value, dict):
        for key in ("count", "total", "sum", "value", "result", "length"):
            numeric = _coerce_numeric_value(value.get(key))
            if numeric is not None:
                return numeric
        if len(value) == 1:
            only_value = next(iter(value.values()))
            return _coerce_numeric_value(only_value)
    return None


def _coerce_sequence(value: object) -> list[Any] | None:
    if isinstance(value, list):
        return value
    if isinstance(value, dict):
        for key in ("items", "result", "value", "data", "rows", "records"):
            nested = value.get(key)
            if isinstance(nested, list):
                return nested
    return None


def _values_match(expected: object, observed: object, *, operation: str) -> bool:
    if operation in {"count", "sum", "increment", "decrement", "string_length"}:
        expected_numeric = _coerce_numeric_result(expected)
        observed_numeric = _coerce_numeric_result(observed)
        if expected_numeric is None or observed_numeric is None:
            return False
        return expected_numeric == observed_numeric
    return _jsonish_equal(expected, observed)


def _normalize_observed_for_operation(operation: str, value: object) -> object:
    if operation in {"count", "sum", "increment", "decrement", "string_length"}:
        numeric = _coerce_numeric_result(value)
        return numeric if numeric is not None else value
    return value


def _infer_numeric_field(task: str, source_list: list[Any]) -> str | None:
    task_lower = task.lower()
    candidate_fields: list[str] = []
    for item in source_list:
        if not isinstance(item, dict):
            continue
        for key in item.keys():
            key_text = str(key).strip()
            if key_text and key_text not in candidate_fields:
                candidate_fields.append(key_text)
    for field in candidate_fields:
        if field.lower() in task_lower:
            return field
    for field in candidate_fields:
        for item in source_list:
            if isinstance(item, dict) and _coerce_numeric_value(item.get(field)) is not None:
                return field
    return None


def _compute_expected_value(payload: RuntimeStateVerifierInput) -> _ExpectedValueSpec | None:
    operation = _derive_selected_operation(payload)
    source_path, source_value = _resolve_source_value(payload)
    if source_value is None:
        return None

    if operation == "count":
        sequence = _coerce_sequence(source_value)
        if sequence is None:
            return None
        return {
            "operation": "count",
            "source_path": source_path,
            "value": len(sequence),
            "description": f"count of `{source_path}`" if source_path else "count of the source collection",
        }

    if operation == "sum":
        sequence = _coerce_sequence(source_value)
        if sequence is None:
            return None
        numeric_field = _infer_numeric_field(str(payload.get("task", "") or ""), sequence)
        if numeric_field is None:
            numeric_values = [_coerce_numeric_value(item) for item in sequence]
            if any(value is None for value in numeric_values):
                return None
            value = sum(number for number in numeric_values if number is not None)
            return {
                "operation": "sum",
                "source_path": source_path,
                "value": value,
                "description": f"sum of values in `{source_path}`" if source_path else "sum of the source values",
            }
        values: list[float] = []
        for item in sequence:
            if not isinstance(item, dict):
                return None
            numeric = _coerce_numeric_value(item.get(numeric_field))
            if numeric is None:
                return None
            values.append(numeric)
        return {
            "operation": "sum",
            "source_path": source_path,
            "value": sum(values),
            "field": numeric_field,
            "description": (
                f"sum of `{numeric_field}` in `{source_path}`" if source_path else f"sum of `{numeric_field}`"
            ),
        }

    if operation in {"first", "last"}:
        sequence = _coerce_sequence(source_value)
        if sequence is None or not sequence:
            return None
        value = sequence[0] if operation == "first" else sequence[-1]
        return {
            "operation": operation,
            "source_path": source_path,
            "value": value,
            "description": f"`{operation}` item from `{source_path}`" if source_path else f"`{operation}` item",
        }

    if operation in {"increment", "decrement"}:
        before_value = _coerce_numeric_result(source_value)
        if before_value is None:
            return None
        argument = _coerce_numeric_value(payload.get("operation_argument"))
        delta = argument if argument is not None else 1.0
        value = before_value + delta if operation == "increment" else before_value - delta
        return {
            "operation": operation,
            "source_path": source_path,
            "value": value,
            "description": f"`{operation}` result for `{source_path}`" if source_path else f"`{operation}` result",
        }

    if operation == "string_length" and isinstance(source_value, str):
        return {
            "operation": "string_length",
            "source_path": source_path,
            "value": len(source_value),
            "description": f"length of `{source_path}`" if source_path else "string length",
        }

    return None


def _build_passed_result(
    *,
    summary: str,
    evidence: list[str] | None = None,
    confidence: float = 1.0,
) -> RuntimeStateVerifierResult:
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
    expected: dict[str, Any],
    actual: dict[str, Any],
    evidence: list[str] | None = None,
    must_change: list[str] | None = None,
    suggested_patch: str = "",
    patch_scope: str = "local",
    confidence: float = 1.0,
    severity: str = "high",
) -> RuntimeStateVerifierResult:
    return {
        "verifier_name": _AGENT_NAME,
        "passed": False,
        "error_family": error_family,
        "error_code": error_code,
        "severity": severity if severity in _VALID_SEVERITIES else "high",
        "summary": summary.strip() or "Runtime/state verification failed.",
        "field_path": field_path,
        "evidence": _ensure_string_list(evidence or []),
        "expected": expected,
        "actual": actual,
        "fixer_brief": _normalize_fixer_brief(
            {
                "goal": "Fix the execution target only.",
                "must_change": must_change or [summary],
                "must_preserve": ["Keep unrelated workflow paths unchanged."],
                "forbidden_fixes": ["Do not mutate additional workflow paths."],
                "suggested_patch": suggested_patch or summary,
                "patch_scope": patch_scope,
            },
            passed=False,
        ),
        "confidence": _clamp_confidence(confidence),
    }


def _build_path_mismatch_result(
    *,
    error_code: str,
    summary: str,
    field_path: str | None,
    expected_update_path: str | None,
    changes: list[_StateChange],
    relevant_changes: list[_StateChange],
    unexpected_changes: list[_StateChange],
    must_change: list[str],
    suggested_patch: str,
    confidence: float = 1.0,
) -> RuntimeStateVerifierResult:
    return _build_failed_result(
        error_family="runtime_state",
        error_code=error_code,
        summary=summary,
        field_path=field_path,
        expected={
            "field_path": expected_update_path,
            "expected_behavior": (
                f"Update only `{expected_update_path}`." if expected_update_path else "Do not change workflow state."
            ),
            "relevant_diff": _compact_changes(relevant_changes),
        },
        actual={
            "changed_paths": _summarize_paths(changes),
            "relevant_diff": _compact_changes(unexpected_changes or relevant_changes or changes),
        },
        evidence=[
            "Primary evidence source: before_state/after_state diff.",
        ],
        must_change=must_change,
        suggested_patch=suggested_patch,
        patch_scope="local",
        confidence=confidence,
    )


def _evaluate_state_path_mismatch(payload: RuntimeStateVerifierInput) -> RuntimeStateVerifierResult | None:
    before_state = payload.get("before_state")
    after_state = payload.get("after_state")
    if before_state is None or after_state is None:
        return None

    changes = _collect_state_changes(before_state, after_state)
    changes = _filter_ignorable_root_artifacts(changes)
    expected_update_path = _derive_expected_update_path(payload)

    if expected_update_path:
        before_found, before_value = _resolve_workflow_path_value(before_state, expected_update_path)
        after_found, after_value = _resolve_workflow_path_value(after_state, expected_update_path)
        target_changed = before_found != after_found or not _jsonish_equal(before_value, after_value)
        related_changes = _related_changes(changes, expected_update_path)
        unexpected_changes = _unexpected_changes(changes, expected_update_path)

        if not target_changed and not changes:
            return _build_path_mismatch_result(
                error_code="required_field_unchanged",
                summary=f"Required field `{expected_update_path}` did not change after execution.",
                field_path=expected_update_path,
                expected_update_path=expected_update_path,
                changes=changes,
                relevant_changes=related_changes,
                unexpected_changes=unexpected_changes,
                must_change=[f"Make sure `{expected_update_path}` changes to the requested result."],
                suggested_patch=f"Write the requested result to `{expected_update_path}`.",
            )

        if not target_changed and changes:
            wrong_path = unexpected_changes[0]["path"] if unexpected_changes else changes[0]["path"]
            result = _build_path_mismatch_result(
                error_code="wrong_path_updated",
                summary=f"Updated `{wrong_path}` instead of the required `{expected_update_path}`.",
                field_path=wrong_path,
                expected_update_path=expected_update_path,
                changes=changes,
                relevant_changes=related_changes,
                unexpected_changes=unexpected_changes,
                must_change=[f"Update `{expected_update_path}` instead of `{wrong_path}`."],
                suggested_patch=f"Move the write/update to `{expected_update_path}` and stop mutating `{wrong_path}`.",
            )
            result["evidence"].append(f"Expected update path `{expected_update_path}` did not change.")
            return result

        if unexpected_changes:
            wrong_path = unexpected_changes[0]["path"]
            result = _build_path_mismatch_result(
                error_code="extra_unintended_state_change",
                summary=f"Unexpected state change detected at `{wrong_path}`.",
                field_path=wrong_path,
                expected_update_path=expected_update_path,
                changes=changes,
                relevant_changes=related_changes,
                unexpected_changes=unexpected_changes,
                must_change=[f"Do not mutate `{wrong_path}`.", f"Keep the update isolated to `{expected_update_path}`."],
                suggested_patch=f"Remove the unintended mutation at `{wrong_path}` and keep only the required update at `{expected_update_path}`.",
            )
            result["evidence"].append(f"Expected update path `{expected_update_path}` changed, but unrelated paths also changed.")
            return result

        return None

    if _derive_expected_result_action(payload) != "save_to_wf_vars" and changes:
        wrong_path = changes[0]["path"]
        result = _build_path_mismatch_result(
            error_code="extra_unintended_state_change",
            summary=f"Workflow state changed at `{wrong_path}` even though the task does not require a state update.",
            field_path=wrong_path,
            expected_update_path=None,
            changes=changes,
            relevant_changes=[],
            unexpected_changes=changes,
            must_change=["Do not mutate workflow state for this task."],
            suggested_patch="Remove the unintended workflow write/update.",
        )
        result["evidence"].append("Execution evidence shows a state mutation for a return-only task.")
        return result

    return None


def _evaluate_runtime_value_mismatch(payload: RuntimeStateVerifierInput) -> RuntimeStateVerifierResult | None:
    expected_spec = _compute_expected_value(payload)
    if expected_spec is None:
        return None

    operation = str(expected_spec.get("operation", "") or "")
    expected_value = expected_spec.get("value")
    description = str(expected_spec.get("description", "") or "expected runtime result")
    expected_update_path = _derive_expected_update_path(payload)

    runtime_result = payload.get("runtime_result")
    if runtime_result is not None:
        normalized_runtime = _normalize_observed_for_operation(operation, runtime_result)
        if not _values_match(expected_value, normalized_runtime, operation=operation):
            return _build_failed_result(
                error_family="runtime_state",
                error_code="runtime_result_contradicts_request",
                summary=f"`runtime_result` is `{_compact_json(normalized_runtime, limit=200)}`, but `{description}` is required.",
                field_path=expected_update_path or _derive_source_field_path(payload),
                expected={
                    "field_path": expected_update_path or _derive_source_field_path(payload),
                    "expected_behavior": f"Produce `{description}`.",
                    "value": expected_value,
                    "operation": operation,
                },
                actual={
                    "runtime_result": runtime_result,
                    "value": normalized_runtime,
                    "operation": operation,
                },
                evidence=["Primary evidence source: runtime_result."],
                must_change=[f"Make `runtime_result` match {description}."],
                suggested_patch=f"Fix the computation so runtime_result equals the required {description}.",
                patch_scope="function_level",
                confidence=1.0,
            )

    if expected_update_path and payload.get("after_state") is not None:
        found, after_value = _resolve_workflow_path_value(payload.get("after_state"), expected_update_path)
        if found:
            normalized_after = _normalize_observed_for_operation(operation, after_value)
            if not _values_match(expected_value, normalized_after, operation=operation):
                before_state = payload.get("before_state")
                after_state = payload.get("after_state")
                changes = _collect_state_changes(before_state, after_state) if before_state is not None else []
                relevant = _related_changes(changes, expected_update_path)
                return _build_failed_result(
                    error_family="runtime_state",
                    error_code="wrong_value_after_execution",
                    summary=f"Field `{expected_update_path}` has `{_compact_json(normalized_after, limit=200)}` after execution, but `{description}` is required.",
                    field_path=expected_update_path,
                    expected={
                        "field_path": expected_update_path,
                        "expected_behavior": f"Write `{description}` to `{expected_update_path}`.",
                        "value": expected_value,
                        "operation": operation,
                    },
                    actual={
                        "value": normalized_after,
                        "after_value": after_value,
                        "operation": operation,
                        "relevant_diff": _compact_changes(relevant),
                    },
                    evidence=["Primary evidence source: after_state."],
                    must_change=[f"Write the correct value to `{expected_update_path}`."],
                    suggested_patch=f"Fix the execution so `{expected_update_path}` receives the required {description}.",
                    patch_scope="local",
                    confidence=1.0,
                )

    return None


def _build_positive_execution_result(payload: RuntimeStateVerifierInput) -> RuntimeStateVerifierResult | None:
    before_state = payload.get("before_state")
    after_state = payload.get("after_state")
    runtime_result = payload.get("runtime_result")
    expected_update_path = _derive_expected_update_path(payload)
    evidence: list[str] = []

    if before_state is not None and after_state is not None:
        changes = _collect_state_changes(before_state, after_state)
        changes = _filter_ignorable_root_artifacts(changes)
        if expected_update_path:
            before_found, before_value = _resolve_workflow_path_value(before_state, expected_update_path)
            after_found, after_value = _resolve_workflow_path_value(after_state, expected_update_path)
            target_changed = before_found != after_found or not _jsonish_equal(before_value, after_value)
            unexpected = _unexpected_changes(changes, expected_update_path)
            if target_changed and not unexpected:
                evidence.append("Primary evidence source: before_state/after_state diff.")
                evidence.append(f"Only the expected update path `{expected_update_path}` changed.")
                return _build_passed_result(
                    summary=f"Observed after_state changed only the expected path `{expected_update_path}`.",
                    evidence=evidence,
                    confidence=1.0,
                )
        elif not changes and runtime_result is not None:
            evidence.append("Primary evidence source: before_state/after_state diff and runtime_result.")
            evidence.append("Workflow state stayed unchanged, which matches a return-only execution path.")
            return _build_passed_result(
                summary="Execution evidence shows no unintended workflow-state mutation.",
                evidence=evidence,
                confidence=1.0,
            )

    if runtime_result is not None and before_state is None and after_state is None:
        return _build_passed_result(
            summary="Concrete runtime_result is present and no contradictory state evidence was provided.",
            evidence=["Primary evidence source: runtime_result."],
            confidence=0.9,
        )

    return None


def _normalize_runtime_state_verifier_result(raw: object) -> RuntimeStateVerifierResult:
    data = raw if isinstance(raw, dict) else {}
    passed = bool(data.get("passed", False))
    severity = str(data.get("severity", "") or "").strip().lower()
    if severity not in _VALID_SEVERITIES:
        severity = "low" if passed else "high"

    summary = str(data.get("summary", "") or "").strip()
    if not summary:
        summary = "Runtime/state check passed." if passed else "Runtime/state verification failed."

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


def _build_semantic_context_lines(payload: RuntimeStateVerifierInput) -> list[str]:
    lines: list[str] = []
    operation = _derive_selected_operation(payload)
    expected_result_action = _derive_expected_result_action(payload)
    if operation:
        lines.append("- selected operation: " + operation)
    if expected_result_action:
        lines.append("- expected result action: " + expected_result_action)
    source_path = _derive_source_field_path(payload)
    if source_path:
        lines.append("- source field path: " + source_path)
    output_path = _derive_output_field_path(payload)
    if output_path:
        lines.append("- output field path: " + output_path)
    expected_update_path = _derive_expected_update_path(payload)
    if expected_update_path:
        lines.append("- expected update path: " + expected_update_path)
    if payload.get("operation_argument") is not None:
        lines.append("- operation argument: " + _compact_json(payload.get("operation_argument"), limit=200))
    semantic_expectations = _ensure_string_list(payload.get("semantic_expectations"))
    if semantic_expectations:
        lines.append("- semantic expectations: " + ", ".join(semantic_expectations))
    return lines


def _build_diff_sections(payload: RuntimeStateVerifierInput) -> tuple[str, str]:
    before_state = payload.get("before_state")
    after_state = payload.get("after_state")
    if before_state is None or after_state is None:
        return "", ""
    changes = _collect_state_changes(before_state, after_state)
    expected_update_path = _derive_expected_update_path(payload)
    relevant = _related_changes(changes, expected_update_path) if expected_update_path else changes
    all_changes_section = "Observed changed paths:\n" + _compact_json(_compact_changes(changes), limit=2000) if changes else "Observed changed paths:\n[]"
    relevant_section = ""
    if expected_update_path:
        relevant_section = (
            f"Relevant diff for {expected_update_path}:\n" + _compact_json(_compact_changes(relevant), limit=2000)
        )
    return all_changes_section, relevant_section


def _build_runtime_state_verifier_prompt(payload: RuntimeStateVerifierInput) -> str:
    task = str(payload.get("task", "") or "").strip()
    code = str(payload.get("code", "") or "").strip()
    context_lines = _build_semantic_context_lines(payload)
    all_changes_section, relevant_section = _build_diff_sections(payload)
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
        }
    focused_sections: list[str] = []
    for path in _unique_strings(
        [
            _derive_source_field_path(payload) or "",
            _derive_output_field_path(payload) or "",
            _derive_expected_update_path(payload) or "",
        ]
    ):
        before_state = payload.get("before_state")
        after_state = payload.get("after_state")
        if before_state is not None:
            found, value = _resolve_workflow_path_value(before_state, path)
            if found:
                focused_sections.append(f"before_state value at {path}:\n{_compact_json(value, limit=800)}")
        if after_state is not None:
            found, value = _resolve_workflow_path_value(after_state, path)
            if found:
                focused_sections.append(f"after_state value at {path}:\n{_compact_json(value, limit=800)}")

    sections = [
        f"Task:\n{task}" if task else "",
        "Scope:\nCheck only execution evidence from before_state, after_state, and runtime_result.",
        "Strict rules:\n"
        "- Use only explicit input data.\n"
        "- Never invent workflow paths, variables, state changes, or runtime evidence.\n"
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
        *focused_sections,
        all_changes_section,
        relevant_section,
        f"Lua solution under review:\n```lua\n{code}\n```",
        _OUTPUT_SCHEMA_TEXT,
    ]
    return _build_prompt_sections(*sections)


def build_runtime_state_verifier_input_from_state(state: dict[str, Any]) -> RuntimeStateVerifierInput:
    compiled_request = state.get("compiled_request")
    if not isinstance(compiled_request, dict):
        compiled_request = {}

    planner_result = compiled_request.get("planner_result")
    if not isinstance(planner_result, dict):
        planner_result = {}

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
        state.get("runtime_state_output_field_path")
        or compiled_request.get("runtime_state_output_field_path")
        or compiled_request.get("selected_save_path")
        or ""
    ).strip()
    expected_result_action = (
        str(state.get("expected_result_action", "") or "").strip()
        or str(planner_result.get("expected_result_action", "") or "").strip()
        or str(compiled_request.get("expected_result_action", "") or "").strip()
    )
    if not expected_result_action:
        if output_field_path:
            expected_result_action = "save_to_wf_vars"
        elif source_field_path and _task_expects_state_update(task):
            expected_result_action = "save_to_wf_vars"
        else:
            expected_result_action = "return"

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
        "expected_result_action": expected_result_action or None,
        "source_field_path": source_field_path or None,
        "output_field_path": output_field_path or None,
        "expected_workflow_paths": expected_workflow_paths,
        "selected_operation": _normalize_nullable_string(compiled_request.get("selected_operation")),
        "operation_argument": compiled_request.get("operation_argument"),
        "semantic_expectations": _ensure_string_list(compiled_request.get("semantic_expectations")),
        "parsed_context": parsed_context,
        "runtime_result": runtime_result,
        "before_state": before_state,
        "after_state": after_state,
        "allowed_workflow_paths": allowed_workflow_paths,
        "available_code_variables": _extract_code_variables(code),
        "available_runtime_evidence": {
            "runtime_result": runtime_result is not None,
            "before_state": before_state is not None,
            "after_state": after_state is not None,
        },
    }


def to_aggregate_verification_result(result: RuntimeStateVerifierResult) -> dict[str, Any]:
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


class RuntimeStateVerifierAgent:
    """LLM-backed runtime/state verifier with evidence-first behavior."""

    def __init__(self, llm: LLMProvider) -> None:
        self._llm = llm

    @property
    def name(self) -> str:
        return _AGENT_NAME

    async def verify(self, payload: RuntimeStateVerifierInput) -> RuntimeStateVerifierResult:
        payload = dict(payload)
        payload["before_state"] = _normalize_workflow_snapshot(payload.get("before_state"))
        payload["after_state"] = _normalize_workflow_snapshot(payload.get("after_state"))
        code = str(payload.get("code", "") or "")
        operation = _derive_selected_operation(payload)
        expected_update_path = _derive_expected_update_path(payload)
        logger.info(
            f"[{_AGENT_NAME}] started",
            code_len=len(code),
            operation=operation or "unknown",
            expected_update_path=expected_update_path or "none",
            has_before_state=payload.get("before_state") is not None,
            has_after_state=payload.get("after_state") is not None,
            has_runtime_result=payload.get("runtime_result") is not None,
        )

        path_mismatch = _evaluate_state_path_mismatch(payload)
        if path_mismatch is not None:
            logger.info(
                f"[{_AGENT_NAME}] path_mismatch",
                error_code=path_mismatch["error_code"],
                field_path=path_mismatch["field_path"] or "none",
            )
            return path_mismatch

        value_mismatch = _evaluate_runtime_value_mismatch(payload)
        if value_mismatch is not None:
            logger.info(
                f"[{_AGENT_NAME}] value_mismatch",
                error_code=value_mismatch["error_code"],
                field_path=value_mismatch["field_path"] or "none",
            )
            return value_mismatch

        positive_result = _build_positive_execution_result(payload)
        if positive_result is not None:
            logger.info(f"[{_AGENT_NAME}] positive_evidence")
            return positive_result

        prompt = _build_runtime_state_verifier_prompt(payload)
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
                summary=f"RuntimeStateVerifier could not produce a valid verdict: {exc}",
                field_path=expected_update_path or _derive_source_field_path(payload),
                expected={"expected_behavior": "Produce a runtime/state verdict for the current execution evidence."},
                actual={"error": str(exc)},
                evidence=["LLM call failed before a structured verifier verdict was produced."],
                must_change=[],
                suggested_patch="",
                patch_scope="none",
                confidence=0.0,
            )

        logger.info(
            f"[{_AGENT_NAME}/llm.generate_json] done",
            raw_keys=list(raw.keys()) if isinstance(raw, dict) else [],
        )
        result = _normalize_runtime_state_verifier_result(raw)
        logger.info(
            f"[{_AGENT_NAME}] completed",
            passed=result["passed"],
            error_code=result["error_code"] or "none",
            field_path=result["field_path"] or "none",
            confidence=result["confidence"],
        )
        return result


def create_runtime_state_verifier_node(llm: LLMProvider) -> Callable:
    agent = RuntimeStateVerifierAgent(llm)

    async def verify_runtime_state(state: dict[str, Any]) -> RuntimeStateVerifierNodeOutput:
        payload = build_runtime_state_verifier_input_from_state(state)
        result = await agent.verify(payload)
        aggregate = to_aggregate_verification_result(result)
        return {
            "runtime_state_verifier_result": result,
            "verification": aggregate,
            "verification_passed": bool(result["passed"]),
            "failure_stage": "" if result["passed"] else "runtime_state_verification",
        }

    return verify_runtime_state
