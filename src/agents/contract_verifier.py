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
Check only contract errors in a Lua workflow solution.
Do not judge business logic.
Check:
- correct wf.vars / wf.initVariables paths
- correct return target or update target
- correct top-level result shape
- forbidden patterns: print, io.write, io.read, invented demo tables
Use runtime_result and after_state as primary evidence when present.
Return JSON only.
If passed=false, describe the exact contract mismatch and a minimal patch."""

_WORKFLOW_PATH_RE = re.compile(r"wf\.(?:vars|initVariables)(?:\.[A-Za-z_][A-Za-z0-9_]*)+")
_WORKFLOW_ROOT_RE = re.compile(r"\bwf\.(?:vars|initVariables)\b")
_RETURN_WHOLE_WORKFLOW_RE = re.compile(
    r"(?im)^\s*return\s+\(?\s*(wf\.(?:vars|initVariables))\s*\)?\s*$"
)
_LOCAL_TABLE_ASSIGN_RE = re.compile(r"(?im)^\s*local\s+([A-Za-z_][A-Za-z0-9_]*)\s*=\s*\{")
_FORBIDDEN_CALL_PATTERNS = (
    ("print", re.compile(r"(?<![\w.])print\s*\(")),
    ("io.write", re.compile(r"\bio\.write\s*\(")),
    ("io.read", re.compile(r"\bio\.read\s*\(")),
)

_VALID_SEVERITIES = frozenset({"low", "medium", "high", "critical"})
_VALID_PATCH_SCOPES = frozenset({"none", "local", "function_level", "multi_block", "rewrite"})


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


def _ensure_string_list(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item).strip() for item in value if str(item).strip()]


def _extract_workflow_paths(text: str) -> list[str]:
    if not text:
        return []
    return _unique_strings(_WORKFLOW_PATH_RE.findall(text))


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
        rendered = json.dumps(value, ensure_ascii=False, indent=2)
    except Exception:
        rendered = str(value)
    if len(rendered) <= limit:
        return rendered
    return rendered[: limit - 3].rstrip() + "..."


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

    sections = [
        f"Task:\n{task}" if task else "",
        "Scope:\nCheck contract mismatches only. Ignore business-logic quality and deep semantic correctness.",
        "Expected contract:\n" + "\n".join(expected_lines) if expected_lines else "",
        (
            "Workflow context:\n" + _compact_json(payload.get("parsed_context"))
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
        f"Lua solution under review:\n```lua\n{code}\n```",
        "Return strict JSON in this exact shape:\n" + json.dumps(_OUTPUT_SHAPE_EXAMPLE, ensure_ascii=False, indent=2),
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
    expected_paths = _unique_strings(
        _ensure_string_list(compiled_request.get("expected_workflow_paths"))
        + ([selected_primary_path] if selected_primary_path else [])
        + _ensure_string_list(planner_result.get("identified_workflow_paths"))
        + _extract_workflow_paths(task)
    )

    expected_result_action = (
        str(state.get("expected_result_action", "") or "").strip()
        or str(planner_result.get("expected_result_action", "") or "").strip()
        or str(compiled_request.get("expected_result_action", "") or "").strip()
    )

    expected_return_path: str | None = None
    expected_update_path: str | None = None
    if expected_result_action == "return" and selected_primary_path:
        expected_return_path = selected_primary_path
    if expected_result_action == "save_to_wf_vars" and selected_primary_path:
        expected_update_path = selected_primary_path

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
        "after_state": diagnostics.get("workflow_state"),
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
    """LLM-backed contract-only verifier with local hard blockers."""

    def __init__(self, llm: LLMProvider) -> None:
        self._llm = llm

    @property
    def name(self) -> str:
        return _AGENT_NAME

    async def verify(self, payload: ContractVerifierInput) -> ContractVerifierResult:
        code = str(payload.get("code", "") or "")
        expected_paths = _unique_strings(_ensure_string_list(payload.get("expected_workflow_paths")))

        logger.info(
            f"[{_AGENT_NAME}] started",
            code_len=len(code),
            expected_path_count=len(expected_paths),
            has_runtime_result=payload.get("runtime_result") is not None,
            has_after_state=payload.get("after_state") is not None,
        )

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
    """Factory for future verification-pipeline wiring."""
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
