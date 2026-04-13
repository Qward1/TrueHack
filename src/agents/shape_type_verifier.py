"""Standalone ShapeTypeVerifier agent for shape/type-only workflow checks."""

from __future__ import annotations

import json
import re
from typing import Any, Callable, TypedDict

import structlog

from src.core.llm import LLMProvider

logger = structlog.get_logger(__name__)

_AGENT_NAME = "ShapeTypeVerifier"

_SYSTEM_PROMPT = """You are ShapeTypeVerifier.
Check only data shape and type errors in a Lua workflow solution.
Do not judge full business logic.
Distinguish:
- scalar
- object-like table
- array-like table
- empty table
Check:
- object left as object when array is required
- array damaged into object or scalar
- normalization applied at the wrong level
- shape-sensitive logic that relies only on type(x) == "table"
- _utils.array.markAsArray applied to the wrong level
Use runtime_result and after_state as primary evidence when present.
Return JSON only.
If passed=false, point to the exact field path, expected shape, actual shape, and a minimal patch."""

_WORKFLOW_PATH_RE = re.compile(r"wf\.(?:vars|initVariables)(?:\.[A-Za-z_][A-Za-z0-9_]*)+")
_TYPE_TABLE_RE = re.compile(r"type\s*\(\s*[^)]+\)\s*[~=]=\s*['\"]table['\"]")
_NUMERIC_KEY_GUARD_PATTERNS = (
    re.compile(r"for\s+\w+\s+in\s+pairs\s*\("),
    re.compile(r"type\s*\(\s*\w+\s*\)\s*[~=]=\s*['\"]number['\"]"),
    re.compile(r"math\.floor\s*\("),
    re.compile(r"ipairs\s*\("),
)
_MARK_AS_ARRAY_RE = re.compile(r"_utils\.array\.markAsArray\s*\(\s*([^)]+?)\s*\)")
_ARRAY_NEW_RE = re.compile(r"_utils\.array\.new\s*\(")

_VALID_SEVERITIES = frozenset({"low", "medium", "high", "critical"})
_VALID_PATCH_SCOPES = frozenset({"none", "local", "function_level", "multi_block", "rewrite"})
_VALID_SHAPES = frozenset({"scalar", "object-like table", "array-like table", "empty table", "nil"})


class ShapeTypeVerifierInput(TypedDict, total=False):
    task: str
    code: str
    target_field_path: str | None
    expected_workflow_paths: list[str]
    selected_primary_type: str | None
    semantic_expectations: list[str]
    expected_shape: str | None
    parsed_context: object
    runtime_result: object
    before_state: object
    after_state: object


class FixerBrief(TypedDict):
    goal: str
    must_change: list[str]
    must_preserve: list[str]
    forbidden_fixes: list[str]
    suggested_patch: str
    patch_scope: str


class ShapeTypeVerifierResult(TypedDict):
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


class ShapeTypeVerifierNodeOutput(TypedDict, total=False):
    shape_type_verifier_result: ShapeTypeVerifierResult
    verification: dict[str, Any]
    verification_passed: bool
    failure_stage: str


_OUTPUT_SHAPE_EXAMPLE: dict[str, Any] = {
    "verifier_name": _AGENT_NAME,
    "passed": True,
    "error_family": None,
    "error_code": None,
    "severity": "low",
    "summary": "Shape/type check passed.",
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


def _extract_workflow_paths(text: str) -> list[str]:
    if not text:
        return []
    return _unique_strings(_WORKFLOW_PATH_RE.findall(text))


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
        rendered = json.dumps(value, ensure_ascii=False, indent=2)
    except Exception:
        rendered = str(value)
    if len(rendered) <= limit:
        return rendered
    return rendered[: limit - 3].rstrip() + "..."


def _build_prompt_sections(*sections: str) -> str:
    return "\n\n".join(section for section in sections if section and section.strip())


def _normalize_fixer_brief(raw: object, *, passed: bool) -> FixerBrief:
    data = raw if isinstance(raw, dict) else {}
    patch_scope = str(data.get("patch_scope", "") or "").strip()
    if patch_scope not in _VALID_PATCH_SCOPES:
        patch_scope = "none" if passed else "local"
    goal = str(data.get("goal", "") or "").strip()
    suggested_patch = str(data.get("suggested_patch", "") or "").strip()
    if not passed and not goal:
        goal = "Fix the shape/type mismatch only."
    return {
        "goal": goal,
        "must_change": _ensure_string_list(data.get("must_change")),
        "must_preserve": _ensure_string_list(data.get("must_preserve")),
        "forbidden_fixes": _ensure_string_list(data.get("forbidden_fixes")),
        "suggested_patch": suggested_patch,
        "patch_scope": patch_scope,
    }


def _normalize_shape(value: object) -> str | None:
    if value is None:
        return None
    text = str(value or "").strip().lower()
    mapping = {
        "scalar": "scalar",
        "object-like table": "object-like table",
        "object_like_table": "object-like table",
        "object": "object-like table",
        "array-like table": "array-like table",
        "array_like_table": "array-like table",
        "array": "array-like table",
        "empty table": "empty table",
        "empty_table": "empty table",
        "nil": "nil",
    }
    normalized = mapping.get(text)
    if normalized in _VALID_SHAPES:
        return normalized
    return None


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
) -> ShapeTypeVerifierResult:
    normalized_fixer = fixer_brief or {
        "goal": "Fix the shape/type mismatch only.",
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
        "summary": str(summary or "Shape/type verification failed.").strip(),
        "field_path": _normalize_nullable_string(field_path),
        "evidence": _ensure_string_list(evidence or []),
        "expected": _ensure_object(expected or {}),
        "actual": _ensure_object(actual or {}),
        "fixer_brief": _normalize_fixer_brief(normalized_fixer, passed=False),
        "confidence": _clamp_confidence(confidence),
    }


def _classify_shape(value: object) -> str:
    if value is None:
        return "nil"
    if isinstance(value, bool):
        return "scalar"
    if isinstance(value, (int, float, str)):
        return "scalar"
    if isinstance(value, list):
        return "empty table" if len(value) == 0 else "array-like table"
    if isinstance(value, dict):
        if not value:
            return "empty table"
        numeric_keys: list[int] = []
        for key in value.keys():
            if isinstance(key, int):
                numeric_keys.append(key)
                continue
            if isinstance(key, str) and key.isdigit():
                numeric_keys.append(int(key))
                continue
            return "object-like table"
        numeric_keys = sorted(numeric_keys)
        if numeric_keys == list(range(1, len(numeric_keys) + 1)):
            return "array-like table"
        return "object-like table"
    return "scalar"


def _shape_matches(expected_shape: str, actual_shape: str) -> bool:
    if expected_shape == "array-like table":
        return actual_shape in {"array-like table", "empty table"}
    if expected_shape == "object-like table":
        return actual_shape == "object-like table"
    if expected_shape == "scalar":
        return actual_shape == "scalar"
    if expected_shape == "empty table":
        return actual_shape == "empty table"
    if expected_shape == "nil":
        return actual_shape == "nil"
    return expected_shape == actual_shape


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


def _derive_target_field_path(payload: ShapeTypeVerifierInput) -> str | None:
    explicit = _normalize_nullable_string(payload.get("target_field_path"))
    if explicit:
        return explicit
    expected_paths = _unique_strings(_ensure_string_list(payload.get("expected_workflow_paths")))
    if expected_paths:
        return expected_paths[0]
    extracted = _extract_workflow_paths(str(payload.get("task", "") or ""))
    if extracted:
        return extracted[0]
    return None


def _derive_expected_shape(payload: ShapeTypeVerifierInput) -> str | None:
    direct = _normalize_shape(payload.get("expected_shape"))
    if direct:
        return direct

    semantic_expectations = {item.lower() for item in _ensure_string_list(payload.get("semantic_expectations"))}
    selected_primary_type = str(payload.get("selected_primary_type", "") or "").strip().lower()
    task = str(payload.get("task", "") or "").lower()

    if "array_normalization" in semantic_expectations:
        return "array-like table"
    if any(token in task for token in ("массив", "array", "list")):
        return "array-like table"
    if any(token in task for token in ("объект", "object")):
        return "object-like table"
    if any(token in task for token in ("scalar", "строк", "числ", "number", "string", "boolean", "bool")):
        return "scalar"

    type_map = {
        "scalar": "scalar",
        "object": "object-like table",
        "array_object": "array-like table",
        "array_scalar": "array-like table",
    }
    return type_map.get(selected_primary_type)


def _extract_mark_as_array_targets(code: str) -> list[str]:
    return _unique_strings([str(match.group(1) or "").strip() for match in _MARK_AS_ARRAY_RE.finditer(code or "")])


def _uses_table_only_shape_logic(code: str) -> bool:
    if not _TYPE_TABLE_RE.search(code or ""):
        return False
    return not any(pattern.search(code or "") for pattern in _NUMERIC_KEY_GUARD_PATTERNS)


def _build_evidence_result(
    *,
    passed: bool,
    field_path: str | None,
    expected_shape: str,
    actual_shape: str,
    summary: str,
    error_code: str | None = None,
    error_family: str | None = None,
    evidence: list[str] | None = None,
    confidence: float = 1.0,
) -> ShapeTypeVerifierResult:
    if passed:
        return {
            "verifier_name": _AGENT_NAME,
            "passed": True,
            "error_family": None,
            "error_code": None,
            "severity": "low",
            "summary": summary,
            "field_path": None,
            "evidence": _ensure_string_list(evidence or []),
            "expected": {"shape": expected_shape, "field_path": field_path},
            "actual": {"shape": actual_shape, "field_path": field_path},
            "fixer_brief": _normalize_fixer_brief({}, passed=True),
            "confidence": _clamp_confidence(confidence),
        }

    return _build_failed_result(
        error_family=error_family or "shape_type",
        error_code=error_code or "shape_mismatch",
        severity="high",
        summary=summary,
        field_path=field_path,
        evidence=evidence or [],
        expected={"shape": expected_shape, "field_path": field_path},
        actual={"shape": actual_shape, "field_path": field_path},
        fixer_brief={
            "goal": "Fix the shape/type mismatch only.",
            "must_change": [
                (
                    f"Normalize `{field_path}` to `{expected_shape}`."
                    if field_path
                    else f"Normalize the target value to `{expected_shape}`."
                )
            ],
            "must_preserve": ["Keep the existing data values and intended nesting."],
            "forbidden_fixes": [
                "Do not change unrelated contract or business logic.",
                "Do not rely only on `type(x) == \"table\"` for shape-sensitive normalization.",
            ],
            "suggested_patch": (
                f"At `{field_path}`, rebuild the value into `{expected_shape}` and preserve nested item data."
                if field_path
                else f"Rebuild the target value into `{expected_shape}` and preserve the original data payload."
            ),
            "patch_scope": "local" if field_path else "function_level",
        },
        confidence=confidence,
    )


def _decide_error_code(
    *,
    expected_shape: str,
    actual_shape: str,
    field_path: str | None,
    before_shape: str | None,
    selected_primary_type: str,
) -> tuple[str, str]:
    nested = bool(field_path and field_path.count(".") >= 3)
    if expected_shape == "array-like table" and actual_shape == "object-like table":
        if nested:
            return "shape_type", "nested_field_wrong_shape"
        if before_shape == "object-like table":
            return "shape_type", "object_left_object"
        return "shape_type", "object_left_object"
    if expected_shape == "array-like table" and actual_shape == "scalar":
        return "shape_type", "scalar_not_normalized"
    if expected_shape == "scalar" and actual_shape in {"object-like table", "array-like table", "empty table"}:
        return "shape_type", "scalar_not_normalized"
    if selected_primary_type in {"array_object", "array_scalar"} and actual_shape in {"object-like table", "scalar"}:
        return "shape_type", "array_damaged"
    return "shape_type", "shape_mismatch"


def _evaluate_shape_evidence(payload: ShapeTypeVerifierInput) -> ShapeTypeVerifierResult | None:
    expected_shape = _derive_expected_shape(payload)
    if not expected_shape:
        return None

    field_path = _derive_target_field_path(payload)
    selected_primary_type = str(payload.get("selected_primary_type", "") or "").strip().lower()
    before_shape: str | None = None

    evidence_value: object | None = None
    evidence_source = ""
    if payload.get("after_state") is not None and field_path:
        found, value = _resolve_workflow_path_value(payload.get("after_state"), field_path)
        if found:
            evidence_value = value
            evidence_source = "after_state"
            if payload.get("before_state") is not None:
                before_found, before_value = _resolve_workflow_path_value(payload.get("before_state"), field_path)
                if before_found:
                    before_shape = _classify_shape(before_value)

    if evidence_source == "" and payload.get("runtime_result") is not None:
        evidence_value = payload.get("runtime_result")
        evidence_source = "runtime_result"

    if evidence_source == "":
        return None

    actual_shape = _classify_shape(evidence_value)
    if _shape_matches(expected_shape, actual_shape):
        return _build_evidence_result(
            passed=True,
            field_path=field_path,
            expected_shape=expected_shape,
            actual_shape=actual_shape,
            summary=f"Observed {evidence_source} confirms the expected `{expected_shape}` shape.",
            evidence=[
                f"Primary evidence source: {evidence_source}.",
                (
                    f"Observed field `{field_path}` shape is `{actual_shape}`."
                    if field_path and evidence_source == "after_state"
                    else f"Observed result shape is `{actual_shape}`."
                ),
            ],
            confidence=1.0,
        )

    error_family, error_code = _decide_error_code(
        expected_shape=expected_shape,
        actual_shape=actual_shape,
        field_path=field_path,
        before_shape=before_shape,
        selected_primary_type=selected_primary_type,
    )
    target_text = f"Field `{field_path}`" if field_path and evidence_source == "after_state" else "Observed result"
    summary = (
        f"{target_text} has shape `{actual_shape}` in {evidence_source}, but `{expected_shape}` is required."
    )
    evidence = [f"Primary evidence source: {evidence_source}."]
    if field_path and evidence_source == "after_state":
        evidence.append(f"Resolved `{field_path}` in after_state as `{actual_shape}`.")
    else:
        evidence.append(f"runtime_result has shape `{actual_shape}`.")
    if before_shape:
        evidence.append(f"Before execution, the same field had shape `{before_shape}`.")
    return _build_evidence_result(
        passed=False,
        field_path=field_path,
        expected_shape=expected_shape,
        actual_shape=actual_shape,
        summary=summary,
        error_code=error_code,
        error_family=error_family,
        evidence=evidence,
        confidence=1.0,
    )


def _detect_local_shape_failure(payload: ShapeTypeVerifierInput) -> ShapeTypeVerifierResult | None:
    code = str(payload.get("code", "") or "")
    expected_shape = _derive_expected_shape(payload)
    field_path = _derive_target_field_path(payload)
    selected_primary_type = str(payload.get("selected_primary_type", "") or "").strip().lower()
    semantic_expectations = {item.lower() for item in _ensure_string_list(payload.get("semantic_expectations"))}

    if expected_shape == "array-like table" and (
        "array_normalization" in semantic_expectations or selected_primary_type in {"object", "scalar"}
    ):
        if _uses_table_only_shape_logic(code):
            return _build_failed_result(
                error_family="shape_logic",
                error_code="table_only_shape_check",
                severity="high",
                summary=(
                    "The shape-sensitive logic relies only on `type(x) == \"table\"` and does not distinguish object-like vs array-like tables."
                ),
                field_path=field_path,
                evidence=[
                    "Detected a table-only type guard.",
                    "No numeric-key or array-shape discriminator was found near the normalization logic.",
                ],
                expected={"shape": expected_shape, "field_path": field_path},
                actual={"shape_logic": "table-only guard"},
                fixer_brief={
                    "goal": "Add real shape discrimination for object-like vs array-like tables.",
                    "must_change": ["Replace the table-only shortcut with explicit array-vs-object shape checks."],
                    "must_preserve": ["Keep the current target field and data payload."],
                    "forbidden_fixes": [
                        "Do not rely only on `type(x) == \"table\"`.",
                        "Do not treat `next(x)` as a full object-vs-array proof.",
                    ],
                    "suggested_patch": "Inspect keys or equivalent array semantics before deciding whether to wrap or preserve the table.",
                    "patch_scope": "function_level",
                },
                confidence=0.98,
            )

        mark_targets = _extract_mark_as_array_targets(code)
        if field_path:
            for mark_target in mark_targets:
                normalized_target = mark_target.strip()
                if normalized_target != field_path and (
                    normalized_target.startswith(field_path + ".")
                    or field_path.startswith(normalized_target + ".")
                ):
                    return _build_failed_result(
                        error_family="normalization_level",
                        error_code="mark_as_array_wrong_level",
                        severity="high",
                        summary=(
                            f"`_utils.array.markAsArray` is applied to `{normalized_target}` instead of the required field `{field_path}`."
                        ),
                        field_path=field_path,
                        evidence=[f"Found `_utils.array.markAsArray({normalized_target})`."],
                        expected={"field_path": field_path, "shape": expected_shape},
                        actual={"mark_as_array_target": normalized_target},
                        fixer_brief={
                            "goal": "Apply array normalization at the correct nested level.",
                            "must_change": [f"Normalize `{field_path}` instead of `{normalized_target}`."],
                            "must_preserve": ["Keep the surrounding workflow structure unchanged."],
                            "forbidden_fixes": ["Do not mark a parent or child level as array when the target field is different."],
                            "suggested_patch": f"Move the shape fix to `{field_path}` and keep other levels unchanged.",
                            "patch_scope": "local",
                        },
                        confidence=0.99,
                    )

                if (
                    normalized_target == field_path
                    and selected_primary_type in {"object", "scalar"}
                    and not _ARRAY_NEW_RE.search(code)
                ):
                    return _build_failed_result(
                        error_family="shape_logic",
                        error_code="mark_as_array_wrong_level",
                        severity="high",
                        summary=(
                            f"`_utils.array.markAsArray({field_path})` is applied without rebuilding the value into a real array at the required level."
                        ),
                        field_path=field_path,
                        evidence=[f"Found `_utils.array.markAsArray({field_path})` but no `_utils.array.new()` in the normalization path."],
                        expected={"field_path": field_path, "shape": expected_shape},
                        actual={"shape_logic": "relabeling source value in place"},
                        fixer_brief={
                            "goal": "Create the correct array shape instead of relabeling the source object/scalar in place.",
                            "must_change": [f"Rebuild `{field_path}` into a new array-shaped value before marking it as array."],
                            "must_preserve": ["Keep the original nested data values."],
                            "forbidden_fixes": ["Do not simply relabel the original object/scalar as an array in place."],
                            "suggested_patch": "Create a new array, place the original value at the correct level, then mark that new array as array-shaped.",
                            "patch_scope": "function_level",
                        },
                        confidence=0.97,
                    )

    return None


def _normalize_shape_type_verifier_result(raw: object) -> ShapeTypeVerifierResult:
    data = raw if isinstance(raw, dict) else {}
    passed = bool(data.get("passed", False))
    severity = str(data.get("severity", "") or "").strip().lower()
    if severity not in _VALID_SEVERITIES:
        severity = "low" if passed else "high"

    summary = str(data.get("summary", "") or "").strip()
    if not summary:
        summary = "Shape/type check passed." if passed else "Shape/type verification failed."

    error_family = _normalize_nullable_string(data.get("error_family"))
    error_code = _normalize_nullable_string(data.get("error_code"))
    field_path = _normalize_nullable_string(data.get("field_path"))
    if passed:
        error_family = None
        error_code = None
        field_path = None

    expected = _ensure_object(data.get("expected"))
    actual = _ensure_object(data.get("actual"))
    if "shape" in expected:
        expected["shape"] = _normalize_shape(expected.get("shape")) or str(expected.get("shape"))
    if "shape" in actual:
        actual["shape"] = _normalize_shape(actual.get("shape")) or str(actual.get("shape"))

    return {
        "verifier_name": _AGENT_NAME,
        "passed": passed,
        "error_family": error_family,
        "error_code": error_code,
        "severity": severity,
        "summary": summary,
        "field_path": field_path,
        "evidence": _ensure_string_list(data.get("evidence")),
        "expected": expected,
        "actual": actual,
        "fixer_brief": _normalize_fixer_brief(data.get("fixer_brief"), passed=passed),
        "confidence": _clamp_confidence(data.get("confidence")),
    }


def _build_shape_type_verifier_prompt(payload: ShapeTypeVerifierInput) -> str:
    task = str(payload.get("task", "") or "").strip()
    code = str(payload.get("code", "") or "").strip()
    target_field_path = _derive_target_field_path(payload)
    expected_shape = _derive_expected_shape(payload)
    selected_primary_type = str(payload.get("selected_primary_type", "") or "").strip()
    semantic_expectations = _ensure_string_list(payload.get("semantic_expectations"))

    expected_lines: list[str] = []
    if target_field_path:
        expected_lines.append("- target field path: " + target_field_path)
    if expected_shape:
        expected_lines.append("- expected shape: " + expected_shape)
    if selected_primary_type:
        expected_lines.append("- selected primary type from compiler: " + selected_primary_type)
    if semantic_expectations:
        expected_lines.append("- semantic expectations: " + ", ".join(semantic_expectations))

    before_value_section = ""
    after_value_section = ""
    if payload.get("before_state") is not None and target_field_path:
        found, value = _resolve_workflow_path_value(payload.get("before_state"), target_field_path)
        if found:
            before_value_section = f"before_state value at {target_field_path}:\n{_compact_json(value)}"
    if payload.get("after_state") is not None and target_field_path:
        found, value = _resolve_workflow_path_value(payload.get("after_state"), target_field_path)
        if found:
            after_value_section = f"after_state value at {target_field_path}:\n{_compact_json(value)}"

    sections = [
        f"Task:\n{task}" if task else "",
        "Scope:\nCheck only shape/type mismatches. Ignore contract-only issues and full business logic.",
        "Expected shape contract:\n" + "\n".join(expected_lines) if expected_lines else "",
        (
            "Parsed workflow context:\n" + _compact_json(payload.get("parsed_context"))
            if payload.get("parsed_context") is not None
            else ""
        ),
        (
            "runtime_result:\n" + _compact_json(payload.get("runtime_result"))
            if payload.get("runtime_result") is not None
            else ""
        ),
        (
            "before_state:\n" + _compact_json(payload.get("before_state"))
            if payload.get("before_state") is not None
            else ""
        ),
        (
            "after_state:\n" + _compact_json(payload.get("after_state"))
            if payload.get("after_state") is not None
            else ""
        ),
        before_value_section,
        after_value_section,
        f"Lua solution under review:\n```lua\n{code}\n```",
        "Return strict JSON in this exact shape:\n" + json.dumps(_OUTPUT_SHAPE_EXAMPLE, ensure_ascii=False, indent=2),
    ]
    return _build_prompt_sections(*sections)


def build_shape_type_verifier_input_from_state(state: dict[str, Any]) -> ShapeTypeVerifierInput:
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
    selected_primary_path = str(compiled_request.get("selected_primary_path", "") or "").strip()
    expected_workflow_paths = _unique_strings(
        _ensure_string_list(compiled_request.get("expected_workflow_paths"))
        + ([selected_primary_path] if selected_primary_path else [])
        + _extract_workflow_paths(task)
    )

    runtime_result: object = diagnostics.get("result_value")
    if runtime_result is None:
        preview = str(diagnostics.get("result_preview", "") or "").strip()
        if preview:
            runtime_result = preview

    parsed_context = compiled_request.get("parsed_context")
    before_state = parsed_context if compiled_request.get("has_parseable_context") else None

    return {
        "task": task,
        "code": str(state.get("generated_code", "") or state.get("current_code", "") or ""),
        "target_field_path": selected_primary_path or None,
        "expected_workflow_paths": expected_workflow_paths,
        "selected_primary_type": _normalize_nullable_string(compiled_request.get("selected_primary_type")),
        "semantic_expectations": _ensure_string_list(compiled_request.get("semantic_expectations")),
        "expected_shape": _normalize_nullable_string(
            state.get("shape_type_expected_shape") or compiled_request.get("shape_type_expected_shape")
        ),
        "parsed_context": parsed_context,
        "runtime_result": runtime_result,
        "before_state": before_state,
        "after_state": diagnostics.get("workflow_state"),
    }


def to_aggregate_verification_result(result: ShapeTypeVerifierResult) -> dict[str, Any]:
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


class ShapeTypeVerifierAgent:
    """LLM-backed shape/type verifier with evidence-first and local-guard behavior."""

    def __init__(self, llm: LLMProvider) -> None:
        self._llm = llm

    @property
    def name(self) -> str:
        return _AGENT_NAME

    async def verify(self, payload: ShapeTypeVerifierInput) -> ShapeTypeVerifierResult:
        code = str(payload.get("code", "") or "")
        target_field_path = _derive_target_field_path(payload)
        expected_shape = _derive_expected_shape(payload)

        logger.info(
            f"[{_AGENT_NAME}] started",
            code_len=len(code),
            target_field_path=target_field_path or "none",
            expected_shape=expected_shape or "unknown",
            has_runtime_result=payload.get("runtime_result") is not None,
            has_after_state=payload.get("after_state") is not None,
        )

        evidence_result = _evaluate_shape_evidence(payload)
        if evidence_result is not None:
            logger.info(
                f"[{_AGENT_NAME}] evidence_result",
                passed=evidence_result["passed"],
                error_code=evidence_result["error_code"] or "none",
                field_path=evidence_result["field_path"] or "none",
            )
            return evidence_result

        local_failure = _detect_local_shape_failure(payload)
        if local_failure is not None:
            logger.info(
                f"[{_AGENT_NAME}] local_failure",
                error_code=local_failure["error_code"],
                field_path=local_failure["field_path"] or "none",
            )
            return local_failure

        prompt = _build_shape_type_verifier_prompt(payload)
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
                summary=f"ShapeTypeVerifier could not produce a valid verdict: {exc}",
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
        result = _normalize_shape_type_verifier_result(raw)
        logger.info(
            f"[{_AGENT_NAME}] completed",
            passed=result["passed"],
            error_code=result["error_code"] or "none",
            field_path=result["field_path"] or "none",
            confidence=result["confidence"],
        )
        return result


def create_shape_type_verifier_node(llm: LLMProvider) -> Callable:
    agent = ShapeTypeVerifierAgent(llm)

    async def verify_shape_type(state: dict[str, Any]) -> ShapeTypeVerifierNodeOutput:
        payload = build_shape_type_verifier_input_from_state(state)
        result = await agent.verify(payload)
        aggregate = to_aggregate_verification_result(result)
        return {
            "shape_type_verifier_result": result,
            "verification": aggregate,
            "verification_passed": bool(result["passed"]),
            "failure_stage": "" if result["passed"] else "shape_type_verification",
        }

    return verify_shape_type
