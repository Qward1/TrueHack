"""Standalone universal post-verification fixer for Lua workflow code.

This agent is intentionally self-contained.
It consumes the unified verifier output contract and produces one fix candidate
for any verifier in the new standalone verification pipeline.
"""

from __future__ import annotations

import json
import re
from typing import Any, Callable, TypedDict

import structlog

from src.core.llm import LLMProvider

logger = structlog.get_logger(__name__)

_AGENT_NAME = "UniversalVerificationFixer"
_SYSTEM_PROMPT = """You are UniversalVerificationFixer.
You fix Lua workflow code after verification.
Do not solve the whole task from scratch unless patch_scope says rewrite.
Use verifier_result as the main source of truth.
Your job:
- understand the exact reported problem
- keep working parts unchanged
- apply the smallest valid patch when possible
- obey must_change, must_preserve, forbidden_fixes
Use only the data explicitly provided in the input.
Do not invent workflow paths, variables, fields, helpers, runtime evidence, or state changes.
You may use only names from allowed_workflow_paths, available_code_variables, verifier_result, current_lua_code, or focused evidence sections.
Patch only the reported problem.
Do not introduce new helper functions, workflow paths, or variables unless they already exist in the current code or are explicitly required by verifier_result.
Do not ignore the verifier diagnosis.
Do not return the same code unless no valid patch is possible.
Return JSON only.

Output schema:
{
  "fixed": true,
  "changed": true,
  "applied_error_family": "string",
  "applied_error_code": "string",
  "applied_strategy": "string",
  "preserved_constraints": [],
  "remaining_risks": [],
  "fixed_lua_body": "plain lua code string without lua{ }lua wrapper"
}

Rules:
- fixed_lua_body must be a JSON string with Lua code only, without markdown fences and without lua{ }lua
- if verifier_result.passed=true, keep code unchanged
- if verifier_result.passed=false, patch exactly that reported error
- preserve the stated working parts
- never use forbidden_fixes
- if no semantic change was possible, return changed=false"""

_VALID_PATCH_SCOPES = {"none", "local", "function_level", "multi_block", "rewrite"}
_ZERO_WIDTH_RE = re.compile(r"[\u200b-\u200d\ufeff]")
_FENCED_RE = re.compile(r"^```(?:[A-Za-z0-9_-]+)?\s*([\s\S]*?)```$", re.IGNORECASE)
_LOWCODE_WRAPPER_RE = re.compile(r"^lua\{\s*([\s\S]*?)\s*\}lua$", re.IGNORECASE)
_WORKFLOW_PATH_RE = re.compile(r"wf\.(?:vars|initVariables)(?:\.[A-Za-z_][A-Za-z0-9_]*)+")


class FixerBrief(TypedDict):
    goal: str
    must_change: list[str]
    must_preserve: list[str]
    forbidden_fixes: list[str]
    suggested_patch: str
    patch_scope: str


class NormalizedVerifierResult(TypedDict):
    verifier_name: str
    passed: bool
    error_family: str
    error_code: str
    severity: str
    summary: str
    field_path: str | None
    evidence: list[str]
    expected: dict[str, Any]
    actual: dict[str, Any]
    fixer_brief: FixerBrief
    confidence: float


class UniversalVerificationFixerInput(TypedDict, total=False):
    task: str
    code: str
    workflow_context: object
    before_state: object
    after_state: object
    runtime_result: object
    verifier_result: object
    previous_fix_attempts: list[object]
    allowed_workflow_paths: list[str]
    available_code_variables: list[str]
    available_runtime_evidence: dict[str, bool]


class UniversalVerificationFixerResult(TypedDict):
    fixed: bool
    changed: bool
    applied_error_family: str
    applied_error_code: str
    applied_strategy: str
    preserved_constraints: list[str]
    remaining_risks: list[str]
    fixed_lua_code: str


class UniversalVerificationFixerNodeOutput(TypedDict):
    generated_code: str
    universal_verification_fixer_result: UniversalVerificationFixerResult
    fix_verification_iterations: int
    failure_stage: str
    validation_passed: bool
    verification_passed: bool
    save_success: bool
    save_skipped: bool
    save_skip_reason: str
    save_error: str
    saved_to: str
    saved_jsonstring_to: str
    explanation: dict[str, Any]
    suggested_changes: list[str]
    clarifying_questions: list[str]
    previous_fix_attempts: list[dict[str, Any]]
    verification_chain_current_verifier: str
    verification_chain_current_node: str
    verification_chain_current_index: int
    verification_chain_current_stage_passed: bool
    verification_chain_current_failure_stage: str
    verification_chain_next_verifier: str
    verification_chain_next_node: str
    verification_chain_last_transition: str
    verification_chain_stage_fix_counts: dict[str, int]
    verification_chain_stage_fix_limits: dict[str, int]
    verification_chain_stage_results: dict[str, dict[str, Any]]
    verification_chain_history: list[dict[str, Any]]


def _normalize_nullable_string(value: object) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _ensure_string_list(value: object) -> list[str]:
    if isinstance(value, str):
        text = value.strip()
        return [text] if text else []
    if not isinstance(value, list):
        return []
    normalized: list[str] = []
    for item in value:
        text = str(item).strip()
        if text:
            normalized.append(text)
    return normalized


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


def _ensure_list(value: object) -> list[Any]:
    return list(value) if isinstance(value, list) else []


def _normalize_string(value: object) -> str:
    return str(value or "").strip()


def _normalize_nonnegative_int(value: object, *, default: int = 0) -> int:
    try:
        numeric = int(value)
    except (TypeError, ValueError):
        return default
    return numeric if numeric >= 0 else default


def _clamp_confidence(value: object) -> float:
    try:
        confidence = float(value)
    except (TypeError, ValueError):
        return 0.0
    if confidence < 0.0:
        return 0.0
    if confidence > 1.0:
        return 1.0
    return confidence


def _compact_json(value: object) -> str:
    try:
        return json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True)
    except TypeError:
        return json.dumps(str(value), ensure_ascii=False)


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


def _strip_markdown_fence(text: str) -> str:
    cleaned = str(text or "").strip()
    match = _FENCED_RE.match(cleaned)
    if not match:
        return cleaned
    return str(match.group(1) or "").strip()


def _unwrap_lua_wrapper(text: str) -> str:
    cleaned = _ZERO_WIDTH_RE.sub("", str(text or "")).replace("\r\n", "\n").replace("\r", "\n").strip()
    previous = None
    while cleaned and cleaned != previous:
        previous = cleaned
        cleaned = _strip_markdown_fence(cleaned)
        quoted = cleaned[:1] if cleaned[:1] in {"'", '"'} else ""
        if quoted and cleaned.endswith(quoted):
            cleaned = cleaned[1:-1].strip()
        match = _LOWCODE_WRAPPER_RE.fullmatch(cleaned)
        if not match:
            break
        cleaned = str(match.group(1) or "").strip()
    return cleaned


def _wrap_lua_code(lua_code: str) -> str:
    body = _unwrap_lua_wrapper(lua_code)
    return f"lua{{\n{body.strip()}\n}}lua"


def _normalize_code_for_storage(lua_code: str) -> str:
    return _unwrap_lua_wrapper(lua_code).strip()


def _code_signature(lua_code: str) -> str:
    normalized = _normalize_code_for_storage(lua_code)
    lines = [line.rstrip() for line in normalized.replace("\r\n", "\n").replace("\r", "\n").split("\n")]
    return "\n".join(lines).strip()


def _normalize_fixer_brief(raw: object) -> FixerBrief:
    data = raw if isinstance(raw, dict) else {}
    must_change = _ensure_string_list(data.get("must_change"))
    patch_scope = str(data.get("patch_scope", "") or "").strip().lower()
    if patch_scope not in _VALID_PATCH_SCOPES:
        patch_scope = "local" if must_change else "none"
    return {
        "goal": str(data.get("goal", "") or "").strip(),
        "must_change": must_change,
        "must_preserve": _ensure_string_list(data.get("must_preserve")),
        "forbidden_fixes": _ensure_string_list(data.get("forbidden_fixes")),
        "suggested_patch": str(data.get("suggested_patch", "") or "").strip(),
        "patch_scope": patch_scope,
    }


def _normalize_verifier_result(raw: object) -> NormalizedVerifierResult:
    data = raw if isinstance(raw, dict) else {}
    passed = bool(data.get("passed", False))
    field_path = _normalize_nullable_string(data.get("field_path"))
    return {
        "verifier_name": str(data.get("verifier_name", "") or "UnknownVerifier").strip() or "UnknownVerifier",
        "passed": passed,
        "error_family": str(data.get("error_family", "") or "").strip(),
        "error_code": str(data.get("error_code", "") or "").strip(),
        "severity": str(data.get("severity", "") or ("low" if passed else "high")).strip().lower() or ("low" if passed else "high"),
        "summary": str(data.get("summary", "") or ("Verification passed." if passed else "Verification failed.")).strip(),
        "field_path": field_path,
        "evidence": _ensure_string_list(data.get("evidence")),
        "expected": _ensure_object(data.get("expected")),
        "actual": _ensure_object(data.get("actual")),
        "fixer_brief": _normalize_fixer_brief(data.get("fixer_brief")),
        "confidence": _clamp_confidence(data.get("confidence")),
    }


def _collect_verifier_related_paths(raw: object) -> list[str]:
    if not isinstance(raw, dict):
        return []
    fragments: list[str] = []
    field_path = _normalize_nullable_string(raw.get("field_path"))
    if field_path:
        fragments.append(field_path)
    for key in ("expected", "actual", "fixer_brief"):
        value = raw.get(key)
        if value is None:
            continue
        fragments.append(_compact_json(value))
    fragments.append(str(raw.get("summary", "") or ""))
    return _unique_strings(_extract_workflow_paths("\n".join(fragments)))


def _build_verifier_diagnosis_section(verifier_result: NormalizedVerifierResult) -> str:
    lines = [
        f"- verifier_name: {verifier_result['verifier_name']}",
        f"- passed: {str(verifier_result['passed']).lower()}",
        f"- error_family: {verifier_result['error_family'] or ''}",
        f"- error_code: {verifier_result['error_code'] or ''}",
        f"- severity: {verifier_result['severity']}",
        f"- summary: {verifier_result['summary']}",
        f"- field_path: {verifier_result['field_path'] or ''}",
    ]
    sections = [
        "Verifier diagnosis:\n" + "\n".join(lines),
        "Verifier evidence:\n" + _compact_json(verifier_result["evidence"][:5]),
        "Verifier expected:\n" + _compact_json(verifier_result["expected"]),
        "Verifier actual:\n" + _compact_json(verifier_result["actual"]),
    ]
    return "\n\n".join(section for section in sections if section.strip())


def _normalize_previous_fix_attempts(value: object) -> list[dict[str, Any]]:
    attempts: list[dict[str, Any]] = []
    if not isinstance(value, list):
        return attempts
    for item in value:
        if isinstance(item, dict):
            attempts.append(dict(item))
            continue
        text = str(item).strip()
        if text:
            attempts.append({"note": text})
    return attempts


def _normalize_history_entries(value: object) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    entries: list[dict[str, Any]] = []
    for item in value:
        if isinstance(item, dict):
            entries.append(dict(item))
    return entries


def _normalize_stage_results(value: object) -> dict[str, dict[str, Any]]:
    if not isinstance(value, dict):
        return {}
    normalized: dict[str, dict[str, Any]] = {}
    for key, item in value.items():
        verifier_name = _normalize_string(key)
        if verifier_name and isinstance(item, dict):
            normalized[verifier_name] = dict(item)
    return normalized


def _normalize_stage_fix_counts(value: object) -> dict[str, int]:
    if not isinstance(value, dict):
        return {}
    normalized: dict[str, int] = {}
    for key, item in value.items():
        verifier_name = _normalize_string(key)
        if verifier_name:
            normalized[verifier_name] = _normalize_nonnegative_int(item, default=0)
    return normalized


def _normalize_stage_fix_limits(value: object) -> dict[str, int]:
    if not isinstance(value, dict):
        return {}
    normalized: dict[str, int] = {}
    for key, item in value.items():
        verifier_name = _normalize_string(key)
        if verifier_name:
            normalized[verifier_name] = max(1, _normalize_nonnegative_int(item, default=1))
    return normalized


def _build_noop_result(code: str, verifier_result: NormalizedVerifierResult, *, reason: str) -> UniversalVerificationFixerResult:
    return {
        "fixed": True,
        "changed": False,
        "applied_error_family": verifier_result["error_family"],
        "applied_error_code": verifier_result["error_code"],
        "applied_strategy": reason,
        "preserved_constraints": list(verifier_result["fixer_brief"]["must_preserve"]),
        "remaining_risks": [],
        "fixed_lua_code": _wrap_lua_code(code),
    }


def _build_failure_result(
    code: str,
    verifier_result: NormalizedVerifierResult,
    *,
    strategy: str,
    risk: str,
) -> UniversalVerificationFixerResult:
    return {
        "fixed": False,
        "changed": False,
        "applied_error_family": verifier_result["error_family"],
        "applied_error_code": verifier_result["error_code"],
        "applied_strategy": strategy,
        "preserved_constraints": list(verifier_result["fixer_brief"]["must_preserve"]),
        "remaining_risks": [risk],
        "fixed_lua_code": _wrap_lua_code(code),
    }


def _normalize_universal_verification_fixer_result(
    raw: object,
    *,
    original_code: str,
    verifier_result: NormalizedVerifierResult,
) -> UniversalVerificationFixerResult:
    data = raw if isinstance(raw, dict) else {}
    fixed_lua_body = str(data.get("fixed_lua_body", "") or "").strip()
    fixed_lua_code = _wrap_lua_code(
        fixed_lua_body or str(data.get("fixed_lua_code", "") or original_code)
    )
    changed = _code_signature(fixed_lua_code) != _code_signature(original_code)
    fixed = bool(data.get("fixed", False))

    if verifier_result["passed"]:
        fixed = True
        changed = False
        fixed_lua_code = _wrap_lua_code(original_code)
    elif not changed:
        fixed = False

    strategy = str(data.get("applied_strategy", "") or "").strip()
    if not strategy:
        patch_scope = verifier_result["fixer_brief"]["patch_scope"]
        strategy = "rewrite_from_verifier_brief" if patch_scope == "rewrite" else "minimal_patch_from_verifier_brief"

    preserved_constraints = _ensure_string_list(data.get("preserved_constraints"))
    if not preserved_constraints:
        preserved_constraints = list(verifier_result["fixer_brief"]["must_preserve"])

    return {
        "fixed": fixed,
        "changed": changed,
        "applied_error_family": str(data.get("applied_error_family", "") or verifier_result["error_family"]).strip(),
        "applied_error_code": str(data.get("applied_error_code", "") or verifier_result["error_code"]).strip(),
        "applied_strategy": strategy,
        "preserved_constraints": preserved_constraints,
        "remaining_risks": _ensure_string_list(data.get("remaining_risks")),
        "fixed_lua_code": fixed_lua_code,
    }


def _render_attempts(attempts: list[dict[str, Any]]) -> str:
    if not attempts:
        return "[]"
    return _compact_json(attempts[:5])


def _build_universal_verification_fixer_prompt(payload: UniversalVerificationFixerInput) -> str:
    task = str(payload.get("task", "") or "").strip()
    code = _normalize_code_for_storage(str(payload.get("code", "") or ""))
    workflow_context = _normalize_workflow_snapshot(payload.get("workflow_context"))
    before_state = _normalize_workflow_snapshot(payload.get("before_state"))
    after_state = _normalize_workflow_snapshot(payload.get("after_state"))
    verifier_result = _normalize_verifier_result(payload.get("verifier_result"))
    previous_attempts = _normalize_previous_fix_attempts(payload.get("previous_fix_attempts"))
    allowed_workflow_paths = _unique_strings(
        _ensure_string_list(payload.get("allowed_workflow_paths"))
        or _collect_verifier_related_paths(payload.get("verifier_result"))
        or _extract_workflow_paths(code)
    )
    available_code_variables = _unique_strings(
        _ensure_string_list(payload.get("available_code_variables")) or _extract_code_variables(code)
    )
    available_runtime_evidence = payload.get("available_runtime_evidence")
    if not isinstance(available_runtime_evidence, dict):
        available_runtime_evidence = {
            "workflow_context": workflow_context is not None,
            "before_state": before_state is not None,
            "after_state": after_state is not None,
            "runtime_result": payload.get("runtime_result") is not None,
            "previous_fix_attempts": bool(previous_attempts),
        }
    focused_sections: list[str] = []
    focused_path = verifier_result["field_path"]
    if focused_path:
        for label, root in (
            ("workflow_context", workflow_context),
            ("before_state", before_state),
            ("after_state", after_state),
        ):
            if root is None:
                continue
            found, value = _resolve_workflow_path_value(root, focused_path)
            if found:
                focused_sections.append(f"{label} value at {focused_path}:\n{_compact_json(value)}")

    sections = [
        f"Task:\n{task}" if task else "",
        "Current broken Lua code:\n" + _wrap_lua_code(code),
        "Strict rules:\n"
        "- Use only explicit input data.\n"
        "- Never invent workflow paths, variables, fields, helpers, or runtime evidence.\n"
        "- You may reference only names from allowed_workflow_paths, available_code_variables, verifier_result, the current Lua code, or focused evidence sections.\n"
        "- Patch only the reported problem and keep unrelated code unchanged.\n"
        "- If no real patch is possible, keep the code unchanged and return changed=false.",
        _build_verifier_diagnosis_section(verifier_result),
        "Fix strategy:\n"
        + "\n".join(
            [
                f"- goal: {verifier_result['fixer_brief']['goal'] or verifier_result['summary']}",
                f"- patch_scope: {verifier_result['fixer_brief']['patch_scope']}",
                f"- must_change: {verifier_result['fixer_brief']['must_change']}",
                f"- must_preserve: {verifier_result['fixer_brief']['must_preserve']}",
                f"- forbidden_fixes: {verifier_result['fixer_brief']['forbidden_fixes']}",
                f"- suggested_patch: {verifier_result['fixer_brief']['suggested_patch']}",
            ]
        ),
        _format_named_list("allowed_workflow_paths", allowed_workflow_paths),
        _format_named_list("available_code_variables", available_code_variables),
        _render_presence_map("available_runtime_evidence", available_runtime_evidence),
        (
            "runtime_result:\n" + _compact_json(payload.get("runtime_result"))
            if payload.get("runtime_result") is not None
            else ""
        ),
        *focused_sections,
        "previous_fix_attempts:\n" + _render_attempts(previous_attempts),
        "Output rules:\n"
        "- fixed_lua_body must be a JSON string containing only the Lua code body.\n"
        "- Do not include lua{ }lua inside fixed_lua_body.\n"
        "- Do not include markdown fences anywhere in the JSON.",
        (
            "Return JSON only with fields fixed, changed, applied_error_family, applied_error_code, "
            "applied_strategy, preserved_constraints, remaining_risks, fixed_lua_body."
        ),
    ]
    return "\n\n".join(section for section in sections if section)


def _select_verifier_result_from_state(state: dict[str, Any]) -> object:
    current_verifier = _normalize_string(state.get("verification_chain_current_verifier"))
    stage_results = state.get("verification_chain_stage_results")
    if current_verifier and isinstance(stage_results, dict):
        current_stage_result = stage_results.get(current_verifier)
        if isinstance(current_stage_result, dict) and current_stage_result.get("verifier_name"):
            return current_stage_result

    verification = state.get("verification")
    if isinstance(verification, dict) and verification.get("verifier_name"):
        return verification

    fallback: object = {}
    for key, value in state.items():
        if key.endswith("_verifier_result") and isinstance(value, dict) and value.get("verifier_name"):
            fallback = value
    return fallback


def build_universal_verification_fixer_input_from_state(state: dict[str, Any]) -> UniversalVerificationFixerInput:
    compiled_request = state.get("compiled_request", {})
    diagnostics = state.get("diagnostics", {})

    if not isinstance(compiled_request, dict):
        compiled_request = {}
    if not isinstance(diagnostics, dict):
        diagnostics = {}

    task = str(compiled_request.get("verification_prompt", "") or "").strip()
    if not task:
        task = str(compiled_request.get("task_text", "") or "").strip()
    if not task:
        task = str(compiled_request.get("original_task", "") or "").strip()
    if not task:
        task = str(state.get("user_input", "") or "").strip()

    verifier_result = _select_verifier_result_from_state(state)
    runtime_result = diagnostics.get("result_value")
    if runtime_result is None:
        preview = diagnostics.get("result_preview")
        if preview not in (None, ""):
            runtime_result = preview

    parsed_context = compiled_request.get("parsed_context")
    before_state = diagnostics.get("before_state")
    if before_state is None and compiled_request.get("has_parseable_context"):
        before_state = parsed_context
    before_state = _normalize_workflow_snapshot(before_state)
    after_state = _normalize_workflow_snapshot(diagnostics.get("workflow_state"))
    code = str(state.get("generated_code", "") or state.get("current_code", "") or "")
    selected_primary_path = _normalize_string(compiled_request.get("selected_primary_path"))
    selected_save_path = _normalize_string(compiled_request.get("selected_save_path"))
    expected_workflow_paths = _ensure_string_list(compiled_request.get("expected_workflow_paths"))
    allowed_workflow_paths = _unique_strings(
        _extract_inventory_paths(compiled_request.get("workflow_path_inventory"))
        + _extract_workflow_paths(code)
        + ([selected_primary_path] if selected_primary_path else [])
        + ([selected_save_path] if selected_save_path else [])
        + expected_workflow_paths
        + _collect_verifier_related_paths(verifier_result)
    )
    previous_fix_attempts = _ensure_list(state.get("previous_fix_attempts"))

    return {
        "task": task,
        "code": code,
        "workflow_context": parsed_context,
        "before_state": before_state,
        "after_state": after_state,
        "runtime_result": runtime_result,
        "verifier_result": verifier_result,
        "previous_fix_attempts": previous_fix_attempts,
        "allowed_workflow_paths": allowed_workflow_paths,
        "available_code_variables": _extract_code_variables(code),
        "available_runtime_evidence": {
            "workflow_context": parsed_context is not None,
            "before_state": before_state is not None,
            "after_state": after_state is not None,
            "runtime_result": runtime_result is not None,
            "previous_fix_attempts": bool(previous_fix_attempts),
        },
    }


class UniversalVerificationFixerAgent:
    """LLM-backed fixer that patches exactly the verifier-reported issue."""

    def __init__(self, llm: LLMProvider) -> None:
        self._llm = llm

    @property
    def name(self) -> str:
        return _AGENT_NAME

    async def fix(self, payload: UniversalVerificationFixerInput) -> UniversalVerificationFixerResult:
        code = _normalize_code_for_storage(str(payload.get("code", "") or ""))
        verifier_result = _normalize_verifier_result(payload.get("verifier_result"))
        previous_attempts = _normalize_previous_fix_attempts(payload.get("previous_fix_attempts"))

        logger.info(
            f"[{_AGENT_NAME}] started",
            code_len=len(code),
            verifier_name=verifier_result["verifier_name"],
            verifier_passed=verifier_result["passed"],
            error_code=verifier_result["error_code"] or "none",
            patch_scope=verifier_result["fixer_brief"]["patch_scope"],
            previous_attempt_count=len(previous_attempts),
        )

        if verifier_result["passed"]:
            logger.info(f"[{_AGENT_NAME}] verifier_passed_noop")
            return _build_noop_result(code, verifier_result, reason="verifier_passed")

        prompt = _build_universal_verification_fixer_prompt(payload)
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
            return _build_failure_result(
                code,
                verifier_result,
                strategy="llm_unavailable",
                risk=f"Fixer could not produce a patch: {exc}",
            )

        logger.info(
            f"[{_AGENT_NAME}/llm.generate_json] done",
            raw_keys=list(raw.keys()) if isinstance(raw, dict) else [],
        )
        result = _normalize_universal_verification_fixer_result(
            raw,
            original_code=code,
            verifier_result=verifier_result,
        )

        if not result["changed"] and verifier_result["fixer_brief"]["patch_scope"] != "none":
            retry_payload = dict(payload)
            retry_attempts = list(previous_attempts)
            retry_attempts.append(
                {
                    "verifier_name": verifier_result["verifier_name"],
                    "error_code": verifier_result["error_code"],
                    "applied_strategy": result["applied_strategy"],
                    "changed": result["changed"],
                    "note": "Previous attempt returned unchanged code.",
                }
            )
            retry_payload["previous_fix_attempts"] = retry_attempts
            retry_prompt = _build_universal_verification_fixer_prompt(retry_payload) + (
                "\n\nPrevious attempt returned unchanged code. "
                "Apply a real patch now unless no valid patch is possible."
            )
            logger.info(f"[{_AGENT_NAME}/llm.generate_json] retry_calling")
            try:
                retry_raw = await self._llm.generate_json(
                    retry_prompt,
                    system=_SYSTEM_PROMPT,
                    agent_name=_AGENT_NAME,
                )
            except Exception as exc:
                logger.warning(f"[{_AGENT_NAME}/llm.generate_json] retry_failed", error=str(exc))
            else:
                retry_result = _normalize_universal_verification_fixer_result(
                    retry_raw,
                    original_code=code,
                    verifier_result=verifier_result,
                )
                if retry_result["changed"] or retry_result["fixed"] != result["fixed"]:
                    result = retry_result

        logger.info(
            f"[{_AGENT_NAME}] completed",
            changed=result["changed"],
            fixed=result["fixed"],
            applied_error_code=result["applied_error_code"] or "none",
            strategy=result["applied_strategy"] or "none",
            fixed_code_len=len(_normalize_code_for_storage(result["fixed_lua_code"])),
        )
        return result


def create_universal_verification_fixer_node(llm: LLMProvider) -> Callable:
    agent = UniversalVerificationFixerAgent(llm)

    async def fix_verification_issue(state: dict[str, Any]) -> UniversalVerificationFixerNodeOutput:
        payload = build_universal_verification_fixer_input_from_state(state)
        verifier_result = _normalize_verifier_result(payload.get("verifier_result"))
        fix_iter = _normalize_nonnegative_int(state.get("fix_verification_iterations"), default=0)
        previous_attempts = _normalize_previous_fix_attempts(state.get("previous_fix_attempts"))
        validation_passed = bool(state.get("validation_passed", False))
        current_failure_stage = _normalize_string(
            state.get("verification_chain_current_failure_stage")
        ) or _normalize_string(state.get("failure_stage"))
        current_verifier = _normalize_string(state.get("verification_chain_current_verifier")) or verifier_result["verifier_name"]
        stage_fix_counts = _normalize_stage_fix_counts(state.get("verification_chain_stage_fix_counts"))
        stage_fix_limits = _normalize_stage_fix_limits(state.get("verification_chain_stage_fix_limits"))
        stage_results = _normalize_stage_results(state.get("verification_chain_stage_results"))
        history = _normalize_history_entries(state.get("verification_chain_history"))

        result = await agent.fix(payload)
        original_code = _normalize_code_for_storage(str(state.get("generated_code", "") or state.get("current_code", "") or ""))
        fixed_code = _normalize_code_for_storage(result["fixed_lua_code"])

        verifier_passed = bool(verifier_result["passed"])
        changed = bool(result["changed"])
        if verifier_passed:
            updated_attempts: list[dict[str, Any]] = []
        else:
            new_attempt = {
                "verifier_name": current_verifier or verifier_result["verifier_name"],
                "error_code": result["applied_error_code"],
                "strategy": result["applied_strategy"],
                "changed": changed,
            }
            updated_attempts = previous_attempts + [new_attempt]
            stage_fix_counts[current_verifier or verifier_result["verifier_name"]] = (
                stage_fix_counts.get(current_verifier or verifier_result["verifier_name"], 0) + 1
            )
            stage_result_key = current_verifier or verifier_result["verifier_name"]
            stage_result = stage_results.get(stage_result_key)
            if isinstance(stage_result, dict):
                updated_stage_result = dict(stage_result)
                updated_stage_result["resolved_by_fixer"] = changed
                updated_stage_result["fixed_by_fixer"] = changed
                updated_stage_result["fixer_changed"] = changed
                updated_stage_result["fixer_applied_error_code"] = result["applied_error_code"]
                updated_stage_result["fixer_applied_strategy"] = result["applied_strategy"]
                stage_results[stage_result_key] = updated_stage_result

        next_fix_iter = fix_iter if verifier_passed else fix_iter + 1
        if current_verifier and current_verifier not in stage_fix_limits:
            stage_fix_limits[current_verifier] = 1

        history.append(
            {
                "entry_type": "fixer",
                "verifier_name": current_verifier or verifier_result["verifier_name"],
                "node_name": "fix_verification_issue",
                "failure_stage": current_failure_stage,
                "passed": verifier_passed,
                "error_code": result["applied_error_code"],
                "strategy": result["applied_strategy"],
                "changed": changed,
                "fixed": bool(result["fixed"]),
                "summary": verifier_result["summary"],
            }
        )

        accepted_code = fixed_code if changed else original_code

        return {
            "generated_code": accepted_code,
            "universal_verification_fixer_result": result,
            "fix_verification_iterations": next_fix_iter,
            "failure_stage": "" if verifier_passed else current_failure_stage,
            "validation_passed": validation_passed,
            "verification_passed": verifier_passed,
            "save_success": False,
            "save_skipped": False,
            "save_skip_reason": "",
            "save_error": "",
            "saved_to": "",
            "saved_jsonstring_to": "",
            "explanation": {},
            "suggested_changes": [],
            "clarifying_questions": [],
            "previous_fix_attempts": updated_attempts,
            "verification_chain_current_verifier": current_verifier,
            "verification_chain_current_node": _normalize_string(state.get("verification_chain_current_node")),
            "verification_chain_current_index": _normalize_nonnegative_int(
                state.get("verification_chain_current_index"),
                default=-1,
            ),
            "verification_chain_current_stage_passed": verifier_passed,
            "verification_chain_current_failure_stage": current_failure_stage,
            "verification_chain_next_verifier": _normalize_string(state.get("verification_chain_next_verifier")),
            "verification_chain_next_node": _normalize_string(state.get("verification_chain_next_node")),
            "verification_chain_last_transition": (
                "fixer_noop_pass"
                if verifier_passed
                else ("fixer_changed_code" if changed else "fixer_noop_failed")
            ),
            "verification_chain_stage_fix_counts": stage_fix_counts,
            "verification_chain_stage_fix_limits": stage_fix_limits,
            "verification_chain_stage_results": stage_results,
            "verification_chain_history": history,
        }

    return fix_verification_issue
