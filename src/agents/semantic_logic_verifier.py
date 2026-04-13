"""Standalone SemanticLogicVerifier agent for semantic-only workflow checks."""

from __future__ import annotations

import json
import re
from collections import Counter
from typing import Any, Callable, TypedDict

import structlog

from src.core.llm import LLMProvider

logger = structlog.get_logger(__name__)

_AGENT_NAME = "SemanticLogicVerifier"

_SYSTEM_PROMPT = """You are SemanticLogicVerifier.
Check only semantic logic errors in a Lua workflow solution.
Do not judge contract errors.
Check:
- wrong filtering condition
- wrong and/or/not logic
- missing required items
- extra items included
- wrong aggregation
- wrong computed values
Use runtime_result and state differences as primary evidence when present.
Return JSON only.
If passed=false, give one concrete counterexample, expected behavior, actual behavior, and a minimal patch."""

_WORKFLOW_PATH_RE = re.compile(r"wf\.(?:vars|initVariables)(?:\.[A-Za-z_][A-Za-z0-9_]*)+")
_RETURN_SOURCE_RE = re.compile(r"return\s+(wf\.(?:vars|initVariables)(?:\.[A-Za-z_][A-Za-z0-9_]*)+)")
_FIRST_INDEX_RE = re.compile(r"\[\s*1\s*\]")
_LAST_INDEX_RE = re.compile(r"\[\s*#\s*[A-Za-z_][A-Za-z0-9_]*\s*\]")
_IF_LINE_RE = re.compile(r"^\s*if\s+(.+?)\s+then\s*$", re.IGNORECASE | re.MULTILINE)

_VALID_SEVERITIES = frozenset({"low", "medium", "high", "critical"})
_VALID_PATCH_SCOPES = frozenset({"none", "local", "function_level", "multi_block", "rewrite"})
_IDENTITY_KEYS = ("id", "ID", "sku", "SKU", "email", "name", "key", "uuid", "code")
_BOOLEAN_TRUE_MARKERS = ("true", "yes", "enabled", "active", "verified")
_BOOLEAN_FALSE_MARKERS = ("false", "no", "disabled", "inactive", "archived")
_FILTER_MARKERS = (
    "filter",
    "only",
    "where",
    "with ",
    "without ",
    "non-empty",
    "not empty",
    "отфильтр",
    "только",
    "остав",
    "с непуст",
    "не пуст",
    "есть ",
    "без ",
)
_AND_MARKERS = (" and ", " both ", " all ", " и ", " оба ", " все ")
_OR_MARKERS = (" or ", " either ", " any ", " или ", " либо ")
_NOT_MARKERS = (" not ", " without ", " except ", " excluding ", " не ", " без ")


class SemanticLogicVerifierInput(TypedDict, total=False):
    task: str
    code: str
    source_field_path: str | None
    output_field_path: str | None
    expected_workflow_paths: list[str]
    selected_operation: str | None
    operation_argument: object
    semantic_expectations: list[str]
    requested_item_keys: list[str]
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


class SemanticLogicVerifierResult(TypedDict):
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


class SemanticLogicVerifierNodeOutput(TypedDict, total=False):
    semantic_logic_verifier_result: SemanticLogicVerifierResult
    verification: dict[str, Any]
    verification_passed: bool
    failure_stage: str


class _ObservedValue(TypedDict, total=False):
    source: str
    field_path: str | None
    value: object


class _FilterClause(TypedDict, total=False):
    field: str
    kind: str
    value: object


class _FilterSpec(TypedDict, total=False):
    connector: str
    clauses: list[_FilterClause]


_OUTPUT_SHAPE_EXAMPLE: dict[str, Any] = {
    "verifier_name": _AGENT_NAME,
    "passed": False,
    "error_family": "semantic_logic",
    "error_code": "extra_items_included",
    "severity": "high",
    "summary": "Runtime result includes `id=2` even though `email` is empty.",
    "field_path": "wf.vars.users",
    "evidence": [
        "Primary evidence source: runtime_result.",
        "Counterexample item `id=2` has empty `email` but was still included.",
    ],
    "expected": {
        "expected_behavior": "Keep only items where `email` is non-empty.",
        "counterexample": {"id": 2, "email": ""},
        "field_path": "wf.vars.users",
        "operation": "filter",
    },
    "actual": {
        "actual_behavior": "Included `id=2` even though it does not satisfy the requested filter.",
        "counterexample_result": {"id": 2, "email": ""},
        "field_path": "wf.vars.users",
        "operation": "filter",
    },
    "fixer_brief": {
        "goal": "Fix the semantic logic only.",
        "must_change": ["Exclude items where `email` is empty."],
        "must_preserve": ["Keep the current workflow contract and output shape."],
        "forbidden_fixes": ["Do not hardcode the counterexample item.", "Do not change unrelated contract logic."],
        "suggested_patch": "Change the filter predicate so it keeps only items with non-empty `email`.",
        "patch_scope": "function_level",
    },
    "confidence": 1.0,
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
        "goal": goal or "Fix the semantic logic only.",
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


def _compact_identity(value: object) -> str:
    if isinstance(value, dict):
        for key in _IDENTITY_KEYS:
            if key in value:
                return f"{key}={value[key]!r}"
    return _compact_json(value, limit=100)


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


def _normalize_behavior_text(value: object, fallback: str) -> str:
    text = str(value or "").strip()
    if not text:
        return fallback
    normalized = re.sub(r"\s+", " ", text)
    return normalized[:240]


def _normalize_scalar_token(token: str) -> object:
    cleaned = str(token or "").strip().strip("\"'`")
    lowered = cleaned.lower()
    if lowered == "true":
        return True
    if lowered == "false":
        return False
    if re.fullmatch(r"-?\d+", cleaned):
        return int(cleaned)
    if re.fullmatch(r"-?\d+\.\d+", cleaned):
        try:
            return float(cleaned)
        except ValueError:
            return cleaned
    return cleaned


def _normalize_scalar_for_compare(value: object) -> object:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return value
    if value is None:
        return None
    return str(value).strip().lower()


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


def _coerce_non_empty(value: object) -> bool:
    if value is None:
        return False
    if isinstance(value, str):
        return bool(value.strip())
    return bool(value)


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


def _derive_source_field_path(payload: SemanticLogicVerifierInput) -> str | None:
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


def _derive_output_field_path(payload: SemanticLogicVerifierInput) -> str | None:
    explicit = _normalize_nullable_string(payload.get("output_field_path"))
    if explicit:
        return explicit
    return None


def _derive_selected_operation(payload: SemanticLogicVerifierInput) -> str:
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
    if any(marker in task for marker in _FILTER_MARKERS):
        return "filter"
    return selected or "llm"


def _primary_field_path(payload: SemanticLogicVerifierInput) -> str | None:
    return _derive_output_field_path(payload) or _derive_source_field_path(payload)


def _resolve_source_value(payload: SemanticLogicVerifierInput) -> tuple[str | None, object | None]:
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


def _resolve_observed_value(payload: SemanticLogicVerifierInput) -> _ObservedValue | None:
    after_state = payload.get("after_state")
    output_path = _derive_output_field_path(payload)
    source_path = _derive_source_field_path(payload)
    if after_state is not None and output_path:
        found, value = _resolve_workflow_path_value(after_state, output_path)
        if found:
            return {"source": "after_state", "field_path": output_path, "value": value}
    if payload.get("runtime_result") is not None:
        return {"source": "runtime_result", "field_path": None, "value": payload.get("runtime_result")}
    if after_state is not None and source_path:
        found, value = _resolve_workflow_path_value(after_state, source_path)
        if found:
            return {"source": "after_state", "field_path": source_path, "value": value}
    return None


def _collect_candidate_fields(
    payload: SemanticLogicVerifierInput,
    source_value: object,
) -> list[str]:
    requested = _unique_strings(_ensure_string_list(payload.get("requested_item_keys")))
    if requested:
        return requested
    if isinstance(source_value, list):
        ordered: list[str] = []
        seen: set[str] = set()
        for item in source_value:
            if not isinstance(item, dict):
                continue
            for key in item.keys():
                key_text = str(key).strip()
                if key_text and key_text not in seen:
                    seen.add(key_text)
                    ordered.append(key_text)
        return ordered
    if isinstance(source_value, dict):
        return [str(key).strip() for key in source_value.keys() if str(key).strip()]
    return []


def _infer_boolean_connector(task: str) -> str:
    lowered = f" {task.lower()} "
    has_and = any(marker in lowered for marker in _AND_MARKERS)
    has_or = any(marker in lowered for marker in _OR_MARKERS)
    if has_and and not has_or:
        return "and"
    if has_or and not has_and:
        return "or"
    return ""


def _parse_filter_clause(task_lower: str, field: str) -> _FilterClause | None:
    token = re.escape(field.lower())
    if re.search(rf"\b{token}\b\s+(?:is\s+)?not\s+empty\b", task_lower):
        return {"field": field, "kind": "non_empty"}
    if re.search(rf"\bnon-empty\s+{token}\b", task_lower):
        return {"field": field, "kind": "non_empty"}
    if re.search(rf"\bwith\s+{token}\b", task_lower) or re.search(rf"\bhas\s+{token}\b", task_lower):
        return {"field": field, "kind": "non_empty"}
    if re.search(rf"\b{token}\b\s+(?:не\s+пуст|содержит\s+значение)", task_lower):
        return {"field": field, "kind": "non_empty"}

    negated_match = re.search(rf"\b{token}\b\s*(?:!=|~=|is not|not equal to)\s*([A-Za-z0-9_.@+-]+)", task_lower)
    if negated_match:
        return {"field": field, "kind": "not_equals", "value": _normalize_scalar_token(negated_match.group(1))}

    equals_match = re.search(rf"\b{token}\b\s*(?:==|=|is|equals)\s*([A-Za-z0-9_.@+-]+)", task_lower)
    if equals_match:
        return {"field": field, "kind": "equals", "value": _normalize_scalar_token(equals_match.group(1))}

    if field.lower() in ("active", "enabled", "verified", "archived", "deleted"):
        if re.search(rf"\b(?:not|without|не|без)\s+{token}\b", task_lower):
            return {"field": field, "kind": "falsy"}
        return {"field": field, "kind": "truthy"}

    return None


def _parse_filter_spec(task: str, candidate_fields: list[str]) -> _FilterSpec | None:
    task_lower = task.lower()
    clauses: list[_FilterClause] = []
    for field in candidate_fields:
        if not field or field.lower() not in task_lower:
            continue
        clause = _parse_filter_clause(task_lower, field)
        if clause is not None:
            clauses.append(clause)
    if not clauses and any(marker in task_lower for marker in _FILTER_MARKERS) and len(candidate_fields) == 1:
        field = candidate_fields[0]
        clauses.append({"field": field, "kind": "non_empty"})
    if not clauses:
        return None
    return {
        "connector": _infer_boolean_connector(task),
        "clauses": clauses,
    }


def _describe_filter_clause(clause: _FilterClause) -> str:
    field = clause.get("field", "value")
    kind = clause.get("kind", "equals")
    if kind == "non_empty":
        return f"`{field}` is non-empty"
    if kind == "truthy":
        return f"`{field}` is truthy"
    if kind == "falsy":
        return f"`{field}` is falsy"
    if kind == "not_equals":
        return f"`{field}` is not {clause.get('value')!r}"
    return f"`{field}` equals {clause.get('value')!r}"


def _describe_filter_spec(spec: _FilterSpec) -> str:
    connector = spec.get("connector", "and")
    rendered = [_describe_filter_clause(clause) for clause in spec.get("clauses", [])]
    if not rendered:
        return "the requested filter rule"
    return (" " + connector + " ").join(rendered)


def _matches_filter_clause(item: object, clause: _FilterClause) -> bool:
    field = str(clause.get("field", "") or "").strip()
    value = item.get(field) if isinstance(item, dict) and field else item
    kind = str(clause.get("kind", "") or "")
    if kind == "non_empty":
        return _coerce_non_empty(value)
    if kind == "truthy":
        return bool(value)
    if kind == "falsy":
        return not bool(value)
    if kind == "not_equals":
        return _normalize_scalar_for_compare(value) != _normalize_scalar_for_compare(clause.get("value"))
    return _normalize_scalar_for_compare(value) == _normalize_scalar_for_compare(clause.get("value"))


def _matches_filter_spec(item: object, spec: _FilterSpec) -> bool:
    clauses = spec.get("clauses", [])
    if not clauses:
        return True
    results = [_matches_filter_clause(item, clause) for clause in clauses]
    if spec.get("connector", "and") == "or":
        return any(results)
    return all(results)


def _explain_filter_failure(item: object, spec: _FilterSpec) -> str:
    failed = [clause for clause in spec.get("clauses", []) if not _matches_filter_clause(item, clause)]
    if not failed:
        return "it does not satisfy the requested filter"
    if len(failed) == 1:
        return _describe_filter_clause(failed[0]).replace(" is ", " was ").replace(" equals ", " was not equal to ")
    return "it fails " + ", ".join(_describe_filter_clause(clause) for clause in failed)


def _list_diff(expected_items: list[Any], actual_items: list[Any]) -> tuple[list[Any], list[Any]]:
    expected_map: dict[str, list[Any]] = {}
    actual_map: dict[str, list[Any]] = {}
    expected_counter: Counter[str] = Counter()
    actual_counter: Counter[str] = Counter()
    for item in expected_items:
        signature = _jsonish_signature(item)
        expected_counter[signature] += 1
        expected_map.setdefault(signature, []).append(item)
    for item in actual_items:
        signature = _jsonish_signature(item)
        actual_counter[signature] += 1
        actual_map.setdefault(signature, []).append(item)

    missing: list[Any] = []
    extra: list[Any] = []
    for signature, count in (expected_counter - actual_counter).items():
        missing.extend(expected_map.get(signature, [])[:count])
    for signature, count in (actual_counter - expected_counter).items():
        extra.extend(actual_map.get(signature, [])[:count])
    return missing, extra


def _build_passed_result(
    *,
    summary: str,
    evidence: list[str] | None = None,
    confidence: float = 1.0,
) -> SemanticLogicVerifierResult:
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
    expected_behavior: str,
    actual_behavior: str,
    counterexample: object,
    evidence: list[str] | None = None,
    expected_extra: dict[str, Any] | None = None,
    actual_extra: dict[str, Any] | None = None,
    must_change: list[str] | None = None,
    suggested_patch: str = "",
    patch_scope: str = "function_level",
    confidence: float = 1.0,
    severity: str = "high",
) -> SemanticLogicVerifierResult:
    expected = {
        "expected_behavior": _normalize_behavior_text(expected_behavior, "Follow the requested semantic behavior."),
        "counterexample": _compact_counterexample(counterexample),
    }
    actual = {
        "actual_behavior": _normalize_behavior_text(actual_behavior, summary),
        "counterexample_result": _compact_counterexample(counterexample),
    }
    if field_path:
        expected["field_path"] = field_path
        actual["field_path"] = field_path
    if expected_extra:
        expected.update(expected_extra)
    if actual_extra:
        actual.update(actual_extra)
    return {
        "verifier_name": _AGENT_NAME,
        "passed": False,
        "error_family": error_family,
        "error_code": error_code,
        "severity": severity if severity in _VALID_SEVERITIES else "high",
        "summary": summary.strip() or "Semantic verification failed.",
        "field_path": field_path,
        "evidence": _ensure_string_list(evidence or []),
        "expected": expected,
        "actual": actual,
        "fixer_brief": _normalize_fixer_brief(
            {
                "goal": "Fix the semantic logic only.",
                "must_change": must_change or [expected_behavior],
                "must_preserve": ["Keep the workflow contract and data shape unchanged."],
                "forbidden_fixes": [
                    "Do not hardcode the counterexample input or output.",
                    "Do not change unrelated contract or shape logic.",
                ],
                "suggested_patch": suggested_patch or actual_behavior,
                "patch_scope": patch_scope,
            },
            passed=False,
        ),
        "confidence": _clamp_confidence(confidence),
    }


def _evaluate_filter_evidence(payload: SemanticLogicVerifierInput) -> SemanticLogicVerifierResult | None:
    task = str(payload.get("task", "") or "")
    source_path, source_value = _resolve_source_value(payload)
    observed = _resolve_observed_value(payload)
    if observed is None:
        return None
    source_list = _coerce_sequence(source_value)
    observed_list = _coerce_sequence(observed.get("value"))
    if source_list is None or observed_list is None:
        return None

    candidate_fields = _collect_candidate_fields(payload, source_list)
    spec = _parse_filter_spec(task, candidate_fields)
    if spec is None:
        return None

    expected_items = [item for item in source_list if _matches_filter_spec(item, spec)]
    missing_items, extra_items = _list_diff(expected_items, observed_list)
    rule_text = _describe_filter_spec(spec)
    field_path = observed.get("field_path") or source_path

    if not missing_items and not extra_items:
        return _build_passed_result(
            summary=f"Observed {observed['source']} matches the requested filter logic.",
            evidence=[
                f"Primary evidence source: {observed['source']}.",
                f"Expected filter rule: {rule_text}.",
            ],
            confidence=1.0,
        )

    if extra_items and not missing_items:
        item = extra_items[0]
        identifier = _compact_identity(item)
        reason = _explain_filter_failure(item, spec)
        return _build_failed_result(
            error_family="semantic_logic",
            error_code="extra_items_included",
            summary=f"Observed result includes `{identifier}` even though {reason}.",
            field_path=field_path,
            expected_behavior=f"Keep only items where {rule_text}.",
            actual_behavior=f"Included `{identifier}` even though it does not satisfy the requested filter.",
            counterexample=item,
            evidence=[
                f"Primary evidence source: {observed['source']}.",
                f"Counterexample item `{identifier}` does not satisfy {rule_text}.",
            ],
            expected_extra={"operation": "filter"},
            actual_extra={"operation": "filter"},
            must_change=[f"Exclude items that do not satisfy {rule_text}."],
            suggested_patch="Fix the filter predicate so it rejects items that fail the requested rule.",
            confidence=1.0,
        )

    if missing_items and not extra_items:
        item = missing_items[0]
        identifier = _compact_identity(item)
        return _build_failed_result(
            error_family="semantic_logic",
            error_code="missing_required_items",
            summary=f"Observed result omits `{identifier}` even though it satisfies the requested filter.",
            field_path=field_path,
            expected_behavior=f"Keep items where {rule_text}.",
            actual_behavior=f"Dropped `{identifier}` even though it satisfies the requested filter.",
            counterexample=item,
            evidence=[
                f"Primary evidence source: {observed['source']}.",
                f"Expected filter rule: {rule_text}.",
            ],
            expected_extra={"operation": "filter"},
            actual_extra={"operation": "filter"},
            must_change=[f"Include items that satisfy {rule_text}."],
            suggested_patch="Fix the predicate so matching items are preserved.",
            confidence=1.0,
        )

    item = extra_items[0] if extra_items else missing_items[0]
    identifier = _compact_identity(item)
    return _build_failed_result(
        error_family="semantic_logic",
        error_code="wrong_filter_condition",
        summary=f"Observed result around `{identifier}` does not match the requested filter logic.",
        field_path=field_path,
        expected_behavior=f"Keep only items where {rule_text}.",
        actual_behavior="Returned a set of items that does not match the requested filter.",
        counterexample=item,
        evidence=[
            f"Primary evidence source: {observed['source']}.",
            f"Expected filter rule: {rule_text}.",
            f"Missing items: {len(missing_items)}. Extra items: {len(extra_items)}.",
        ],
        expected_extra={"operation": "filter"},
        actual_extra={"operation": "filter"},
        must_change=[f"Make the filter result match {rule_text} exactly."],
        suggested_patch="Rebuild the boolean filter condition so only matching items remain.",
        confidence=1.0,
    )


def _evaluate_count_evidence(payload: SemanticLogicVerifierInput) -> SemanticLogicVerifierResult | None:
    source_path, source_value = _resolve_source_value(payload)
    observed = _resolve_observed_value(payload)
    if observed is None:
        return None
    source_list = _coerce_sequence(source_value)
    actual_value = _coerce_numeric_result(observed.get("value"))
    if source_list is None or actual_value is None:
        return None
    expected_value = float(len(source_list))
    if actual_value == expected_value:
        return _build_passed_result(
            summary=f"Observed {observed['source']} matches the expected count `{int(expected_value)}`.",
            evidence=[f"Primary evidence source: {observed['source']}.", f"Resolved source list at `{source_path}`."],
            confidence=1.0,
        )
    counterexample = {"source_count": int(expected_value), "observed_value": actual_value}
    return _build_failed_result(
        error_family="aggregation",
        error_code="wrong_aggregation",
        summary=f"Observed value is `{actual_value:g}`, but the source list contains `{int(expected_value)}` items.",
        field_path=observed.get("field_path") or source_path,
        expected_behavior=f"Return count `{int(expected_value)}` for the current source list.",
        actual_behavior=f"Returned `{actual_value:g}` instead.",
        counterexample=counterexample,
        evidence=[
            f"Primary evidence source: {observed['source']}.",
            f"Counted `{int(expected_value)}` source items at `{source_path}`.",
        ],
        expected_extra={"operation": "count", "value": int(expected_value)},
        actual_extra={"operation": "count", "value": actual_value},
        must_change=[f"Return the real item count `{int(expected_value)}` for the current input."],
        suggested_patch="Recompute the count from the actual source collection instead of returning a stale or transformed value.",
        confidence=1.0,
    )


def _infer_numeric_field(task: str, candidate_fields: list[str], source_list: list[Any]) -> str | None:
    task_lower = task.lower()
    for field in candidate_fields:
        if field.lower() in task_lower:
            return field
    for field in candidate_fields:
        for item in source_list:
            if isinstance(item, dict) and _coerce_numeric_value(item.get(field)) is not None:
                return field
    return None


def _evaluate_sum_evidence(payload: SemanticLogicVerifierInput) -> SemanticLogicVerifierResult | None:
    task = str(payload.get("task", "") or "")
    source_path, source_value = _resolve_source_value(payload)
    observed = _resolve_observed_value(payload)
    if observed is None:
        return None
    actual_value = _coerce_numeric_result(observed.get("value"))
    if actual_value is None:
        return None

    source_list = _coerce_sequence(source_value)
    if source_list is None:
        return None

    expected_value: float | None = None
    counterexample: object = {"observed_value": actual_value}
    candidate_fields = _collect_candidate_fields(payload, source_list)
    numeric_field = _infer_numeric_field(task, candidate_fields, source_list)

    if numeric_field is None:
        numeric_values = [_coerce_numeric_value(item) for item in source_list]
        if any(value is None for value in numeric_values):
            return None
        expected_value = sum(value for value in numeric_values if value is not None)
        counterexample = {"source_values": source_list[:3], "observed_value": actual_value}
    else:
        numeric_values = []
        first_item: object = {}
        for item in source_list:
            if not isinstance(item, dict):
                return None
            numeric = _coerce_numeric_value(item.get(numeric_field))
            if numeric is None:
                return None
            numeric_values.append(numeric)
            if first_item == {}:
                first_item = item
        expected_value = sum(numeric_values)
        counterexample = {
            "field": numeric_field,
            "sample_item": _compact_counterexample(first_item),
            "observed_value": actual_value,
        }

    if actual_value == expected_value:
        return _build_passed_result(
            summary=f"Observed {observed['source']} matches the expected aggregation `{expected_value:g}`.",
            evidence=[f"Primary evidence source: {observed['source']}.", f"Resolved source data at `{source_path}`."],
            confidence=1.0,
        )

    field_path = observed.get("field_path") or source_path
    target_text = f"sum `{numeric_field}`" if numeric_field else "sum of the source values"
    return _build_failed_result(
        error_family="aggregation",
        error_code="wrong_aggregation",
        summary=f"Observed value is `{actual_value:g}`, but the expected {target_text} is `{expected_value:g}`.",
        field_path=field_path,
        expected_behavior=f"Return `{expected_value:g}` as the {target_text}.",
        actual_behavior=f"Returned `{actual_value:g}` instead.",
        counterexample=counterexample,
        evidence=[
            f"Primary evidence source: {observed['source']}.",
            f"Resolved source data at `{source_path}`.",
        ],
        expected_extra={"operation": "sum", "value": expected_value, "field": numeric_field},
        actual_extra={"operation": "sum", "value": actual_value, "field": numeric_field},
        must_change=[f"Aggregate the real {target_text} from the current source data."],
        suggested_patch="Recompute the numeric aggregation from the source items and the requested field.",
        confidence=1.0,
    )


def _evaluate_first_last_evidence(payload: SemanticLogicVerifierInput, *, operation: str) -> SemanticLogicVerifierResult | None:
    source_path, source_value = _resolve_source_value(payload)
    observed = _resolve_observed_value(payload)
    if observed is None:
        return None
    source_list = _coerce_sequence(source_value)
    if source_list is None or not source_list:
        return None
    expected_value = source_list[0] if operation == "first" else source_list[-1]
    actual_value = observed.get("value")
    if _jsonish_equal(actual_value, expected_value):
        return _build_passed_result(
            summary=f"Observed {observed['source']} matches the expected `{operation}` item.",
            evidence=[f"Primary evidence source: {observed['source']}.", f"Resolved source list at `{source_path}`."],
            confidence=1.0,
        )
    identifier = _compact_identity(expected_value)
    return _build_failed_result(
        error_family="semantic_logic",
        error_code="wrong_computed_value",
        summary=f"Observed value does not match the expected `{operation}` item `{identifier}`.",
        field_path=observed.get("field_path") or source_path,
        expected_behavior=f"Return the `{operation}` item `{identifier}` from the current source list.",
        actual_behavior=f"Returned `{_compact_identity(actual_value)}` instead.",
        counterexample=expected_value,
        evidence=[
            f"Primary evidence source: {observed['source']}.",
            f"The `{operation}` item at `{source_path}` is `{identifier}`.",
        ],
        expected_extra={"operation": operation},
        actual_extra={"operation": operation},
        must_change=[f"Return the real `{operation}` item from the current input list."],
        suggested_patch=f"Use the correct `{operation}` index instead of a different element.",
        confidence=1.0,
    )


def _evaluate_incremental_evidence(payload: SemanticLogicVerifierInput, *, operation: str) -> SemanticLogicVerifierResult | None:
    source_path, source_value = _resolve_source_value(payload)
    observed = _resolve_observed_value(payload)
    if observed is None:
        return None
    before_value = _coerce_numeric_result(source_value)
    actual_value = _coerce_numeric_result(observed.get("value"))
    if before_value is None or actual_value is None:
        return None
    argument = _coerce_numeric_value(payload.get("operation_argument"))
    delta = argument if argument is not None else 1.0
    expected_value = before_value + delta if operation == "increment" else before_value - delta
    if actual_value == expected_value:
        return _build_passed_result(
            summary=f"Observed {observed['source']} matches the expected `{operation}` result.",
            evidence=[f"Primary evidence source: {observed['source']}.", f"Resolved source value at `{source_path}`."],
            confidence=1.0,
        )
    return _build_failed_result(
        error_family="computed_value",
        error_code="wrong_computed_value",
        summary=f"Observed value is `{actual_value:g}`, but `{expected_value:g}` is required.",
        field_path=observed.get("field_path") or source_path,
        expected_behavior=f"Return `{expected_value:g}` after `{operation}`.",
        actual_behavior=f"Returned `{actual_value:g}` instead.",
        counterexample={"before": before_value, "delta": delta},
        evidence=[
            f"Primary evidence source: {observed['source']}.",
            f"Resolved input value `{before_value:g}` at `{source_path}` with delta `{delta:g}`.",
        ],
        expected_extra={"operation": operation, "value": expected_value},
        actual_extra={"operation": operation, "value": actual_value},
        must_change=[f"Apply `{operation}` with delta `{delta:g}` to the current input value."],
        suggested_patch="Fix the arithmetic so the returned value matches the requested increment/decrement.",
        confidence=1.0,
    )


def _evaluate_string_length_evidence(payload: SemanticLogicVerifierInput) -> SemanticLogicVerifierResult | None:
    source_path, source_value = _resolve_source_value(payload)
    observed = _resolve_observed_value(payload)
    if observed is None or not isinstance(source_value, str):
        return None
    actual_value = _coerce_numeric_result(observed.get("value"))
    if actual_value is None:
        return None
    expected_value = float(len(source_value))
    if actual_value == expected_value:
        return _build_passed_result(
            summary=f"Observed {observed['source']} matches the expected string length `{int(expected_value)}`.",
            evidence=[f"Primary evidence source: {observed['source']}.", f"Resolved source string at `{source_path}`."],
            confidence=1.0,
        )
    return _build_failed_result(
        error_family="computed_value",
        error_code="wrong_computed_value",
        summary=f"Observed value is `{actual_value:g}`, but the string length is `{int(expected_value)}`.",
        field_path=observed.get("field_path") or source_path,
        expected_behavior=f"Return string length `{int(expected_value)}`.",
        actual_behavior=f"Returned `{actual_value:g}` instead.",
        counterexample={"source": source_value[:40]},
        evidence=[
            f"Primary evidence source: {observed['source']}.",
            f"The source string at `{source_path}` has length `{int(expected_value)}`.",
        ],
        expected_extra={"operation": "string_length", "value": int(expected_value)},
        actual_extra={"operation": "string_length", "value": actual_value},
        must_change=[f"Return the real string length `{int(expected_value)}`."],
        suggested_patch="Compute the length from the actual input string instead of another value.",
        confidence=1.0,
    )


def _evaluate_semantic_evidence(payload: SemanticLogicVerifierInput) -> SemanticLogicVerifierResult | None:
    operation = _derive_selected_operation(payload)
    if operation == "filter":
        return _evaluate_filter_evidence(payload)
    if operation == "count":
        return _evaluate_count_evidence(payload)
    if operation == "sum":
        return _evaluate_sum_evidence(payload)
    if operation in {"first", "last"}:
        return _evaluate_first_last_evidence(payload, operation=operation)
    if operation in {"increment", "decrement"}:
        return _evaluate_incremental_evidence(payload, operation=operation)
    if operation == "string_length":
        return _evaluate_string_length_evidence(payload)
    return None


def _task_requires_negation(task: str) -> bool:
    lowered = f" {task.lower()} "
    return any(marker in lowered for marker in _NOT_MARKERS)


def _extract_condition_text(code: str) -> str:
    matches = _IF_LINE_RE.findall(code or "")
    if matches:
        return " ".join(match.strip().lower() for match in matches)
    return str(code or "").lower()


def _detect_local_logic_failure(payload: SemanticLogicVerifierInput) -> SemanticLogicVerifierResult | None:
    task = str(payload.get("task", "") or "")
    code = str(payload.get("code", "") or "")
    operation = _derive_selected_operation(payload)
    field_path = _primary_field_path(payload)
    source_path = _derive_source_field_path(payload)
    condition_text = _extract_condition_text(code)

    expected_connector = _infer_boolean_connector(task)
    if expected_connector == "and" and " or " in condition_text and " and " not in condition_text:
        return _build_failed_result(
            error_family="boolean_logic",
            error_code="boolean_operator_mismatch",
            summary="The semantic condition uses `or` where the task requires `and`.",
            field_path=field_path,
            expected_behavior="Require all requested conditions at the same time.",
            actual_behavior="Accepts items when only one of the required conditions is true.",
            counterexample={"task_logic": "and", "code_logic": "or"},
            evidence=["Task text requires `and` semantics.", "Code condition contains `or` without a matching `and` branch."],
            expected_extra={"operation": operation or "filter"},
            actual_extra={"operation": operation or "filter"},
            must_change=["Replace the `or` condition with the required `and` logic."],
            suggested_patch="Rewrite the boolean condition so every required predicate must hold.",
            confidence=0.98,
        )

    if expected_connector == "or" and " and " in condition_text and " or " not in condition_text:
        return _build_failed_result(
            error_family="boolean_logic",
            error_code="boolean_operator_mismatch",
            summary="The semantic condition uses `and` where the task requires `or`.",
            field_path=field_path,
            expected_behavior="Accept items when any requested condition matches.",
            actual_behavior="Requires all conditions to be true at once.",
            counterexample={"task_logic": "or", "code_logic": "and"},
            evidence=["Task text requires `or` semantics.", "Code condition contains `and` without a matching `or` branch."],
            expected_extra={"operation": operation or "filter"},
            actual_extra={"operation": operation or "filter"},
            must_change=["Replace the `and` condition with the required `or` logic."],
            suggested_patch="Rewrite the boolean condition so any valid branch can pass.",
            confidence=0.98,
        )

    if _task_requires_negation(task) and " not " not in condition_text and "~=" not in condition_text and "!=" not in condition_text:
        return _build_failed_result(
            error_family="boolean_logic",
            error_code="missing_negation",
            summary="The task requires a negated semantic condition, but the code does not show one.",
            field_path=field_path,
            expected_behavior="Apply the requested negation in the semantic condition.",
            actual_behavior="Evaluates only the positive condition.",
            counterexample={"task_requires_negation": True},
            evidence=["Task text contains a negation marker.", "Code condition lacks `not`, `~=`, or `!=`."],
            expected_extra={"operation": operation or "filter"},
            actual_extra={"operation": operation or "filter"},
            must_change=["Add the required negated condition to the semantic logic."],
            suggested_patch="Negate the relevant predicate instead of checking only the positive branch.",
            confidence=0.95,
        )

    if source_path:
        direct_return = _RETURN_SOURCE_RE.search(code)
        if direct_return and direct_return.group(1).strip() == source_path and operation in {
            "count",
            "sum",
            "increment",
            "decrement",
            "string_length",
        }:
            return _build_failed_result(
                error_family="semantic_transformation",
                error_code="wrong_transformation",
                summary=f"The code returns `{source_path}` directly instead of performing the requested `{operation}` logic.",
                field_path=field_path,
                expected_behavior=f"Apply the requested `{operation}` logic before returning a result.",
                actual_behavior=f"Returns `{source_path}` unchanged.",
                counterexample={"source_path": source_path},
                evidence=["Detected a direct return of the source workflow path.", f"The requested operation is `{operation}`."],
                expected_extra={"operation": operation},
                actual_extra={"operation": operation},
                must_change=[f"Compute the `{operation}` result instead of returning `{source_path}` unchanged."],
                suggested_patch=f"Replace the direct return with real `{operation}` logic based on `{source_path}`.",
                confidence=0.97,
            )

    if operation == "first" and _LAST_INDEX_RE.search(code):
        return _build_failed_result(
            error_family="semantic_logic",
            error_code="wrong_computed_value",
            summary="The code reads the last item while the task asks for the first one.",
            field_path=field_path,
            expected_behavior="Return the first item from the source list.",
            actual_behavior="Reads the last item instead.",
            counterexample={"expected": "first", "actual": "last"},
            evidence=["The requested operation is `first`.", "The code uses a last-item style index (`#array`)."],
            expected_extra={"operation": "first"},
            actual_extra={"operation": "first"},
            must_change=["Use the first item instead of the last item."],
            suggested_patch="Replace the last-item index with the first-item access path.",
            confidence=0.98,
        )

    if operation == "last" and _FIRST_INDEX_RE.search(code):
        return _build_failed_result(
            error_family="semantic_logic",
            error_code="wrong_computed_value",
            summary="The code reads the first item while the task asks for the last one.",
            field_path=field_path,
            expected_behavior="Return the last item from the source list.",
            actual_behavior="Reads the first item instead.",
            counterexample={"expected": "last", "actual": "first"},
            evidence=["The requested operation is `last`.", "The code uses a first-item style index (`[1]`)."],
            expected_extra={"operation": "last"},
            actual_extra={"operation": "last"},
            must_change=["Use the last item instead of the first item."],
            suggested_patch="Replace the first-item access with last-item logic.",
            confidence=0.98,
        )

    return None


def _normalize_semantic_logic_verifier_result(raw: object) -> SemanticLogicVerifierResult:
    data = raw if isinstance(raw, dict) else {}
    passed = bool(data.get("passed", False))
    severity = str(data.get("severity", "") or "").strip().lower()
    if severity not in _VALID_SEVERITIES:
        severity = "low" if passed else "high"

    summary = str(data.get("summary", "") or "").strip()
    if not summary:
        summary = "Semantic logic check passed." if passed else "Semantic logic verification failed."

    error_family = _normalize_nullable_string(data.get("error_family"))
    error_code = _normalize_nullable_string(data.get("error_code"))
    field_path = _normalize_nullable_string(data.get("field_path"))
    if passed:
        error_family = None
        error_code = None
        field_path = None

    expected = _ensure_object(data.get("expected"))
    actual = _ensure_object(data.get("actual"))
    if not passed:
        expected["expected_behavior"] = _normalize_behavior_text(
            expected.get("expected_behavior"),
            "Follow the requested semantic behavior.",
        )
        actual["actual_behavior"] = _normalize_behavior_text(actual.get("actual_behavior"), summary)

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


def _build_semantic_logic_verifier_prompt(payload: SemanticLogicVerifierInput) -> str:
    task = str(payload.get("task", "") or "").strip()
    code = str(payload.get("code", "") or "").strip()
    source_path = _derive_source_field_path(payload)
    output_path = _derive_output_field_path(payload)
    operation = _derive_selected_operation(payload)
    semantic_expectations = _ensure_string_list(payload.get("semantic_expectations"))
    requested_item_keys = _ensure_string_list(payload.get("requested_item_keys"))

    source_value_section = ""
    observed_value_section = ""
    resolved_source_path, resolved_source_value = _resolve_source_value(payload)
    if resolved_source_path and resolved_source_value is not None:
        source_value_section = f"Original source value at {resolved_source_path}:\n{_compact_json(resolved_source_value)}"
    observed = _resolve_observed_value(payload)
    if observed is not None:
        observed_label = observed.get("field_path") or "return value"
        observed_value_section = f"Observed value from {observed['source']} at {observed_label}:\n{_compact_json(observed.get('value'))}"

    expectation_lines: list[str] = []
    if operation:
        expectation_lines.append("- selected operation: " + operation)
    if source_path:
        expectation_lines.append("- source field path: " + source_path)
    if output_path:
        expectation_lines.append("- output field path: " + output_path)
    if payload.get("operation_argument") is not None:
        expectation_lines.append("- operation argument: " + _compact_json(payload.get("operation_argument"), limit=200))
    if semantic_expectations:
        expectation_lines.append("- semantic expectations: " + ", ".join(semantic_expectations))
    if requested_item_keys:
        expectation_lines.append("- requested item keys: " + ", ".join(requested_item_keys))

    sections = [
        f"Task:\n{task}" if task else "",
        "Scope:\nCheck only semantic logic mismatches. Ignore workflow contract issues and shape-only issues.",
        "Semantic expectations:\n" + "\n".join(expectation_lines) if expectation_lines else "",
        (
            "Parsed workflow context:\n" + _compact_json(payload.get("parsed_context"))
            if payload.get("parsed_context") is not None
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
        (
            "runtime_result:\n" + _compact_json(payload.get("runtime_result"))
            if payload.get("runtime_result") is not None
            else ""
        ),
        source_value_section,
        observed_value_section,
        f"Lua solution under review:\n```lua\n{code}\n```",
        "Return strict JSON in this exact shape:\n" + json.dumps(_OUTPUT_SHAPE_EXAMPLE, ensure_ascii=False, indent=2),
    ]
    return _build_prompt_sections(*sections)


def build_semantic_logic_verifier_input_from_state(state: dict[str, Any]) -> SemanticLogicVerifierInput:
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
        state.get("semantic_logic_output_field_path")
        or compiled_request.get("semantic_logic_output_field_path")
        or compiled_request.get("selected_save_path")
        or ""
    ).strip()

    expected_workflow_paths = _unique_strings(
        _ensure_string_list(compiled_request.get("expected_workflow_paths"))
        + ([source_field_path] if source_field_path else [])
        + ([output_field_path] if output_field_path else [])
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
        "source_field_path": source_field_path or None,
        "output_field_path": output_field_path or None,
        "expected_workflow_paths": expected_workflow_paths,
        "selected_operation": _normalize_nullable_string(compiled_request.get("selected_operation")),
        "operation_argument": compiled_request.get("operation_argument"),
        "semantic_expectations": _ensure_string_list(compiled_request.get("semantic_expectations")),
        "requested_item_keys": _ensure_string_list(compiled_request.get("requested_item_keys")),
        "parsed_context": parsed_context,
        "runtime_result": runtime_result,
        "before_state": before_state,
        "after_state": diagnostics.get("workflow_state"),
    }


def to_aggregate_verification_result(result: SemanticLogicVerifierResult) -> dict[str, Any]:
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


class SemanticLogicVerifierAgent:
    """LLM-backed semantic verifier with evidence-first and local-guard behavior."""

    def __init__(self, llm: LLMProvider) -> None:
        self._llm = llm

    @property
    def name(self) -> str:
        return _AGENT_NAME

    async def verify(self, payload: SemanticLogicVerifierInput) -> SemanticLogicVerifierResult:
        code = str(payload.get("code", "") or "")
        operation = _derive_selected_operation(payload)
        logger.info(
            f"[{_AGENT_NAME}] started",
            code_len=len(code),
            operation=operation or "unknown",
            source_field_path=_derive_source_field_path(payload) or "none",
            output_field_path=_derive_output_field_path(payload) or "none",
            has_runtime_result=payload.get("runtime_result") is not None,
            has_after_state=payload.get("after_state") is not None,
        )

        evidence_result = _evaluate_semantic_evidence(payload)
        if evidence_result is not None:
            logger.info(
                f"[{_AGENT_NAME}] evidence_result",
                passed=evidence_result["passed"],
                error_code=evidence_result["error_code"] or "none",
                field_path=evidence_result["field_path"] or "none",
            )
            return evidence_result

        local_failure = _detect_local_logic_failure(payload)
        if local_failure is not None:
            logger.info(
                f"[{_AGENT_NAME}] local_failure",
                error_code=local_failure["error_code"],
                field_path=local_failure["field_path"] or "none",
            )
            return local_failure

        prompt = _build_semantic_logic_verifier_prompt(payload)
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
                summary=f"SemanticLogicVerifier could not produce a valid verdict: {exc}",
                field_path=_primary_field_path(payload),
                expected_behavior="Produce a semantic verdict for the current solution.",
                actual_behavior="No structured semantic verdict was produced.",
                counterexample={"error": str(exc)},
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
        result = _normalize_semantic_logic_verifier_result(raw)
        logger.info(
            f"[{_AGENT_NAME}] completed",
            passed=result["passed"],
            error_code=result["error_code"] or "none",
            field_path=result["field_path"] or "none",
            confidence=result["confidence"],
        )
        return result


def create_semantic_logic_verifier_node(llm: LLMProvider) -> Callable:
    agent = SemanticLogicVerifierAgent(llm)

    async def verify_semantic_logic(state: dict[str, Any]) -> SemanticLogicVerifierNodeOutput:
        payload = build_semantic_logic_verifier_input_from_state(state)
        result = await agent.verify(payload)
        aggregate = to_aggregate_verification_result(result)
        return {
            "semantic_logic_verifier_result": result,
            "verification": aggregate,
            "verification_passed": bool(result["passed"]),
            "failure_stage": "" if result["passed"] else "semantic_logic_verification",
        }

    return verify_semantic_logic
