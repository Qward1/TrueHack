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
  "fixed_lua_code": "lua{...}lua"
}

Rules:
- fixed_lua_code must always be valid lua{...}lua
- if verifier_result.passed=true, keep code unchanged
- if verifier_result.passed=false, patch exactly that reported error
- preserve the stated working parts
- never use forbidden_fixes
- if no semantic change was possible, return changed=false"""

_VALID_PATCH_SCOPES = {"none", "local", "function_level", "multi_block", "rewrite"}
_ZERO_WIDTH_RE = re.compile(r"[\u200b-\u200d\ufeff]")
_FENCED_RE = re.compile(r"^```(?:[A-Za-z0-9_-]+)?\s*([\s\S]*?)```$", re.IGNORECASE)
_LOWCODE_WRAPPER_RE = re.compile(r"^lua\{\s*([\s\S]*?)\s*\}lua$", re.IGNORECASE)


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


def _ensure_object(value: object) -> dict[str, Any]:
    return dict(value) if isinstance(value, dict) else {}


def _ensure_list(value: object) -> list[Any]:
    return list(value) if isinstance(value, list) else []


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
    fixed_lua_code = _wrap_lua_code(str(data.get("fixed_lua_code", "") or original_code))
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
    verifier_result = _normalize_verifier_result(payload.get("verifier_result"))
    previous_attempts = _normalize_previous_fix_attempts(payload.get("previous_fix_attempts"))

    sections = [
        f"Task:\n{task}" if task else "",
        "Current broken Lua code:\n" + _wrap_lua_code(code),
        "Verifier result (source of truth):\n" + _compact_json(verifier_result),
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
        (
            "Workflow context:\n" + _compact_json(payload.get("workflow_context"))
            if payload.get("workflow_context") is not None
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
        "previous_fix_attempts:\n" + _render_attempts(previous_attempts),
        (
            "Return JSON only with fields fixed, changed, applied_error_family, applied_error_code, "
            "applied_strategy, preserved_constraints, remaining_risks, fixed_lua_code."
        ),
    ]
    return "\n\n".join(section for section in sections if section)


def _select_verifier_result_from_state(state: dict[str, Any]) -> object:
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

    runtime_result = diagnostics.get("result_value")
    if runtime_result is None:
        preview = diagnostics.get("result_preview")
        if preview not in (None, ""):
            runtime_result = preview

    parsed_context = compiled_request.get("parsed_context")
    before_state = parsed_context if compiled_request.get("has_parseable_context") else parsed_context

    return {
        "task": task,
        "code": str(state.get("generated_code", "") or state.get("current_code", "") or ""),
        "workflow_context": parsed_context,
        "before_state": before_state,
        "after_state": diagnostics.get("workflow_state"),
        "runtime_result": runtime_result,
        "verifier_result": _select_verifier_result_from_state(state),
        "previous_fix_attempts": _ensure_list(state.get("previous_fix_attempts")),
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
        fix_iter = int(state.get("fix_verification_iterations", 0) or 0)
        previous_attempts = _normalize_previous_fix_attempts(state.get("previous_fix_attempts"))

        result = await agent.fix(payload)
        fixed_code = _normalize_code_for_storage(result["fixed_lua_code"])

        new_attempt = {
            "verifier_name": verifier_result["verifier_name"],
            "error_code": result["applied_error_code"],
            "strategy": result["applied_strategy"],
            "changed": result["changed"],
        }
        updated_attempts = previous_attempts + [new_attempt]

        return {
            "generated_code": fixed_code,
            "universal_verification_fixer_result": result,
            "fix_verification_iterations": fix_iter if verifier_result["passed"] else fix_iter + 1,
            "failure_stage": "" if (verifier_result["passed"] or result["changed"]) else "verification_fix",
            "validation_passed": True,
            "verification_passed": bool(verifier_result["passed"]),
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
        }

    return fix_verification_issue
