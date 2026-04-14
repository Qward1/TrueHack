"""Standalone ContractVerifier agent for contract-only workflow checks.

This module is self-contained by design:
- its prompt lives here;
- its input and output schemas live here;
- its normalization and local guard logic live here;
- its bridge into the current aggregate `verification` contract lives here.

Future pipeline slot:
    validate_code
        -> ContractVerifier
        -> shared verification fixer on failure
        -> next verifier in the chain
"""

from __future__ import annotations

import json
import re
from typing import Any, Callable, TypedDict

import structlog

from src.core.llm import LLMProvider

logger = structlog.get_logger(__name__)

_AGENT_NAME = "ContractVerifier"

_SYSTEM_PROMPT = """You are ContractVerifier.
Check only workflow contract:
- correct wf.vars / wf.initVariables paths
- correct return or update target
- correct top-level result shape
- forbid print, io.write, io.read, and invented demo tables
Use only explicit input evidence.
Never invent paths, variables, fields, runtime evidence, or state changes.
If evidence is weak, stay conservative.
Use literal JSON values, not schema placeholder text.
Return JSON only."""

_WORKFLOW_PATH_RE = re.compile(r"wf\.(?:vars|initVariables)(?:\.[A-Za-z_][A-Za-z0-9_]*)+")
_WORKFLOW_ROOT_RE = re.compile(r"\bwf\.(?:vars|initVariables)\b")
_RETURN_WHOLE_WORKFLOW_RE = re.compile(
    r"(?im)^\s*return\s+\(?\s*(wf\.(?:vars|initVariables))\s*\)?\s*$"
)
_RETURN_PATH_RE = re.compile(
    r"(?im)^\s*return\s+\(?\s*(wf\.(?:vars|initVariables)(?:\.[A-Za-z_][A-Za-z0-9_]*)+)\s*\)?\s*$"
)
_LOCAL_TABLE_ASSIGN_RE = re.compile(r"(?im)^\s*local\s+([A-Za-z_][A-Za-z0-9_]*)\s*=\s*\{")
_FORBIDDEN_CALL_PATTERNS = (
    ("print", re.compile(r"(?<![\w.])print\s*\(")),
    ("io.write", re.compile(r"\bio\.write\s*\(")),
    ("io.read", re.compile(r"\bio\.read\s*\(")),
)

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


class ContractVerifierInput(TypedDict, total=False):
    """Canonical input contract for ContractVerifier."""

    task: str
    code: str
    expected_workflow_paths: list[str]
    expected_result_action: str
    expected_return_path: str | None
    expected_update_path: str | None
    expected_top_level_type: str | None
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


class ContractVerifierResult(TypedDict):
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


class ContractVerifierNodeOutput(TypedDict, total=False):
    contract_verifier_result: ContractVerifierResult
    verification: dict[str, Any]
    verification_passed: bool
    failure_stage: str


_OUTPUT_SHAPE_EXAMPLE: dict[str, Any] = {
    "verifier_name": _AGENT_NAME,
    "passed": True,
    "error_family": None,
    "error_code": None,
    "severity": "low",
    "summary": "Contract check passed.",
    "field_path": None,
    "evidence": [],
    "expected": {},
    "actual": {},
    "fixer_brief": {
        "goal": "",
        "must_change": [],
        "must_preserve": [],
        "forbidden_fixes": [],
        "suggested_patch": "",
        "patch_scope": "none",
    },
    "confidence": 0.0,
}

_OUTPUT_SCHEMA_TEXT = """Return JSON only with keys:
verifier_name, passed, error_family, error_code, severity, summary,
field_path, evidence, expected, actual, fixer_brief, confidence.

fixer_brief keys:
goal, must_change, must_preserve, forbidden_fixes, suggested_patch, patch_scope.

Rules:
- Keep evidence, expected, actual, and fixer_brief minimal.
- field_path must be null or an exact path proven by the input.
- Use a literal severity such as "low" or "high".
- Do not add any path or variable that is not explicitly available."""


def _ensure_string_list(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item).strip() for item in value if str(item).strip()]


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


def _compact_json(value: object, limit: int = 1000) -> str:
    try:
        rendered = json.dumps(value, ensure_ascii=False, indent=2)
    except Exception:
        rendered = str(value)
    if len(rendered) <= limit:
        return rendered
    return rendered[: limit - 3].rstrip() + "..."


def _summarize_for_prompt(value: object, *, max_items: int = 2, max_keys: int = 6) -> object:
    if isinstance(value, list):
        summary = [_summarize_for_prompt(item, max_items=max_items, max_keys=max_keys) for item in value[:max_items]]
        if len(value) > max_items:
            summary.append(f"... {len(value) - max_items} more items")
        return summary
    if isinstance(value, dict):
        keys = list(value.keys())
        summary: dict[str, Any] = {}
        for key in keys[:max_keys]:
            summary[str(key)] = _summarize_for_prompt(value[key], max_items=max_items, max_keys=max_keys)
        if len(keys) > max_keys:
            summary["..."] = f"{len(keys) - max_keys} more keys"
        return summary
    return value


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


def _build_prompt_sections(*sections: str) -> str:
    return "\n\n".join(section for section in sections if section and section.strip())


def _find_forbidden_calls(code: str) -> list[str]:
    found: list[str] = []
    for label, pattern in _FORBIDDEN_CALL_PATTERNS:
        if pattern.search(code or ""):
            found.append(label)
    return found


def _looks_like_demo_data(code: str) -> bool:
    stripped = str(code or "").strip()
    if not stripped or _WORKFLOW_ROOT_RE.search(stripped):
        return False
    return bool(_LOCAL_TABLE_ASSIGN_RE.search(stripped))


def _normalize_fixer_brief(raw: object, *, passed: bool) -> FixerBrief:
    data = raw if isinstance(raw, dict) else {}
    patch_scope = str(data.get("patch_scope", "") or "").strip()
    if patch_scope not in _VALID_PATCH_SCOPES:
        patch_scope = "none" if passed else "local"
    goal = str(data.get("goal", "") or "").strip()
    suggested_patch = str(data.get("suggested_patch", "") or "").strip()
    if not passed and not goal:
        goal = "Fix the contract mismatch only."
    return {
        "goal": goal,
        "must_change": _ensure_string_list(data.get("must_change")),
        "must_preserve": _ensure_string_list(data.get("must_preserve")),
        "forbidden_fixes": _ensure_string_list(data.get("forbidden_fixes")),
        "suggested_patch": suggested_patch,
        "patch_scope": patch_scope,
    }


def _build_failed_result(
    *,
    error_family: str,
    error_code: str,
    severity: str,
    summary: str,
    field_path: str | None = None,
    evidence: list[str] | None = None,
    expected: dict[str, Any] | None = None,
    actual: dict[str, Any] | None = None,
    fixer_brief: FixerBrief | None = None,
    confidence: float = 1.0,
) -> ContractVerifierResult:
    normalized_fixer = fixer_brief or {
        "goal": "Fix the contract mismatch only.",
        "must_change": [],
        "must_preserve": [],
        "forbidden_fixes": [],
        "suggested_patch": "",
        "patch_scope": "local",
    }
    return {
        "verifier_name": _AGENT_NAME,
        "passed": False,
        "error_family": error_family,
        "error_code": error_code,
        "severity": severity if severity in _VALID_SEVERITIES else "high",
        "summary": str(summary or "Contract verification failed.").strip(),
        "field_path": _normalize_nullable_string(field_path),
        "evidence": _ensure_string_list(evidence or []),
        "expected": _ensure_object(expected or {}),
        "actual": _ensure_object(actual or {}),
        "fixer_brief": _normalize_fixer_brief(normalized_fixer, passed=False),
        "confidence": _clamp_confidence(confidence),
    }


def _build_passed_result(
    *,
    summary: str,
    evidence: list[str] | None = None,
    expected: dict[str, Any] | None = None,
    actual: dict[str, Any] | None = None,
    confidence: float = 1.0,
) -> ContractVerifierResult:
    return {
        "verifier_name": _AGENT_NAME,
        "passed": True,
        "error_family": None,
        "error_code": None,
        "severity": "low",
        "summary": str(summary or "Contract check passed.").strip(),
        "field_path": None,
        "evidence": _ensure_string_list(evidence or []),
        "expected": _ensure_object(expected or {}),
        "actual": _ensure_object(actual or {}),
        "fixer_brief": _normalize_fixer_brief({}, passed=True),
        "confidence": _clamp_confidence(confidence),
    }


def _jsonish_signature(value: object) -> str:
    try:
        return json.dumps(value, ensure_ascii=False, sort_keys=True)
    except Exception:
        return repr(value)


def _jsonish_equal(left: object, right: object) -> bool:
    return _jsonish_signature(left) == _jsonish_signature(right)


def _normalize_contract_type(value: object) -> str | None:
    text = str(value or "").strip().lower()
    mapping = {
        "scalar": "scalar",
        "string": "scalar",
        "number": "scalar",
        "boolean": "scalar",
        "bool": "scalar",
        "object": "object-like table",
        "object-like table": "object-like table",
        "object_like_table": "object-like table",
        "map": "object-like table",
        "array": "array-like table",
        "list": "array-like table",
        "array-like table": "array-like table",
        "array_like_table": "array-like table",
        "empty table": "empty table",
        "empty_table": "empty table",
        "nil": "nil",
    }
    normalized = mapping.get(text)
    return normalized or None


def _classify_top_level_type(value: object) -> str:
    if value is None:
        return "nil"
    if isinstance(value, (bool, int, float, str)):
        return "scalar"
    if isinstance(value, list):
        return "empty table" if not value else "array-like table"
    if isinstance(value, dict):
        if not value:
            return "empty table"
        numeric_keys: list[int] = []
        for key in value.keys():
            if isinstance(key, int):
                numeric_keys.append(int(key))
                continue
            if isinstance(key, str) and key.isdigit():
                numeric_keys.append(int(key))
                continue
            return "object-like table"
        numeric_keys.sort()
        if numeric_keys == list(range(1, len(numeric_keys) + 1)):
            return "array-like table"
        return "object-like table"
    return "object-like table"


def _split_workflow_path(path: str) -> list[str]:
    normalized = str(path or "").strip()
    if not normalized.startswith("wf."):
        return []
    return [token for token in normalized.split(".") if token]


def _resolve_workflow_path_value(root: object, path: str) -> tuple[bool, object]:
    tokens = _split_workflow_path(path)
    if not tokens:
        return False, None
    current = root
    for token in tokens:
        if isinstance(current, dict) and token in current:
            current = current[token]
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


def _task_expects_state_update(task: str) -> bool:
    lowered = str(task or "").lower()
    return any(marker in lowered for marker in _SAVE_MARKERS)


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


def _is_ignorable_root_artifact(change: dict[str, Any]) -> bool:
    path = str(change.get("path", "") or "")
    if path not in {"wf.vars", "wf.initVariables"}:
        return False
    return _is_empty_container_like(change.get("before")) and _is_empty_container_like(change.get("after"))


def _filter_ignorable_root_artifacts(changes: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [change for change in changes if not _is_ignorable_root_artifact(change)]


def _collect_state_changes(before: object, after: object, prefix: str = "") -> list[dict[str, Any]]:
    if _jsonish_equal(before, after):
        return []
    path = prefix
    if isinstance(before, dict) and isinstance(after, dict):
        changes: list[dict[str, Any]] = []
        for key in sorted(set(before.keys()) | set(after.keys()), key=str):
            child_path = f"{path}.{key}" if path else str(key)
            if key not in before or key not in after:
                changes.append({"path": child_path, "before": before.get(key), "after": after.get(key)})
                continue
            changes.extend(_collect_state_changes(before.get(key), after.get(key), child_path))
        return changes
    if isinstance(before, list) and isinstance(after, list):
        return [{"path": path or "root", "before": before, "after": after}]
    return [{"path": path or "root", "before": before, "after": after}]


def _extract_direct_return_path(code: str) -> str | None:
    match = _RETURN_PATH_RE.search(str(code or ""))
    if not match:
        return None
    return _normalize_nullable_string(match.group(1))


def _evaluate_contract_evidence(payload: ContractVerifierInput) -> ContractVerifierResult | None:
    expected_result_action = _normalize_nullable_string(payload.get("expected_result_action")) or ""
    expected_return_path = _normalize_nullable_string(payload.get("expected_return_path"))
    expected_update_path = _normalize_nullable_string(payload.get("expected_update_path"))
    expected_top_level_type = _normalize_contract_type(payload.get("expected_top_level_type"))
    runtime_result = payload.get("runtime_result")
    before_state = payload.get("before_state")
    after_state = payload.get("after_state")
    code = str(payload.get("code", "") or "")

    if expected_result_action == "save_to_wf_vars" and expected_update_path and before_state is not None and after_state is not None:
        changes = _collect_state_changes(before_state, after_state, "")
        changes = _filter_ignorable_root_artifacts(changes)
        changed_paths = [change["path"] for change in changes]
        before_found, before_value = _resolve_workflow_path_value(before_state, expected_update_path)
        after_found, after_value = _resolve_workflow_path_value(after_state, expected_update_path)
        target_changed = before_found != after_found or not _jsonish_equal(before_value, after_value)
        related_changes = [change for change in changes if _paths_related(expected_update_path, str(change["path"]))]
        if not target_changed:
            wrong_change = next((change for change in changes if change["path"].startswith("wf.")), None)
            if wrong_change is not None:
                return _build_failed_result(
                    error_family="workflow_path",
                    error_code="wrong_update_path",
                    severity="high",
                    summary=(
                        f"The execution updated `{wrong_change['path']}` instead of the required "
                        f"`{expected_update_path}`."
                    ),
                    field_path=str(wrong_change["path"]),
                    evidence=[
                        "Concrete evidence source: before_state/after_state diff.",
                        f"Expected update path `{expected_update_path}` did not change.",
                    ],
                    expected={"update_path": expected_update_path},
                    actual={"changed_paths": changed_paths, "relevant_diff": changes[:5]},
                    fixer_brief={
                        "goal": "Write the result to the correct workflow path only.",
                        "must_change": [f"Update `{expected_update_path}` instead of `{wrong_change['path']}`."],
                        "must_preserve": ["Keep the existing business logic if it already computes the right value."],
                        "forbidden_fixes": ["Do not change unrelated workflow paths."],
                        "suggested_patch": f"Move the save/update target to `{expected_update_path}`.",
                        "patch_scope": "local",
                    },
                    confidence=1.0,
                )
            return _build_failed_result(
                error_family="update_contract",
                error_code="required_field_unchanged",
                severity="high",
                summary=f"The required workflow target `{expected_update_path}` did not change after execution.",
                field_path=expected_update_path,
                evidence=[
                    "Concrete evidence source: before_state/after_state diff.",
                    f"No change was recorded at `{expected_update_path}`.",
                ],
                expected={"update_path": expected_update_path},
                actual={"changed_paths": changed_paths},
                fixer_brief={
                    "goal": "Write the final result to the required workflow path.",
                    "must_change": [f"Assign or update `{expected_update_path}` with the final result."],
                    "must_preserve": ["Keep unrelated workflow fields unchanged."],
                    "forbidden_fixes": ["Do not leave the target field untouched."],
                    "suggested_patch": f"Store the final result into `{expected_update_path}`.",
                    "patch_scope": "local",
                },
                confidence=1.0,
            )
        actual_type = _classify_top_level_type(after_value)
        if expected_top_level_type and actual_type != expected_top_level_type:
            return _build_failed_result(
                error_family="result_shape",
                error_code="wrong_top_level_type",
                severity="high",
                summary=(
                    f"`{expected_update_path}` has the wrong top-level result shape after execution: "
                    f"expected `{expected_top_level_type}`, got `{actual_type}`."
                ),
                field_path=expected_update_path,
                evidence=[
                    "Concrete evidence source: after_state target value.",
                    f"Observed `{expected_update_path}` as `{actual_type}` after execution.",
                ],
                expected={"top_level_type": expected_top_level_type, "update_path": expected_update_path},
                actual={"top_level_type": actual_type, "value": after_value},
                fixer_brief={
                    "goal": "Preserve the update target but fix the top-level result shape only.",
                    "must_change": [f"Normalize the final value at `{expected_update_path}` to `{expected_top_level_type}`."],
                    "must_preserve": ["Keep the existing workflow path target."],
                    "forbidden_fixes": ["Do not change the required workflow target."],
                    "suggested_patch": f"Keep writing to `{expected_update_path}` but normalize the result shape to `{expected_top_level_type}`.",
                    "patch_scope": "local",
                },
                confidence=1.0,
            )
        return _build_passed_result(
            summary=f"Concrete execution evidence shows the required workflow target `{expected_update_path}` was updated.",
            evidence=[
                "Concrete evidence source: before_state/after_state diff.",
                f"`{expected_update_path}` changed after execution.",
            ],
            expected={"update_path": expected_update_path, "top_level_type": expected_top_level_type},
            actual={
                "update_path": expected_update_path,
                "top_level_type": actual_type,
                "changed_paths": changed_paths,
                "relevant_diff": related_changes[:5],
            },
            confidence=0.96,
        )

    if expected_top_level_type and runtime_result is not None:
        actual_type = _classify_top_level_type(runtime_result)
        if actual_type != expected_top_level_type:
            return _build_failed_result(
                error_family="result_shape",
                error_code="wrong_top_level_type",
                severity="high",
                summary=(
                    f"The runtime result has the wrong top-level shape: expected "
                    f"`{expected_top_level_type}`, got `{actual_type}`."
                ),
                evidence=[
                    "Concrete evidence source: runtime_result.",
                    f"Observed runtime_result as `{actual_type}`.",
                ],
                expected={"top_level_type": expected_top_level_type},
                actual={"top_level_type": actual_type, "runtime_result": runtime_result},
                fixer_brief={
                    "goal": "Fix only the top-level result shape.",
                    "must_change": [f"Normalize the returned value to `{expected_top_level_type}`."],
                    "must_preserve": ["Keep the existing business logic."],
                    "forbidden_fixes": ["Do not replace the whole workflow contract with a different one."],
                    "suggested_patch": f"Wrap or unwrap the final return so the top-level shape becomes `{expected_top_level_type}`.",
                    "patch_scope": "local",
                },
                confidence=1.0,
            )

    if expected_result_action == "return" and expected_return_path and runtime_result is not None:
        roots = [before_state, after_state]
        for root in roots:
            found_expected, expected_value = _resolve_workflow_path_value(root, expected_return_path) if root is not None else (False, None)
            if found_expected and _jsonish_equal(runtime_result, expected_value):
                return _build_passed_result(
                    summary=f"Concrete runtime evidence matches the expected return target `{expected_return_path}`.",
                    evidence=[
                        "Concrete evidence source: runtime_result matched the expected workflow path value.",
                        f"Resolved `{expected_return_path}` to the same value as runtime_result.",
                    ],
                    expected={"return_path": expected_return_path},
                    actual={"return_path": expected_return_path, "runtime_result": runtime_result},
                    confidence=0.95,
                )

        actual_return_path = _extract_direct_return_path(code)
        if actual_return_path and actual_return_path != expected_return_path:
            for root in roots:
                found_actual, actual_value = _resolve_workflow_path_value(root, actual_return_path) if root is not None else (False, None)
                if found_actual and _jsonish_equal(runtime_result, actual_value):
                    return _build_failed_result(
                        error_family="return_contract",
                        error_code="wrong_return_path",
                        severity="high",
                        summary=(
                            f"The script returns `{actual_return_path}` instead of the required "
                            f"`{expected_return_path}`."
                        ),
                        field_path=actual_return_path,
                        evidence=[
                            "Concrete evidence source: runtime_result plus explicit return path.",
                            f"runtime_result matches `{actual_return_path}`, not `{expected_return_path}`.",
                        ],
                        expected={"return_path": expected_return_path},
                        actual={"return_path": actual_return_path, "runtime_result": runtime_result},
                        fixer_brief={
                            "goal": "Return the correct workflow target only.",
                            "must_change": [f"Return `{expected_return_path}` instead of `{actual_return_path}`."],
                            "must_preserve": ["Keep the existing business logic if it already computes the right value."],
                            "forbidden_fixes": ["Do not return the wrong workflow path or the whole workflow table."],
                            "suggested_patch": f"Replace the final return target with `{expected_return_path}`.",
                            "patch_scope": "local",
                        },
                        confidence=1.0,
                    )

    return None


def _detect_local_contract_failure(payload: ContractVerifierInput) -> ContractVerifierResult | None:
    code = str(payload.get("code", "") or "")
    forbidden_calls = _find_forbidden_calls(code)
    if forbidden_calls:
        must_change = [f"Remove `{name}` usage from the workflow script." for name in forbidden_calls]
        forbidden_fixes = [f"Do not replace `{name}` with another console I/O call." for name in forbidden_calls]
        return _build_failed_result(
            error_family="forbidden_pattern",
            error_code="forbidden_io",
            severity="critical",
            summary=(
                "Forbidden console I/O detected: "
                + ", ".join(forbidden_calls)
                + ". Workflow scripts must return a value or update wf.vars / wf.initVariables directly."
            ),
            evidence=[f"Code uses {name}." for name in forbidden_calls],
            expected={"forbidden_patterns": ["print", "io.write", "io.read"]},
            actual={"forbidden_patterns": forbidden_calls},
            fixer_brief={
                "goal": "Remove forbidden console I/O without changing the business logic.",
                "must_change": must_change,
                "must_preserve": [
                    "Keep the existing workflow data flow and business logic.",
                ],
                "forbidden_fixes": forbidden_fixes,
                "suggested_patch": "Delete the console I/O call and keep the result in return or wf.vars/wf.initVariables update form.",
                "patch_scope": "local",
            },
            confidence=1.0,
        )

    whole_workflow_match = _RETURN_WHOLE_WORKFLOW_RE.search(code)
    if whole_workflow_match:
        returned_root = str(whole_workflow_match.group(1) or "").strip()
        return _build_failed_result(
            error_family="return_contract",
            error_code="return_whole_workflow_table",
            severity="high",
            summary=(
                f"The script returns `{returned_root}` as a whole table instead of the requested contract result."
            ),
            field_path=returned_root,
            evidence=[f"Found `return {returned_root}` at top-level return."],
            expected={"return_value": "targeted result only"},
            actual={"return_value": returned_root},
            fixer_brief={
                "goal": "Return only the requested result instead of the whole workflow table.",
                "must_change": [f"Replace `return {returned_root}` with the exact requested value or target path result."],
                "must_preserve": ["Keep the current computation logic if it is otherwise correct."],
                "forbidden_fixes": ["Do not return `wf.vars` or `wf.initVariables` wholesale."],
                "suggested_patch": "Return the final target value or store it in the expected wf path; do not expose the whole workflow root.",
                "patch_scope": "local",
            },
            confidence=1.0,
        )

    if _looks_like_demo_data(code):
        local_tables = [match.group(1) for match in _LOCAL_TABLE_ASSIGN_RE.finditer(code)]
        return _build_failed_result(
            error_family="workflow_data",
            error_code="demo_data",
            severity="critical",
            summary="The script appears to use invented demo tables instead of workflow data from wf.vars / wf.initVariables.",
            evidence=[f"Detected local table literal `{name}` without any workflow-path access." for name in local_tables[:3]],
            expected={"data_source": "wf.vars / wf.initVariables"},
            actual={"data_source": "local table literals", "table_names": local_tables[:5]},
            fixer_brief={
                "goal": "Replace invented demo data with direct workflow-path access.",
                "must_change": ["Read input from wf.vars / wf.initVariables instead of local demo tables."],
                "must_preserve": ["Keep only the transformation logic that can run on real workflow data."],
                "forbidden_fixes": ["Do not keep hardcoded sample arrays, objects, or fake input payloads."],
                "suggested_patch": "Remove the demo table and bind the logic to the real workflow path referenced by the task/context.",
                "patch_scope": "function_level",
            },
            confidence=0.98,
        )

    return None


def _normalize_contract_verifier_result(raw: object) -> ContractVerifierResult:
    data = raw if isinstance(raw, dict) else {}
    passed = bool(data.get("passed", False))
    severity = str(data.get("severity", "") or "").strip().lower()
    if severity not in _VALID_SEVERITIES:
        severity = "low" if passed else "high"

    summary = str(data.get("summary", "") or "").strip()
    if not summary:
        summary = "Contract check passed." if passed else "Contract verification failed."

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


def _build_contract_verifier_prompt(payload: ContractVerifierInput) -> str:
    task = str(payload.get("task", "") or "").strip()
    code = str(payload.get("code", "") or "").strip()
    expected_paths = _unique_strings(
        [str(item).strip() for item in payload.get("expected_workflow_paths", []) or [] if str(item).strip()]
    )
    allowed_workflow_paths = _unique_strings(
        _ensure_string_list(payload.get("allowed_workflow_paths")) or expected_paths
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
    expected_result_action = str(payload.get("expected_result_action", "") or "").strip()
    expected_return_path = _normalize_nullable_string(payload.get("expected_return_path"))
    expected_update_path = _normalize_nullable_string(payload.get("expected_update_path"))
    expected_top_level_type = _normalize_nullable_string(payload.get("expected_top_level_type"))

    expected_lines: list[str] = []
    if expected_paths:
        expected_lines.append("- expected workflow paths: " + ", ".join(expected_paths))
    if expected_result_action:
        expected_lines.append("- expected result action: " + expected_result_action)
    if expected_return_path:
        expected_lines.append("- expected return path/value source: " + expected_return_path)
    if expected_update_path:
        expected_lines.append("- expected update/save path: " + expected_update_path)
    if expected_top_level_type:
        expected_lines.append("- expected top-level result shape: " + expected_top_level_type)

    focused_sections: list[str] = []
    if expected_return_path and payload.get("before_state") is not None:
        found, value = _resolve_workflow_path_value(payload.get("before_state"), expected_return_path)
        if found:
            focused_sections.append(
                f"before_state value at {expected_return_path}:\n{_compact_json(_summarize_for_prompt(value), limit=350)}"
            )
    if expected_update_path and payload.get("before_state") is not None:
        found, value = _resolve_workflow_path_value(payload.get("before_state"), expected_update_path)
        if found:
            focused_sections.append(
                f"before_state value at {expected_update_path}:\n{_compact_json(_summarize_for_prompt(value), limit=350)}"
            )
    if expected_update_path and payload.get("after_state") is not None:
        found, value = _resolve_workflow_path_value(payload.get("after_state"), expected_update_path)
        if found:
            focused_sections.append(
                f"after_state value at {expected_update_path}:\n{_compact_json(_summarize_for_prompt(value), limit=350)}"
            )

    sections = [
        f"Task:\n{task}" if task else "",
        "Scope:\nCheck contract only. Ignore business logic.",
        "Strict rules:\n"
        "- Use only explicit input data.\n"
        "- Never invent workflow paths, variables, fields, or runtime evidence.\n"
        "- You may reference only names from allowed_workflow_paths, available_code_variables, or focused evidence sections.\n"
        "- Do not fail on suspicion alone. Cite exact evidence or keep the verdict conservative.",
        "Expected contract:\n" + "\n".join(expected_lines) if expected_lines else "",
        _format_named_list("allowed_workflow_paths", allowed_workflow_paths),
        _format_named_list("available_code_variables", available_code_variables),
        _render_presence_map("available_runtime_evidence", available_runtime_evidence),
        (
            "runtime_result:\n" + _compact_json(_summarize_for_prompt(payload.get("runtime_result")), limit=350)
            if payload.get("runtime_result") is not None
            else ""
        ),
        *focused_sections,
        "Lua solution under review:\n" + code,
        _OUTPUT_SCHEMA_TEXT,
    ]
    return _build_prompt_sections(*sections)


def build_contract_verifier_input_from_state(state: dict[str, Any]) -> ContractVerifierInput:
    """Bridge current pipeline state into the new internal ContractVerifier schema."""
    compiled_request = state.get("compiled_request")
    if not isinstance(compiled_request, dict):
        compiled_request = {}

    diagnostics = state.get("diagnostics")
    if not isinstance(diagnostics, dict):
        diagnostics = {}

    planner_result = compiled_request.get("planner_result")
    if not isinstance(planner_result, dict):
        planner_result = {}

    task = (
        str(compiled_request.get("verification_prompt", "") or "").strip()
        or str(compiled_request.get("task_text", "") or "").strip()
        or str(compiled_request.get("original_task", "") or "").strip()
        or str(state.get("user_input", "") or "").strip()
    )

    selected_primary_path = str(compiled_request.get("selected_primary_path", "") or "").strip()
    selected_save_path = str(compiled_request.get("selected_save_path", "") or "").strip()
    expected_paths = _unique_strings(
        _ensure_string_list(compiled_request.get("expected_workflow_paths"))
        + ([selected_primary_path] if selected_primary_path else [])
        + ([selected_save_path] if selected_save_path else [])
        + _ensure_string_list(planner_result.get("identified_workflow_paths"))
        + _extract_workflow_paths(task)
    )
    code = str(state.get("generated_code", "") or state.get("current_code", "") or "")
    allowed_workflow_paths = _unique_strings(
        _extract_inventory_paths(compiled_request.get("workflow_path_inventory"))
        + _extract_workflow_paths(code)
        + ([selected_primary_path] if selected_primary_path else [])
        + ([selected_save_path] if selected_save_path else [])
        + expected_paths
    )

    expected_result_action = (
        str(state.get("expected_result_action", "") or "").strip()
        or str(planner_result.get("expected_result_action", "") or "").strip()
        or str(compiled_request.get("expected_result_action", "") or "").strip()
    )
    if not expected_result_action:
        if selected_save_path:
            expected_result_action = "save_to_wf_vars"
        elif selected_primary_path and _task_expects_state_update(task):
            expected_result_action = "save_to_wf_vars"
        else:
            expected_result_action = "return"

    expected_return_path: str | None = None
    expected_update_path: str | None = None
    if expected_result_action == "return" and selected_primary_path:
        expected_return_path = selected_primary_path
    if expected_result_action == "save_to_wf_vars":
        expected_update_path = selected_save_path or selected_primary_path

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
        "expected_workflow_paths": expected_paths,
        "expected_result_action": expected_result_action,
        "expected_return_path": expected_return_path,
        "expected_update_path": expected_update_path,
        "expected_top_level_type": _normalize_nullable_string(
            state.get("contract_expected_top_level_type")
            or compiled_request.get("contract_expected_top_level_type")
        ),
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


def to_aggregate_verification_result(result: ContractVerifierResult) -> dict[str, Any]:
    """Adapt the detailed internal verdict into the current system aggregate contract."""
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


class ContractVerifierAgent:
    """LLM-backed contract-only verifier with evidence-first behavior."""

    def __init__(self, llm: LLMProvider) -> None:
        self._llm = llm

    @property
    def name(self) -> str:
        return _AGENT_NAME

    async def verify(self, payload: ContractVerifierInput) -> ContractVerifierResult:
        payload = dict(payload)
        payload["before_state"] = _normalize_workflow_snapshot(payload.get("before_state"))
        payload["after_state"] = _normalize_workflow_snapshot(payload.get("after_state"))
        code = str(payload.get("code", "") or "")
        expected_paths = _unique_strings(_ensure_string_list(payload.get("expected_workflow_paths")))

        logger.info(
            f"[{_AGENT_NAME}] started",
            code_len=len(code),
            expected_path_count=len(expected_paths),
            has_runtime_result=payload.get("runtime_result") is not None,
            has_after_state=payload.get("after_state") is not None,
        )

        evidence_result = _evaluate_contract_evidence(payload)
        if evidence_result is not None:
            logger.info(
                f"[{_AGENT_NAME}] evidence_result",
                passed=evidence_result["passed"],
                error_code=evidence_result["error_code"] or "none",
                field_path=evidence_result["field_path"] or "none",
            )
            return evidence_result

        local_failure = _detect_local_contract_failure(payload)
        if local_failure is not None:
            logger.info(
                f"[{_AGENT_NAME}] local_failure",
                error_code=local_failure["error_code"],
                severity=local_failure["severity"],
            )
            return local_failure

        prompt = _build_contract_verifier_prompt(payload)
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
                severity="high",
                summary=f"ContractVerifier could not produce a valid verdict: {exc}",
                evidence=["LLM call failed before a structured verifier verdict was produced."],
                fixer_brief={
                    "goal": "",
                    "must_change": [],
                    "must_preserve": [],
                    "forbidden_fixes": [],
                    "suggested_patch": "",
                    "patch_scope": "none",
                },
                confidence=0.0,
            )

        logger.info(
            f"[{_AGENT_NAME}/llm.generate_json] done",
            raw_keys=list(raw.keys()) if isinstance(raw, dict) else [],
        )

        result = _normalize_contract_verifier_result(raw)
        logger.info(
            f"[{_AGENT_NAME}] completed",
            passed=result["passed"],
            error_code=result["error_code"] or "none",
            severity=result["severity"],
            confidence=result["confidence"],
        )
        return result


def create_contract_verifier_node(llm: LLMProvider) -> Callable:
    """Factory for the active contract-verification stage node."""
    agent = ContractVerifierAgent(llm)

    async def contract_verify(state: dict[str, Any]) -> ContractVerifierNodeOutput:
        payload = build_contract_verifier_input_from_state(state)
        result = await agent.verify(payload)
        aggregate = to_aggregate_verification_result(result)
        return {
            "contract_verifier_result": result,
            "verification": aggregate,
            "verification_passed": bool(result["passed"]),
            "failure_stage": "" if result["passed"] else "contract_verification",
        }

    return contract_verify
