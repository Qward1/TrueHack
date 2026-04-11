"""LangGraph node functions for the canonical Lua generation pipeline."""

from __future__ import annotations

import os
import re
from typing import Any, Callable

import structlog

from src.core.llm import LLMProvider
from src.core.state import PipelineState
from src.tools.lua_tools import (
    LOWCODE_CONTRACT_TEXT,
    LOWCODE_LUA_VERSION,
    LOWCODE_RESPONSE_FORMAT_REQUIREMENT,
    async_run_diagnostics,
    async_verify_requirements,
    compile_lowcode_request,
    extract_function_names,
    format_lowcode_json_payload,
    format_lowcode_jsonstring,
    inspect_lowcode_request_alignment,
    restore_lost_functions,
    smart_normalize,
    validate_lowcode_llm_output,
    validate_lua_response,
)
from src.tools.target_tools import (
    load_target_code,
    resolve_lua_target,
    save_final_output,
)

logger = structlog.get_logger(__name__)

# ── Agent names ─────────────────────────────────────────────────────────────
_AGENT_RESOLVE_TARGET = "TargetResolver"
_AGENT_ROUTE_INTENT = "IntentRouter"
_AGENT_GENERATE_CODE = "CodeGenerator"
_AGENT_REFINE_CODE = "CodeRefiner"
_AGENT_VALIDATE_CODE = "CodeValidator"
_AGENT_FIX_CODE = "CodeFixer"
_AGENT_VERIFY_REQUIREMENTS = "RequirementsVerifier"
_AGENT_GENERATE_E2E = "E2ESuiteBuilder"
_AGENT_RUN_E2E = "E2ERunner"
_AGENT_SAVE_CODE = "CodeSaver"
_AGENT_EXPLAIN = "SolutionExplainer"
_AGENT_ANSWER = "QuestionAnswerer"
_AGENT_PREPARE_RESPONSE = "ResponseAssembler"

_ROUTE_SYSTEM = (
    "You are an intent classifier for a Lua code assistant. "
    "Classify the user message into exactly one category. "
    'Return JSON only: {"intent": "<category>", "confidence": <float>}.'
)

_ROUTE_USER = """Categories:
- create: user wants NEW Lua code
- change: user wants to MODIFY / IMPROVE / FIX existing code
- inspect: user asks to explain or review existing code
- question: user asks about Lua / programming without requesting code changes
- general: greeting or casual text
- retry: rerun validation/fix for already available code

Decision rules:
1. Use only one of the allowed intents: {allowed_intents}
2. "change" is valid only when there is code to modify in chat or pasted in the current message
3. If no code is available yet, requests like "fix/improve/wrap/clean/convert" still mean create new code, not change
4. "inspect" is valid only when there is code to inspect in chat or pasted in the current message
5. If the user pasted Lua code in this message, treat it as available code context

Current code in chat: {has_code}
Lua code pasted in current message: {has_message_code}
Effective code available for modification: {effective_has_code}
User message: {user_input}

JSON only:"""

_GENERATE_SYSTEM = (
    f"You write {LOWCODE_LUA_VERSION} workflow scripts for varied automation tasks. "
    "Return ONLY Lua code.\n"
    f"{LOWCODE_RESPONSE_FORMAT_REQUIREMENT}\n"
    f"{LOWCODE_CONTRACT_TEXT}"
    "Synthesize the script from the current task and workflow context instead of copying canned patterns. "
    "Simple tasks may stay short, but when the task needs normalization, validation, iteration, or multi-step transformation, write the full logic explicitly. "
    "Prefer correctness, robustness, and direct workflow-path usage over terseness."
)

_REFINE_SYSTEM = (
    "You modify existing Lua workflow scripts according to the user's request. "
    "Return ONLY the complete updated script. "
    f"{LOWCODE_RESPONSE_FORMAT_REQUIREMENT} "
    "Preserve existing functions unless explicitly asked to remove them. "
    f"{LOWCODE_CONTRACT_TEXT}"
    "Keep useful existing structure, but expand the script when the new behavior needs additional guards, loops, helpers, or multi-step transformation logic."
)

_FIX_SYSTEM = (
    "You fix broken Lua workflow scripts. "
    "Return ONLY the corrected code.\n"
    f"{LOWCODE_RESPONSE_FORMAT_REQUIREMENT}\n"
    f"{LOWCODE_CONTRACT_TEXT}"
    "Do not convert the script into a console app. Do not add print/io.read/menus. "
    "Fix the actual workflow logic; if the task needs a longer multi-step script, write it instead of forcing a short return-only answer."
)

_ANSWER_SYSTEM = (
    "You are a helpful Lua programming assistant. "
    "Answer in the same language as the user's message."
)

_EXPLAIN_SYSTEM = (
    "You explain generated Lua code for the user. "
    "Return strict JSON only with keys: summary, what_is_in_code, how_it_works, "
    "suggested_changes, clarifying_questions. "
    "Keep suggested_changes and clarifying_questions short lists (1-3 items each). "
    "IMPORTANT: write all text values in the same language as the user_request field "
    "(if the request is in Russian — answer in Russian; if in English — answer in English)."
)

_FENCED_BLOCK_RE = re.compile(r"```(?:[A-Za-z0-9_-]+)?\s*([\s\S]*?)```")
_TRAILING_JSON_CONTEXT_RE = re.compile(
    r"(?s)^(?P<task>.*?)(?:\n\s*)(?P<context>(?:\{[\s\S]*\}|\[[\s\S]*\]))\s*$"
)
_INLINE_LOWCODE_BLOCK_RE = re.compile(r"lua\{\s*[\s\S]*?\s*\}lua", re.IGNORECASE)
_FENCED_CODE_BLOCK_RE = re.compile(r"```(?:lua)?\s*([\s\S]*?)```", re.IGNORECASE)
_INTENT_CHANGE_MARKERS = (
    "исправ",
    "поправ",
    "почин",
    "улучш",
    "доработ",
    "передел",
    "измени",
    "обнови",
    "адаптир",
    "оптимиз",
    "refactor",
    "modify",
    "improve",
    "fix",
    "update",
    "change",
    "adjust",
)
_INTENT_CREATE_MARKERS = (
    "сделай",
    "создай",
    "напиши",
    "сгенер",
    "реализ",
    "нужен скрипт",
    "build",
    "create",
    "generate",
    "write",
    "implement",
)
_INTENT_INSPECT_MARKERS = (
    "объясн",
    "обьясн",
    "как работает",
    "что делает",
    "разбер",
    "проанализ",
    "ревью",
    "review",
    "inspect",
    "explain",
    "analyze",
)
_INTENT_RETRY_MARKERS = (
    "/retry",
    "retry",
    "повтори",
    "перезапусти",
    "прогони еще раз",
    "заново проверь",
)
_INTENT_QUESTION_MARKERS = (
    "почему",
    "зачем",
    "как ",
    "что ",
    "what ",
    "why ",
    "how ",
)
_INTENT_GENERAL_MARKERS = (
    "привет",
    "здравствуй",
    "hello",
    "hi",
    "thanks",
    "спасибо",
)
_TASK_CODE_MARKERS = (
    "скрипт",
    "код",
    "lua",
    "workflow",
    "wf.vars",
    "wf.initvariables",
    "jsonstring",
    "script",
    "code",
)
_PROMPT_STYLE_RULES = """Hard rules:
- Use the exact wf.vars / wf.initVariables paths from the task or provided workflow context.
- Do not recreate the provided input as local demo tables like local data = {...}, local users = {...}, local emails = {...}.
- Do not build an app, service, API, menu, tutorial, or CLI wrapper.
- Do not use print(), io.write(), io.read(), or console prompts.
- Use the amount of structure the task actually needs: locals, conditions, loops, table traversal, normalization steps, and helper functions are all allowed when they are necessary.
- Do not force a non-trivial workflow transformation into a one-line `return`.
- If the task combines or derives data from several workflow fields, read each required field directly from wf.vars / wf.initVariables.
- If the task asks to remove/clear/filter keys inside workflow objects, transform those objects before return; do not return the source path unchanged.
- If the task explicitly asks to save/update wf.vars, use the exact workflow paths and keep the script focused on the requested state change."""
_PROMPT_SYNTHESIS_GUIDANCE = """Generation guidance:
- Derive the script from the current task, normalized workflow inventory, and pasted workflow context.
- Treat the selected primary path as a strong anchor, not as a restriction when the task clearly needs sibling fields or nested related fields.
- Decide the real output/update shape first, then write the Lua needed to produce it.
- Prefer direct workflow data access and small local helpers over invented demo inputs or generic boilerplate.
- If the task is shape-sensitive, treat an array as a table with numeric keys 1..n without gaps. A table with string keys like `name` or `phone` is an object, not an array. Treat an empty table as an array.
- If the task is shape-sensitive, explicitly distinguish object-like tables from array-like tables with numeric keys instead of relying only on `type(x) == "table"`, `next(x)`, or empty/non-empty checks.
- If you need a new array, create it with `_utils.array.new()`, assign items explicitly, then call `_utils.array.markAsArray(arr)` before return/store.
- For numeric aggregation over workflow arrays, iterate the items explicitly and convert number-like string values with `tonumber(...)` or guard nils before arithmetic.
- Keep simple tasks simple, but let complex tasks stay multi-step and explicit."""


def _generation_temperature(compiled_request: dict[str, Any]) -> float:
    if not isinstance(compiled_request, dict):
        return 0.1
    if compiled_request.get("has_parseable_context"):
        return 0.0
    semantic_expectations = [
        str(item).strip()
        for item in compiled_request.get("semantic_expectations", [])
        if str(item).strip()
    ]
    if semantic_expectations:
        return 0.0
    return 0.1


def _target_context(state: PipelineState) -> str:
    target_path = state.get("target_path", "")
    if not target_path:
        return ""
    try:
        relative = os.path.relpath(target_path, state.get("workspace_root", "") or os.getcwd())
    except ValueError:
        relative = target_path
    return f"Primary target Lua file: {relative}"


def _normalize_string_list(value: object, limit: int = 3) -> list[str]:
    if not isinstance(value, list):
        return []
    normalized = [str(item).strip() for item in value if str(item).strip()]
    return normalized[:limit]


def _format_values_for_prompt(values: object, fallback: str = "none") -> str:
    if isinstance(values, list):
        cleaned = [str(value).strip() for value in values if str(value).strip()]
        return ", ".join(cleaned) if cleaned else fallback
    text = str(values or "").strip()
    return text or fallback


def _format_compiled_request_summary(compiled_request: dict[str, Any]) -> str:
    if not isinstance(compiled_request, dict):
        return ""

    lines: list[str] = []
    operation = str(compiled_request.get("selected_operation", "") or "").strip() or "llm"
    selected_path = str(compiled_request.get("selected_primary_path", "") or "").strip() or "none"
    confidence = float(compiled_request.get("confidence", 0.0) or 0.0)
    lines.append("Compiled workflow request:")
    lines.append(f"- selected operation: {operation}")
    lines.append(f"- selected primary path: {selected_path}")
    lines.append(f"- confidence: {confidence:.2f}")
    requested_item_keys = [
        str(key).strip()
        for key in compiled_request.get("requested_item_keys", [])
        if str(key).strip()
    ]
    if requested_item_keys:
        lines.append(f"- requested item keys: {', '.join(requested_item_keys)}")
    semantic_expectations = [
        str(item).strip()
        for item in compiled_request.get("semantic_expectations", [])
        if str(item).strip()
    ]
    if semantic_expectations:
        lines.append(f"- semantic expectations: {', '.join(semantic_expectations)}")
    inferred_explicit_paths = [
        str(path).strip()
        for path in compiled_request.get("inferred_explicit_paths", [])
        if str(path).strip()
    ]
    if inferred_explicit_paths:
        lines.append(f"- inferred explicit paths: {', '.join(inferred_explicit_paths)}")

    inventory = compiled_request.get("workflow_path_inventory", [])
    if isinstance(inventory, list) and inventory:
        lines.append("- available workflow paths:")
        for entry in inventory[:8]:
            path = str(entry.get("path", "")).strip()
            path_type = str(entry.get("type", "")).strip()
            sample = str(entry.get("sample_preview", "")).strip()
            item_keys = _format_values_for_prompt(entry.get("item_keys", []), fallback="")
            suffix = f" sample={sample}" if sample else ""
            if item_keys:
                suffix = f"{suffix} item_keys={item_keys}".strip()
                suffix = f" {suffix}" if suffix else ""
            lines.append(f"  - {path} [{path_type}]{suffix}")

    candidates = compiled_request.get("candidate_paths_ranked", [])
    if isinstance(candidates, list) and candidates:
        lines.append("- ranked candidates:")
        for candidate in candidates[:3]:
            path = str(candidate.get("path", "")).strip()
            path_type = str(candidate.get("type", "")).strip()
            score = float(candidate.get("score", 0.0) or 0.0)
            lines.append(f"  - {path} [{path_type}] score={score:.1f}")

    return "\n".join(lines).strip()


def _format_prompt_workflow_context(compiled_request: dict[str, Any]) -> str:
    if not isinstance(compiled_request, dict):
        return ""

    lines: list[str] = []
    selected_path = str(compiled_request.get("selected_primary_path", "") or "").strip()
    selected_type = str(compiled_request.get("selected_primary_type", "") or "").strip()
    semantic_expectations = [
        str(item).strip()
        for item in compiled_request.get("semantic_expectations", [])
        if str(item).strip()
    ]
    requested_item_keys = [
        str(key).strip()
        for key in compiled_request.get("requested_item_keys", [])
        if str(key).strip()
    ]

    if selected_path:
        lines.append(f"Use workflow path: {selected_path}")
    if selected_type:
        lines.append(f"Path type: {selected_type}")
    if semantic_expectations:
        lines.append(f"Semantic expectations: {', '.join(semantic_expectations)}")
    if requested_item_keys:
        lines.append(f"Requested item keys: {', '.join(requested_item_keys)}")
    return "\n".join(lines).strip()


def _build_clarification_response(compiled_request: dict[str, Any]) -> str:
    question = str(compiled_request.get("clarifying_question", "") or "").strip()
    if question:
        return question
    return "Нужна явная привязка к workflow-пути. Ответь, например: `используй wf.vars.some.path`."


def _normalize_intent_text(text: str) -> str:
    return re.sub(r"\s+", " ", str(text or "").strip().lower())


def _text_has_any(text: str, markers: tuple[str, ...]) -> bool:
    return any(marker in text for marker in markers)


def _extract_message_code_block(user_input: str) -> str:
    raw = str(user_input or "")
    candidates: list[str] = []

    for match in _INLINE_LOWCODE_BLOCK_RE.finditer(raw):
        wrapped = match.group(0).strip()
        if wrapped:
            candidates.append(wrapped)

    for block in _FENCED_CODE_BLOCK_RE.findall(raw):
        candidate = str(block or "").strip()
        if candidate:
            candidates.append(candidate)

    stripped = raw.strip()
    if not candidates and "\n" in stripped:
        code_markers = ("wf.vars", "wf.initVariables", "local ", "function ", "return ", " end", "=")
        if any(marker in stripped for marker in code_markers):
            candidates.append(stripped)

    for candidate in candidates:
        analysis = validate_lua_response(candidate)
        normalized = str(analysis.get("normalized", "") or "").strip()
        if not analysis.get("valid") or not normalized:
            continue
        if "\n" not in normalized and not _INLINE_LOWCODE_BLOCK_RE.search(candidate) and "wf." not in normalized:
            continue
        return normalized

    return ""


def _strip_message_code_blocks(user_input: str) -> str:
    cleaned = _INLINE_LOWCODE_BLOCK_RE.sub("\n", str(user_input or ""))
    cleaned = _FENCED_CODE_BLOCK_RE.sub("\n", cleaned)
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned)
    return cleaned.strip()


def _collect_intent_features(
    *,
    user_input: str,
    has_existing_code: bool,
    base_prompt: str,
) -> dict[str, Any]:
    message_code = _extract_message_code_block(user_input)
    cleaned_message = _strip_message_code_blocks(user_input) if message_code else str(user_input or "").strip()
    normalized = _normalize_intent_text(cleaned_message or user_input)
    task_text, raw_context = split_task_and_context(cleaned_message)

    has_workflow_context = bool(raw_context.strip()) or any(
        marker in user_input for marker in ('"wf"', "wf.vars", "wf.initVariables")
    )
    has_change_signal = _text_has_any(normalized, _INTENT_CHANGE_MARKERS)
    has_create_signal = _text_has_any(normalized, _INTENT_CREATE_MARKERS)
    has_inspect_signal = _text_has_any(normalized, _INTENT_INSPECT_MARKERS)
    has_retry_signal = _text_has_any(normalized, _INTENT_RETRY_MARKERS)
    has_question_signal = user_input.strip().endswith("?") or _text_has_any(normalized, _INTENT_QUESTION_MARKERS)
    has_general_signal = _text_has_any(normalized, _INTENT_GENERAL_MARKERS)
    has_error_signal = any(
        marker in user_input.lower()
        for marker in ("traceback", "runtime error", "syntax error", "stack traceback", "attempt to ", "bad argument")
    )
    has_task_code_signal = has_workflow_context or _text_has_any(normalized, _TASK_CODE_MARKERS)

    effective_has_code = bool(has_existing_code or message_code)
    has_pending_base_prompt = (
        bool(str(base_prompt or "").strip())
        and not effective_has_code
        and str(base_prompt or "").strip() != str(user_input or "").strip()
    )

    return {
        "message_code": message_code,
        "cleaned_message": cleaned_message,
        "normalized": normalized,
        "task_text": task_text,
        "raw_context": raw_context,
        "has_workflow_context": has_workflow_context,
        "has_change_signal": has_change_signal,
        "has_create_signal": has_create_signal,
        "has_inspect_signal": has_inspect_signal,
        "has_retry_signal": has_retry_signal,
        "has_question_signal": has_question_signal,
        "has_general_signal": has_general_signal,
        "has_error_signal": has_error_signal,
        "has_task_code_signal": has_task_code_signal,
        "has_existing_code": has_existing_code,
        "has_message_code": bool(message_code),
        "effective_has_code": effective_has_code,
        "has_pending_base_prompt": has_pending_base_prompt,
    }


def _deterministic_intent_from_features(features: dict[str, Any]) -> tuple[str, str]:
    if features.get("has_pending_base_prompt"):
        return "create", "clarification_followup"

    normalized = str(features.get("normalized", "") or "").strip()
    if not normalized:
        return "general", "empty_message"

    effective_has_code = bool(features.get("effective_has_code"))
    has_retry_signal = bool(features.get("has_retry_signal"))
    has_change_signal = bool(features.get("has_change_signal"))
    has_create_signal = bool(features.get("has_create_signal"))
    has_inspect_signal = bool(features.get("has_inspect_signal"))
    has_question_signal = bool(features.get("has_question_signal"))
    has_general_signal = bool(features.get("has_general_signal"))
    has_error_signal = bool(features.get("has_error_signal"))
    has_task_code_signal = bool(features.get("has_task_code_signal"))
    is_code_task = bool(
        has_create_signal
        or has_change_signal
        or has_error_signal
        or bool(features.get("has_workflow_context"))
        or (has_task_code_signal and not has_question_signal and not has_inspect_signal)
    )

    if has_retry_signal:
        return ("retry", "retry_with_code") if effective_has_code else ("create", "retry_without_code")

    if has_inspect_signal and not (has_change_signal or has_create_signal or has_task_code_signal):
        return ("inspect", "inspect_existing_code") if effective_has_code else ("question", "inspect_without_code")

    if not effective_has_code:
        if has_question_signal and not is_code_task:
            return "question", "question_without_code"
        if is_code_task:
            return "create", "no_code_available"
        if has_general_signal:
            return "general", "general_message"
        return "", ""

    if has_error_signal or has_change_signal:
        return "change", "modify_existing_code"

    if has_inspect_signal and not has_create_signal:
        return "inspect", "inspect_existing_code"

    if has_create_signal and not has_change_signal:
        return "create", "explicit_new_code_request"

    if has_question_signal and not is_code_task:
        return "question", "question_with_code_context"

    if has_general_signal and not is_code_task:
        return "general", "general_message"

    return "", ""


def split_task_and_context(user_input: str) -> tuple[str, str]:
    """Split a user message into task text and pasted workflow context."""
    cleaned = str(user_input or "").strip()
    if not cleaned:
        return "", ""

    fenced_blocks = [
        block.strip()
        for block in _FENCED_BLOCK_RE.findall(cleaned)
        if block and block.strip()
    ]
    if fenced_blocks:
        task = _FENCED_BLOCK_RE.sub("\n", cleaned)
        task = re.sub(r"\n{3,}", "\n\n", task).strip()
        return task or cleaned, "\n\n".join(fenced_blocks)

    trailing_context = _TRAILING_JSON_CONTEXT_RE.match(cleaned)
    if trailing_context:
        task = str(trailing_context.group("task") or "").strip()
        context = str(trailing_context.group("context") or "").strip()
        if task and context and (context.count("\n") >= 1 or ":" in context):
            return task, context

    return cleaned, ""


def _join_prompt_sections(*sections: str) -> str:
    return "\n\n".join(section.strip() for section in sections if section and section.strip())


def _code_signature(code: str) -> str:
    return re.sub(r"\s+", "", str(code or ""))


def _assess_fix_candidate(
    *,
    original_code: str,
    candidate_code: str,
    failure_stage: str,
    diagnostics: dict[str, Any],
    verification: dict[str, Any],
    compiled_request: dict[str, Any],
    verification_prompt: str,
) -> list[str]:
    reasons: list[str] = []
    candidate = str(candidate_code or "").strip()
    if not candidate:
        return ["The fix attempt returned empty code."]

    if _code_signature(candidate) == _code_signature(original_code):
        reasons.append("The fix attempt did not materially change the code.")

    analysis = validate_lua_response(candidate)
    if not analysis.get("valid"):
        reasons.append(str(analysis.get("reason", "") or "The fix attempt does not look like a valid standalone Lua file."))

    if failure_stage == "requirements":
        alignment = inspect_lowcode_request_alignment(
            verification_prompt,
            candidate,
            compiled_request=compiled_request if isinstance(compiled_request, dict) else None,
        )
        remaining = [
            str(item).strip()
            for item in alignment.get("missing_requirements", [])
            if str(item).strip()
        ]
        previous = {
            str(item).strip()
            for item in verification.get("missing_requirements", [])
            if str(item).strip()
        }
        repeated = [item for item in remaining if item in previous]
        if repeated:
            reasons.append(
                "The fix still violates the same deterministic requirements: " + ", ".join(repeated)
            )
        elif remaining:
            reasons.append(
                "The fix still fails deterministic requirement checks: " + ", ".join(remaining)
            )

    if failure_stage == "validation" and diagnostics.get("run_error"):
        runtime_hints = [
            str(item).strip()
            for item in diagnostics.get("runtime_fix_hints", [])
            if str(item).strip()
        ]
        if runtime_hints and _code_signature(candidate) == _code_signature(original_code):
            reasons.append("The fix did not address the runtime diagnostics or runtime fix hints.")

    deduped: list[str] = []
    seen: set[str] = set()
    for reason in reasons:
        if reason not in seen:
            seen.add(reason)
            deduped.append(reason)
    return deduped


def _build_generation_prompt(compiled_request: dict[str, Any], target_context: str = "") -> str:
    task = str(compiled_request.get("task_text", "") or "").strip()
    provided_context = str(compiled_request.get("raw_context", "") or "").strip()
    clarification_text = str(compiled_request.get("clarification_text", "") or "").strip()
    prompt_context = _format_prompt_workflow_context(compiled_request)
    sections = [
        f"Task:\n{task}",
        f"Clarification from user:\n{clarification_text}" if clarification_text else "",
        f"Workflow anchor:\n{prompt_context}" if prompt_context else "",
        (
            "Provided workflow context:\n"
            f"{provided_context}"
        ) if provided_context else "",
        _PROMPT_STYLE_RULES,
        _PROMPT_SYNTHESIS_GUIDANCE,
        LOWCODE_RESPONSE_FORMAT_REQUIREMENT,
    ]
    return _join_prompt_sections(*sections)


def _build_refine_prompt(
    *,
    base_prompt: str,
    user_input: str,
    target_path: str,
    function_list: str,
    code: str,
    compiled_request: dict[str, Any],
) -> str:
    original_task, provided_context = split_task_and_context(base_prompt or user_input)
    prompt_context = _format_prompt_workflow_context(compiled_request)
    clarification_text = str(compiled_request.get("clarification_text", "") or "").strip()
    sections = [
        f"Original task:\n{original_task or user_input.strip()}",
        (
            "Original workflow context:\n"
            f"{provided_context}"
        ) if provided_context else "",
        f"Change clarification:\n{clarification_text}" if clarification_text else "",
        f"Workflow anchor:\n{prompt_context}" if prompt_context else "",
        (
            "Existing functions you must preserve unless the user explicitly removes them:\n"
            f"{function_list}"
        ),
        f"Current code:\n{code}",
        f"Change request:\n{user_input}",
        _PROMPT_STYLE_RULES,
        _PROMPT_SYNTHESIS_GUIDANCE,
        LOWCODE_RESPONSE_FORMAT_REQUIREMENT,
    ]
    return _join_prompt_sections(*sections)


def _build_fix_prompt(
    *,
    base_prompt: str,
    target_path: str,
    failure_stage: str,
    failure_kind: str,
    run_error: str,
    run_output: str,
    runtime_fix_hints: object,
    verification_summary: str,
    missing_requirements: object,
    expected_paths: str,
    actual_paths: str,
    anti_patterns: str,
    code: str,
    compiled_request: dict[str, Any],
) -> str:
    original_task, provided_context = split_task_and_context(base_prompt)
    def _items(value: object) -> list[str]:
        if isinstance(value, list):
            return [str(item).strip() for item in value if str(item).strip()]
        text = str(value or "").strip()
        if not text or text.lower() == "none":
            return []
        return [text]

    prompt_context = _format_prompt_workflow_context(compiled_request)
    clarification_text = str(compiled_request.get("clarification_text", "") or "").strip()
    mandatory_fixes: list[str] = []
    if str(run_error or "").strip():
        mandatory_fixes.append(f"Resolve runtime/syntax failure: {run_error}")
    mandatory_fixes.extend(_items(runtime_fix_hints))
    mandatory_fixes.extend(_items(missing_requirements))
    if not mandatory_fixes and failure_stage:
        mandatory_fixes.append(f"Resolve the current {failure_stage} failure.")
    mandatory_fixes = list(dict.fromkeys(mandatory_fixes))
    sections = [
        f"Original task:\n{original_task or base_prompt.strip()}",
        (
            "Original workflow context:\n"
            f"{provided_context}"
        ) if provided_context else "",
        f"Clarification from user:\n{clarification_text}" if clarification_text else "",
        f"Workflow anchor:\n{prompt_context}" if prompt_context else "",
        "Mandatory fixes:\n" + "\n".join(f"- {item}" for item in mandatory_fixes),
        "Rewrite the script from scratch so it satisfies every mandatory fix. Do not reuse the previous broken response pattern.",
        _PROMPT_STYLE_RULES,
        _PROMPT_SYNTHESIS_GUIDANCE,
        "When runtime diagnostics mention argument types, nil accesses, bad calls, arithmetic/type mismatches, or concatenation issues, fix the root cause instead of reusing the same failing API call shape.",
        "If the task is shape-sensitive, a `next(...)` check by itself is not enough to prove that a table is an array.",
        "Return ONLY the complete corrected Lua file.",
        LOWCODE_RESPONSE_FORMAT_REQUIREMENT,
    ]
    return _join_prompt_sections(*sections)


def create_nodes(llm: LLMProvider) -> dict[str, Callable]:
    """Build node callables from a pre-constructed LLM provider."""

    async def resolve_target(state: PipelineState) -> dict:
        logger.info(
            f"[{_AGENT_RESOLVE_TARGET}] started",
            user_input_len=len(state["user_input"]),
            previous_target=state.get("target_path", ""),
            workspace_root=state.get("workspace_root", ""),
        )
        previous_target = os.path.abspath(state.get("target_path", "")) if state.get("target_path") else ""

        logger.info(
            f"[{_AGENT_RESOLVE_TARGET}/resolve_lua_target] calling",
            user_input=state["user_input"][:80],
            workspace_root=state.get("workspace_root", ""),
            current_target_path=previous_target,
            allow_fallback=False,
        )
        resolved = resolve_lua_target(
            state["user_input"],
            workspace_root=state.get("workspace_root", ""),
            current_target_path=previous_target,
            allow_fallback=False,
        )
        target_path = resolved["target_path"]
        logger.info(
            f"[{_AGENT_RESOLVE_TARGET}/resolve_lua_target] done",
            target_path=target_path,
            explicit=resolved["target_explicit"],
        )

        current_code = state.get("current_code", "")
        if target_path:
            same_target = bool(previous_target and previous_target == os.path.abspath(target_path))
            if not same_target or not current_code.strip():
                logger.info(
                    f"[{_AGENT_RESOLVE_TARGET}/load_target_code] calling",
                    target_path=target_path,
                    reason="new_target" if not same_target else "code_missing",
                )
                current_code = load_target_code(target_path)
                logger.info(
                    f"[{_AGENT_RESOLVE_TARGET}/load_target_code] done",
                    code_len=len(current_code),
                )
        elif not previous_target:
            current_code = ""

        logger.info(
            f"[{_AGENT_RESOLVE_TARGET}] completed",
            target_path=target_path,
            explicit=resolved["target_explicit"],
            code_loaded=bool(current_code.strip()),
        )
        return {
            "workspace_root": resolved["workspace_root"],
            "target_path": target_path,
            "target_directory": resolved["target_directory"],
            "target_explicit": resolved["target_explicit"],
            "current_code": current_code,
        }

    async def route_intent(state: PipelineState) -> dict:
        has_code = bool(state.get("current_code", "").strip())
        features = _collect_intent_features(
            user_input=state["user_input"],
            has_existing_code=has_code,
            base_prompt=state.get("base_prompt", ""),
        )
        message_code = str(features.get("message_code", "") or "").strip()
        effective_has_code = bool(features.get("effective_has_code"))
        logger.info(
            f"[{_AGENT_ROUTE_INTENT}] started",
            user_input=state["user_input"][:80],
            has_existing_code=has_code,
            has_message_code=bool(message_code),
            effective_has_code=effective_has_code,
        )
        deterministic_intent, deterministic_reason = _deterministic_intent_from_features(features)

        llm_intent = ""
        confidence = 0.0
        intent_source = "deterministic"
        if deterministic_intent:
            intent = deterministic_intent
        else:
            allowed_intents = ["create", "question", "general"]
            if effective_has_code:
                allowed_intents = ["create", "change", "inspect", "question", "general", "retry"]

            prompt = _ROUTE_USER.format(
                allowed_intents=", ".join(allowed_intents),
                has_code=str(has_code).lower(),
                has_message_code=str(bool(message_code)).lower(),
                effective_has_code=str(effective_has_code).lower(),
                user_input=state["user_input"],
            )

            logger.info(
                f"[{_AGENT_ROUTE_INTENT}/llm.generate_json] calling",
                prompt_len=len(prompt),
                allowed_intents=allowed_intents,
            )
            result = await llm.generate_json(prompt, system=_ROUTE_SYSTEM)
            llm_intent = str(result.get("intent", "") or "").strip()
            confidence = float(result.get("confidence", 0.5))
            logger.info(
                f"[{_AGENT_ROUTE_INTENT}/llm.generate_json] done",
                raw_intent=llm_intent,
                confidence=confidence,
            )

            intent = llm_intent or "create"
            intent_source = "llm"

        valid_intents = {"create", "change", "inspect", "question", "general", "retry"}
        if intent not in valid_intents:
            intent = "change" if effective_has_code else "create"
            intent_source = "fallback_invalid"

        if intent not in {"create", "question", "general"} and not effective_has_code:
            intent = "create"
            intent_source = "fallback_no_code"

        updates: dict[str, Any] = {"intent": intent}
        if message_code and intent in {"change", "inspect", "retry"} and not has_code:
            updates["current_code"] = message_code
            cleaned_message = str(features.get("cleaned_message", "") or "").strip()
            if cleaned_message and not state.get("base_prompt", "").strip():
                updates["base_prompt"] = cleaned_message

        logger.info(
            f"[{_AGENT_ROUTE_INTENT}] completed",
            intent=intent,
            confidence=confidence,
            source=intent_source,
            deterministic_reason=deterministic_reason or "none",
            llm_intent=llm_intent or "n/a",
        )
        return updates

    async def prepare_generation_context(state: PipelineState) -> dict:
        user_input = state.get("user_input", "")
        current_code = state.get("current_code", "")
        existing_base_prompt = state.get("base_prompt", "")
        intent = state.get("intent", "create")

        task_source_prompt = user_input
        clarification_text = ""
        persistent_base_prompt = existing_base_prompt or user_input
        if not current_code.strip() and existing_base_prompt.strip() and existing_base_prompt.strip() != user_input.strip():
            task_source_prompt = existing_base_prompt
            clarification_text = user_input
            persistent_base_prompt = existing_base_prompt

        task_text, raw_context = split_task_and_context(task_source_prompt)
        compiled_request = compile_lowcode_request(
            task_text=task_text or task_source_prompt,
            raw_context=raw_context,
            clarification_text=clarification_text,
            allow_deterministic=not current_code.strip(),
        )
        verification_prompt = task_source_prompt.strip()
        if current_code.strip() and existing_base_prompt.strip():
            verification_prompt = f"{existing_base_prompt.strip()}\n\nChange request:\n{user_input.strip()}"
        if clarification_text.strip():
            verification_prompt = f"{verification_prompt}\n\nClarification:\n{clarification_text.strip()}"
        compiled_request["verification_prompt"] = verification_prompt

        logger.info(
            "[GenerationContextCompiler] compiled",
            intent=intent,
            has_parseable_context=compiled_request.get("has_parseable_context", False),
            selected_operation=compiled_request.get("selected_operation", ""),
            selected_primary_path=compiled_request.get("selected_primary_path", ""),
            needs_clarification=compiled_request.get("needs_clarification", False),
            generation_mode="model_driven",
        )

        if compiled_request.get("needs_clarification", False):
            response = _build_clarification_response(compiled_request)
            return {
                "base_prompt": persistent_base_prompt,
                "compiled_request": compiled_request,
                "response": response,
                "response_type": "text",
                "clarifying_questions": [response],
                "failure_stage": "clarification",
                "verification": {},
                "verification_passed": False,
                "save_success": False,
                "save_skipped": False,
                "save_skip_reason": "",
                "save_error": "",
            }

        return {
            "base_prompt": persistent_base_prompt,
            "compiled_request": compiled_request,
            "response": "",
            "response_type": "text",
            "clarifying_questions": [],
            "failure_stage": "",
        }

    async def generate_code(state: PipelineState) -> dict:
        user_input = state["user_input"]
        base_prompt = state.get("base_prompt", "") or user_input
        target_path = state.get("target_path", "")
        compiled_request = state.get("compiled_request", {})
        target_directory = state.get("target_directory", state.get("workspace_root", ""))
        target_explicit = state.get("target_explicit", False)
        compiled_request = state.get("compiled_request", {})

        logger.info(
            f"[{_AGENT_GENERATE_CODE}] started",
            user_input=user_input[:80],
            base_prompt_len=len(base_prompt),
            target_path=target_path,
        )

        target_context = _target_context(
            {
                **state,
                "target_path": target_path,
                "target_directory": target_directory,
                "target_explicit": target_explicit,
            }
        )
        prompt = _build_generation_prompt(compiled_request, target_context=target_context)
        generation_temperature = _generation_temperature(compiled_request if isinstance(compiled_request, dict) else {})

        logger.info(
            f"[{_AGENT_GENERATE_CODE}/llm.generate] calling",
            prompt_len=len(prompt),
            target_path=target_path,
            temperature=generation_temperature,
        )
        raw = await llm.generate(prompt=prompt, system=_GENERATE_SYSTEM, temperature=generation_temperature)
        analysis = validate_lowcode_llm_output(raw)
        code = analysis["normalized"]
        logger.info(
            f"[{_AGENT_GENERATE_CODE}/llm.generate] done",
            raw_len=len(raw),
            valid=analysis["valid"],
            reason=analysis.get("reason", ""),
        )

        if not analysis["valid"]:
            logger.warning(
                f"[{_AGENT_GENERATE_CODE}/llm.generate] response not valid Lua, retrying",
                reason=analysis["reason"],
            )
            strict_prompt = (
                f"{prompt}\n\n"
                f"Previous response issue: {analysis['reason']}\n"
                f"{LOWCODE_RESPONSE_FORMAT_REQUIREMENT}"
            )
            strict_system = _GENERATE_SYSTEM
            logger.info(
                f"[{_AGENT_GENERATE_CODE}/llm.generate] retry calling",
                temperature=0.0,
            )
            raw_retry = await llm.generate(
                prompt=strict_prompt,
                system=strict_system,
                temperature=0.0,
            )
            retry_analysis = validate_lowcode_llm_output(raw_retry)
            code = retry_analysis["normalized"]
            logger.info(
                f"[{_AGENT_GENERATE_CODE}/llm.generate] retry done",
                code_len=len(code),
                valid=retry_analysis["valid"],
                reason=retry_analysis.get("reason", ""),
            )

        if not code:
            code = smart_normalize(raw)

        logger.info(
            f"[{_AGENT_GENERATE_CODE}] completed",
            code_len=len(code),
            target_path=target_path,
        )
        return {
            "generated_code": code,
            "base_prompt": base_prompt,
            "compiled_request": compiled_request,
            "fix_iterations": 0,
            "target_path": target_path,
            "target_directory": target_directory,
            "target_explicit": target_explicit,
            "failure_stage": "",
            "verification": {},
            "verification_passed": False,
            "e2e_suite": {},
            "e2e_results": {"summary": "E2E-проверка временно отключена."},
            "e2e_passed": False,
            "save_success": False,
            "save_skipped": False,
            "save_skip_reason": "",
            "save_error": "",
            "saved_to": "",
            "saved_jsonstring_to": "",
            "explanation": {},
            "suggested_changes": [],
            "clarifying_questions": [],
        }

    async def refine_code(state: PipelineState) -> dict:
        existing = state.get("current_code", "")
        user_input = state["user_input"]
        target_path = state.get("target_path", "(not set)")
        compiled_request = state.get("compiled_request", {})

        logger.info(
            f"[{_AGENT_REFINE_CODE}] started",
            user_input=user_input[:80],
            existing_code_len=len(existing),
            target_path=target_path,
        )

        if not existing.strip():
            logger.warning(
                f"[{_AGENT_REFINE_CODE}] no existing code — falling back to generate_code",
            )
            return await generate_code(state)

        logger.info(
            f"[{_AGENT_REFINE_CODE}/extract_function_names] calling",
            code_len=len(existing),
        )
        func_names = extract_function_names(existing)
        logger.info(
            f"[{_AGENT_REFINE_CODE}/extract_function_names] done",
            function_count=len(func_names),
            functions=func_names,
        )

        func_list = "\n".join(f"  - {name}" for name in func_names) or "  (none)"
        prompt = _build_refine_prompt(
            base_prompt=state.get("base_prompt", "") or user_input,
            user_input=user_input,
            target_path=target_path,
            function_list=func_list,
            code=existing,
            compiled_request=compiled_request if isinstance(compiled_request, dict) else {},
        )
        refine_temperature = _generation_temperature(compiled_request if isinstance(compiled_request, dict) else {})

        logger.info(
            f"[{_AGENT_REFINE_CODE}/llm.generate] calling",
            prompt_len=len(prompt),
            target_path=target_path,
            temperature=refine_temperature,
        )
        raw = await llm.generate(prompt=prompt, system=_REFINE_SYSTEM, temperature=refine_temperature)
        analysis = validate_lowcode_llm_output(raw)
        code = analysis["normalized"]
        logger.info(
            f"[{_AGENT_REFINE_CODE}/llm.generate] done",
            raw_len=len(raw),
            normalized_len=len(code),
            valid=analysis["valid"],
            reason=analysis.get("reason", ""),
        )
        if not code:
            code = existing

        logger.info(
            f"[{_AGENT_REFINE_CODE}/restore_lost_functions] calling",
            original_len=len(existing),
            refined_len=len(code),
        )
        code, restored = restore_lost_functions(existing, code, user_input)
        if restored:
            logger.info(
                f"[{_AGENT_REFINE_CODE}/restore_lost_functions] functions restored",
                restored=restored,
            )
        else:
            logger.info(f"[{_AGENT_REFINE_CODE}/restore_lost_functions] done, no functions lost")

        changes = list(state.get("change_requests") or [])
        changes.append(user_input)

        logger.info(
            f"[{_AGENT_REFINE_CODE}] completed",
            code_len=len(code),
            target_path=target_path,
            total_change_requests=len(changes),
        )
        return {
            "generated_code": code,
            "change_requests": changes,
            "compiled_request": compiled_request,
            "failure_stage": "",
            "verification": {},
            "verification_passed": False,
            "e2e_suite": {},
            "e2e_results": {"summary": "E2E-проверка временно отключена."},
            "e2e_passed": False,
            "save_success": False,
            "save_skipped": False,
            "save_skip_reason": "",
            "save_error": "",
            "saved_to": "",
            "saved_jsonstring_to": "",
            "explanation": {},
            "suggested_changes": [],
            "clarifying_questions": [],
        }

    async def validate_code(state: PipelineState) -> dict:
        code = state.get("generated_code", "")
        logger.info(
            f"[{_AGENT_VALIDATE_CODE}] started",
            code_len=len(code),
        )

        if not code.strip():
            logger.warning(f"[{_AGENT_VALIDATE_CODE}] code is empty — skipping diagnostics")
            diagnostics = {
                "success": False,
                "failure_kind": "empty",
                "run_error": "Empty code",
                "run_output": "",
            }
            return {
                "validation_passed": False,
                "failure_stage": "validation",
                "diagnostics": diagnostics,
            }

        logger.info(
            f"[{_AGENT_VALIDATE_CODE}/async_run_diagnostics] calling",
            code_len=len(code),
        )
        diagnostics = await async_run_diagnostics(code)
        passed = diagnostics.get("success", False)
        logger.info(
            f"[{_AGENT_VALIDATE_CODE}/async_run_diagnostics] done",
            passed=passed,
            program_mode=diagnostics.get("program_mode", ""),
            failure_kind=diagnostics.get("failure_kind", ""),
            run_error=diagnostics.get("run_error", "") or "none",
            timed_out=diagnostics.get("timed_out", False),
        )
        logger.info(
            f"[{_AGENT_VALIDATE_CODE}] completed",
            passed=passed,
            failure_stage="" if passed else "validation",
        )
        return {
            "validation_passed": passed,
            "failure_stage": "" if passed else "validation",
            "diagnostics": diagnostics,
        }

    async def fix_code(state: PipelineState) -> dict:
        code = state.get("generated_code", "")
        diagnostics = state.get("diagnostics", {})
        verification = state.get("verification", {})
        base_prompt = state.get("base_prompt", "") or state.get("user_input", "")
        fix_iter = state.get("fix_iterations", 0)
        failure_stage = state.get("failure_stage", "unknown")
        compiled_request = state.get("compiled_request", {})

        logger.info(
            f"[{_AGENT_FIX_CODE}] started",
            iteration=fix_iter + 1,
            failure_stage=failure_stage,
            failure_kind=diagnostics.get("failure_kind", "unknown"),
            code_len=len(code),
            target_path=state.get("target_path", "(not set)"),
        )

        prompt = _build_fix_prompt(
            base_prompt=base_prompt,
            target_path=state.get("target_path", "(not set)"),
            failure_stage=failure_stage,
            failure_kind=diagnostics.get("failure_kind", "unknown"),
            run_error=diagnostics.get("run_error", "none"),
            run_output=diagnostics.get("run_output", "none"),
            runtime_fix_hints=diagnostics.get("runtime_fix_hints", []),
            verification_summary=verification.get("summary", "none"),
            missing_requirements=verification.get("missing_requirements", []),
            expected_paths=_format_values_for_prompt(verification.get("expected_workflow_paths", [])),
            actual_paths=_format_values_for_prompt(verification.get("actual_workflow_paths", [])),
            anti_patterns=_format_values_for_prompt(verification.get("anti_patterns", [])),
            code=code,
            compiled_request=compiled_request if isinstance(compiled_request, dict) else {},
        )
        verification_prompt = (
            str(compiled_request.get("verification_prompt", "")).strip()
            if isinstance(compiled_request, dict)
            else ""
        ) or base_prompt

        messages = [
            {"role": "system", "content": _FIX_SYSTEM},
            {"role": "user", "content": f"Original task:\n{base_prompt}"},
            {"role": "user", "content": prompt},
        ]
        logger.info(
            f"[{_AGENT_FIX_CODE}/llm.chat] calling",
            temperature=0.0,
            messages_count=len(messages),
            failure_stage=failure_stage,
        )
        raw = await llm.chat(messages, temperature=0.0)
        fixed_analysis = validate_lowcode_llm_output(raw)
        fixed = fixed_analysis["normalized"]
        logger.info(
            f"[{_AGENT_FIX_CODE}/llm.chat] done",
            raw_len=len(raw),
            fixed_len=len(fixed),
            valid=fixed_analysis["valid"],
            reason=fixed_analysis.get("reason", ""),
        )
        if not fixed:
            fixed = code

        retry_reasons = _assess_fix_candidate(
            original_code=code,
            candidate_code=fixed,
            failure_stage=failure_stage,
            diagnostics=diagnostics if isinstance(diagnostics, dict) else {},
            verification=verification if isinstance(verification, dict) else {},
            compiled_request=compiled_request if isinstance(compiled_request, dict) else {},
            verification_prompt=verification_prompt,
        )
        if retry_reasons:
            retry_prompt = _join_prompt_sections(
                prompt,
                "The previous fix attempt is still not acceptable for these reasons:",
                "\n".join(f"- {reason}" for reason in retry_reasons),
                "Return a materially different complete Lua script that explicitly resolves every listed issue.",
            )
            retry_messages = [
                {"role": "system", "content": _FIX_SYSTEM},
                {"role": "user", "content": f"Original task:\n{base_prompt}"},
                {"role": "user", "content": retry_prompt},
            ]
            logger.info(
                f"[{_AGENT_FIX_CODE}/llm.chat] retry calling",
                temperature=0.0,
                retry_reason_count=len(retry_reasons),
            )
            retry_raw = await llm.chat(retry_messages, temperature=0.0)
            retry_analysis = validate_lowcode_llm_output(retry_raw)
            retry_fixed = retry_analysis["normalized"]
            logger.info(
                f"[{_AGENT_FIX_CODE}/llm.chat] retry done",
                raw_len=len(retry_raw),
                fixed_len=len(retry_fixed),
                valid=retry_analysis["valid"],
                reason=retry_analysis.get("reason", ""),
            )
            if retry_fixed:
                fixed = retry_fixed

        logger.info(
            f"[{_AGENT_FIX_CODE}] completed",
            iteration=fix_iter + 1,
            code_len=len(fixed),
        )
        return {
            "generated_code": fixed,
            "compiled_request": compiled_request,
            "fix_iterations": fix_iter + 1,
            "failure_stage": "",
            "validation_passed": False,
            "verification_passed": False,
            "e2e_passed": False,
            "e2e_results": {"summary": "E2E-проверка временно отключена."},
            "save_success": False,
            "save_skipped": False,
            "save_skip_reason": "",
            "save_error": "",
            "saved_to": "",
            "saved_jsonstring_to": "",
            "explanation": {},
            "suggested_changes": [],
            "clarifying_questions": [],
        }

    async def verify_requirements(state: PipelineState) -> dict:
        code = state.get("generated_code", "")
        base_prompt = state.get("base_prompt", "") or state.get("user_input", "")
        diagnostics = state.get("diagnostics", {})
        compiled_request = state.get("compiled_request", {})
        verification_prompt = (
            str(compiled_request.get("verification_prompt", "")).strip()
            if isinstance(compiled_request, dict)
            else ""
        ) or base_prompt
        deterministic = inspect_lowcode_request_alignment(
            verification_prompt,
            code,
            compiled_request=compiled_request if isinstance(compiled_request, dict) else None,
        )

        logger.info(
            f"[{_AGENT_VERIFY_REQUIREMENTS}] started",
            code_len=len(code),
            base_prompt_len=len(base_prompt),
            deterministic_expected_paths=deterministic.get("expected_workflow_paths", []),
        )

        try:
            logger.info(
                f"[{_AGENT_VERIFY_REQUIREMENTS}/async_verify_requirements] calling",
                prompt_len=len(verification_prompt),
                has_run_output=bool(diagnostics.get("run_output", "")),
            )
            verification = await async_verify_requirements(
                llm,
                prompt=verification_prompt,
                code=code,
                run_output=diagnostics.get("run_output", ""),
                deterministic_context=deterministic,
            )
            logger.info(
                f"[{_AGENT_VERIFY_REQUIREMENTS}/async_verify_requirements] done",
                passed=verification.get("passed"),
                score=verification.get("score", 0),
                missing=verification.get("missing_requirements", []),
            )
            verification["error"] = False
        except Exception as exc:
            logger.warning(
                f"[{_AGENT_VERIFY_REQUIREMENTS}/async_verify_requirements] failed",
                error=str(exc),
            )
            verification = {
                "passed": deterministic.get("passed", False),
                "score": 0,
                "summary": f"LLM verification unavailable: {exc}",
                "missing_requirements": [],
                "warnings": ["verification_unavailable"],
                "error": True,
            }

        verification_checks = verification.get("checks", {}) if isinstance(verification, dict) else {}
        failed_checks = [
            name
            for name, payload in verification_checks.items()
            if isinstance(payload, dict) and payload.get("status") == "fail"
        ]
        unclear_checks = [
            name
            for name, payload in verification_checks.items()
            if isinstance(payload, dict) and payload.get("status") == "unclear"
        ]
        llm_passed = (bool(verification.get("passed")) or int(verification.get("score", 0) or 0) >= 70) and not failed_checks and not unclear_checks
        combined_missing = [
            *deterministic.get("missing_requirements", []),
            *[
                item
                for item in verification.get("missing_requirements", [])
                if item not in deterministic.get("missing_requirements", [])
            ],
        ]
        combined_warnings = [
            *deterministic.get("warnings", []),
            *[
                item
                for item in verification.get("warnings", [])
                if item not in deterministic.get("warnings", [])
            ],
        ]
        for check_name in failed_checks:
            reason = str(verification_checks.get(check_name, {}).get("reason", "")).strip()
            message = f"Verification checklist failed: {check_name}"
            if reason:
                message = f"{message} — {reason}"
            if message not in combined_missing:
                combined_missing.append(message)
        for check_name in unclear_checks:
            reason = str(verification_checks.get(check_name, {}).get("reason", "")).strip()
            message = f"Verification checklist is unclear: {check_name}"
            if reason:
                message = f"{message} — {reason}"
            if message not in combined_warnings:
                combined_warnings.append(message)
        llm_summary = str(verification.get("summary", "")).strip()
        deterministic_summary = str(deterministic.get("summary", "")).strip()
        compiled_request_summary = ""
        if isinstance(compiled_request, dict) and compiled_request:
            compiled_request_summary = (
                "Compiled request: "
                f"parseable_context={bool(compiled_request.get('has_parseable_context', False))}, "
                f"selected_operation={compiled_request.get('selected_operation', 'llm')}, "
                f"selected_path={compiled_request.get('selected_primary_path', 'none') or 'none'}, "
                f"needs_clarification={bool(compiled_request.get('needs_clarification', False))}"
            )
        llm_conflict = False
        if verification.get("error"):
            summary_parts = [compiled_request_summary, deterministic_summary]
            if llm_summary:
                summary_parts.append(llm_summary)
        else:
            llm_conflict = not deterministic.get("passed", False) and llm_passed
            summary_parts = [part for part in (compiled_request_summary, deterministic_summary) if part]
            if llm_conflict:
                summary_parts.append("LLM verifier disagreed with deterministic checks and was overruled.")
            elif llm_summary:
                summary_parts.append(llm_summary)
        combined_summary = " ".join(part for part in summary_parts if part).strip()
        if llm_conflict:
            conflict_warning = "llm_verifier_conflict_with_deterministic_checks"
            if conflict_warning not in combined_warnings:
                combined_warnings.append(conflict_warning)

        passed = deterministic.get("passed", False) and (llm_passed or verification.get("error"))
        verification = {
            **verification,
            "passed": passed,
            "summary": combined_summary or deterministic_summary or llm_summary or "Verification completed.",
            "missing_requirements": combined_missing,
            "warnings": combined_warnings,
            "expected_workflow_paths": deterministic.get("expected_workflow_paths", []),
            "actual_workflow_paths": deterministic.get("actual_workflow_paths", []),
            "anti_patterns": deterministic.get("anti_patterns", []),
            "deterministic_summary": deterministic_summary,
            "deterministic_passed": deterministic.get("passed", False),
            "llm_verifier_conflict": llm_conflict,
            "selected_operation": compiled_request.get("selected_operation", "") if isinstance(compiled_request, dict) else "",
            "selected_primary_path": compiled_request.get("selected_primary_path", "") if isinstance(compiled_request, dict) else "",
            "needs_clarification": compiled_request.get("needs_clarification", False) if isinstance(compiled_request, dict) else False,
            "has_parseable_context": compiled_request.get("has_parseable_context", False) if isinstance(compiled_request, dict) else False,
        }
        diagnostics_updated = dict(diagnostics)
        diagnostics_updated["verification_summary"] = verification.get("summary", "")
        diagnostics_updated["verification_checked"] = not verification.get("error", False)
        diagnostics_updated["verification_passed"] = passed
        diagnostics_updated["deterministic_verification_summary"] = deterministic_summary
        diagnostics_updated["expected_workflow_paths"] = verification.get("expected_workflow_paths", [])
        diagnostics_updated["actual_workflow_paths"] = verification.get("actual_workflow_paths", [])
        diagnostics_updated["anti_patterns"] = verification.get("anti_patterns", [])
        diagnostics_updated["selected_operation"] = verification.get("selected_operation", "")
        diagnostics_updated["selected_primary_path"] = verification.get("selected_primary_path", "")
        diagnostics_updated["has_parseable_context"] = verification.get("has_parseable_context", False)
        diagnostics_updated["failure_kind"] = "requirements" if combined_missing else diagnostics.get("failure_kind", "")

        logger.info(
            f"[{_AGENT_VERIFY_REQUIREMENTS}] completed",
            passed=passed,
            score=verification.get("score", 0),
            failure_stage="" if (passed or (verification.get("error") and not combined_missing)) else "requirements",
        )
        return {
            "verification": verification,
            "verification_passed": passed,
            "compiled_request": compiled_request,
            "failure_stage": "" if (passed or (verification.get("error") and not combined_missing)) else "requirements",
            "diagnostics": diagnostics_updated,
        }

    async def save_code(state: PipelineState) -> dict:
        code = state.get("generated_code", "")
        target_path = state.get("target_path", "")
        compiled_request = state.get("compiled_request", {})

        logger.info(
            f"[{_AGENT_SAVE_CODE}] started",
            code_len=len(code),
            target_path=target_path,
        )

        if not code.strip():
            logger.warning(f"[{_AGENT_SAVE_CODE}] code is empty — cannot save")
            return {
                "save_success": False,
                "save_skipped": False,
                "save_skip_reason": "",
                "save_error": "Empty code cannot be saved.",
                "saved_to": "",
                "saved_jsonstring_to": "",
            }
        if not target_path:
            logger.info(f"[{_AGENT_SAVE_CODE}] target_path is not set — skipping save")
            return {
                "current_code": code,
                "save_success": False,
                "save_skipped": True,
                "save_skip_reason": "Путь не указан в чате, поэтому код показан только в ответе и не сохранен в файл.",
                "save_error": "",
                "saved_to": "",
                "saved_jsonstring_to": "",
            }

        try:
            logger.info(
                f"[{_AGENT_SAVE_CODE}/save_final_output] calling",
                target_path=target_path,
                code_len=len(code),
            )
            saved = save_final_output(
                target_path,
                code,
                jsonstring_code=format_lowcode_json_payload(
                    code,
                    compiled_request=compiled_request if isinstance(compiled_request, dict) else None,
                    target_path=target_path,
                ),
            )
            logger.info(
                f"[{_AGENT_SAVE_CODE}/save_final_output] done",
                target_path=saved.get("lua_path", target_path),
                jsonstring_path=saved.get("jsonstring_path", ""),
            )
        except OSError as exc:
            logger.error(
                f"[{_AGENT_SAVE_CODE}/save_final_output] failed",
                target_path=target_path,
                error=str(exc),
            )
            return {
                "current_code": code,
                "save_success": False,
                "save_skipped": False,
                "save_skip_reason": "",
                "save_error": str(exc),
                "saved_to": "",
                "saved_jsonstring_to": "",
            }

        logger.info(
            f"[{_AGENT_SAVE_CODE}] completed",
            target_path=saved.get("lua_path", target_path),
            jsonstring_path=saved.get("jsonstring_path", ""),
        )
        return {
            "current_code": code,
            "save_success": True,
            "save_skipped": False,
            "save_skip_reason": "",
            "save_error": "",
            "saved_to": saved.get("lua_path", target_path),
            "saved_jsonstring_to": saved.get("jsonstring_path", ""),
        }

    async def explain_solution(state: PipelineState) -> dict:
        code = state.get("generated_code", "").strip()
        user_input = state.get("user_input", "")

        logger.info(
            f"[{_AGENT_EXPLAIN}] started",
            code_len=len(code),
            user_input=user_input[:80],
        )

        if not code:
            logger.warning(f"[{_AGENT_EXPLAIN}] code is empty — skipping explanation")
            return {"explanation": {}, "suggested_changes": [], "clarifying_questions": []}

        diagnostics = state.get("diagnostics", {})
        verification = state.get("verification", {})
        base_prompt = state.get("base_prompt", "") or user_input

        explain_prompt = (
            f"user_request: {base_prompt}\n\n"
            f"{LOWCODE_CONTRACT_TEXT}\n"
            f"Runtime validation summary:\n"
            f"- run_error: {diagnostics.get('run_error', 'none')}\n"
            f"- verification_summary: {verification.get('summary', 'none')}\n\n"
            "Code:\n"
            f"{format_lowcode_jsonstring(code)}\n\n"
            "Respond with JSON only. Write all text values in the same language as user_request."
        )

        try:
            logger.info(
                f"[{_AGENT_EXPLAIN}/llm.generate_json] calling",
                prompt_len=len(explain_prompt),
                temperature=0.1,
            )
            raw = await llm.generate_json(explain_prompt, system=_EXPLAIN_SYSTEM, temperature=0.1)
            logger.info(
                f"[{_AGENT_EXPLAIN}/llm.generate_json] done",
                has_summary=bool(raw.get("summary")),
                suggested_changes_count=len(raw.get("suggested_changes") or []),
                clarifying_questions_count=len(raw.get("clarifying_questions") or []),
            )
        except Exception as exc:
            logger.warning(
                f"[{_AGENT_EXPLAIN}/llm.generate_json] failed",
                error=str(exc),
            )
            raw = {}

        explanation = {
            "summary": str(raw.get("summary", "")).strip(),
            "what_is_in_code": _normalize_string_list(raw.get("what_is_in_code"), limit=5),
            "how_it_works": _normalize_string_list(raw.get("how_it_works"), limit=5),
        }
        suggested_changes = _normalize_string_list(raw.get("suggested_changes"), limit=3)
        clarifying_questions = _normalize_string_list(raw.get("clarifying_questions"), limit=3)

        if not explanation["summary"]:
            explanation["summary"] = "Код сохранен и готов к следующей итерации."
        if not explanation["what_is_in_code"]:
            explanation["what_is_in_code"] = ["Основная логика задачи реализована в одном Lua-файле."]
        if not explanation["how_it_works"]:
            explanation["how_it_works"] = ["Логика выполняется последовательно через функции и условия Lua."]

        logger.info(
            f"[{_AGENT_EXPLAIN}] completed",
            summary_len=len(explanation["summary"]),
            suggested_changes_count=len(suggested_changes),
            clarifying_questions_count=len(clarifying_questions),
        )
        return {
            "explanation": explanation,
            "suggested_changes": suggested_changes,
            "clarifying_questions": clarifying_questions,
        }

    async def answer_question(state: PipelineState) -> dict:
        user_input = state["user_input"]
        existing = state.get("current_code", "")

        logger.info(
            f"[{_AGENT_ANSWER}] started",
            user_input=user_input[:80],
            has_existing_code=bool(existing.strip()),
        )

        prompt = user_input
        if existing.strip():
            context = _target_context(state)
            if context:
                prompt = f"{context}\n\nCurrent code:\n```lua\n{existing}\n```\n\n{user_input}"
            else:
                prompt = f"Current code:\n```lua\n{existing}\n```\n\n{user_input}"

        logger.info(
            f"[{_AGENT_ANSWER}/llm.generate] calling",
            prompt_len=len(prompt),
        )
        answer = await llm.generate(prompt=prompt, system=_ANSWER_SYSTEM)
        logger.info(
            f"[{_AGENT_ANSWER}/llm.generate] done",
            answer_len=len(answer),
        )
        logger.info(f"[{_AGENT_ANSWER}] completed")
        return {
            "response": answer,
            "response_type": "text",
        }

    async def prepare_response(state: PipelineState) -> dict:
        code = state.get("generated_code", "")
        diagnostics = state.get("diagnostics", {})
        verification = state.get("verification", {})
        compiled_request = state.get("compiled_request", {})
        saved_to = state.get("saved_to", "")
        saved_jsonstring_to = state.get("saved_jsonstring_to", "")
        save_skipped = state.get("save_skipped", False)
        save_skip_reason = state.get("save_skip_reason", "")
        save_error = state.get("save_error", "")
        explanation = state.get("explanation", {})
        suggested_changes = state.get("suggested_changes", [])
        clarifying_questions = state.get("clarifying_questions", [])
        failure_stage = state.get("failure_stage", "")

        logger.info(
            f"[{_AGENT_PREPARE_RESPONSE}] started",
            code_len=len(code),
            save_success=state.get("save_success", False),
            e2e_passed=state.get("e2e_passed", False),
            failure_stage=failure_stage,
            suggested_changes_count=len(suggested_changes),
            clarifying_questions_count=len(clarifying_questions),
        )

        if not code.strip():
            return {
                "response": state.get("response", "Не удалось сгенерировать код."),
                "response_type": state.get("response_type", "error"),
            }

        lines: list[str] = []
        if state.get("save_success", False):
            lines.append("Код сгенерирован, прошел проверки и сохранен.\n")
        elif save_skipped:
            lines.append("Код сгенерирован и прошел проверки.\n")
            lines.append(f"{save_skip_reason or 'Путь не указан, поэтому файл не сохранялся.'}\n")
        else:
            lines.append("Код подготовлен, но финальные условия сохранения не выполнены.\n")
            if failure_stage:
                lines.append(f"Этап с проблемой: {failure_stage}\n")

        if diagnostics.get("run_error"):
            lines.append(f"Runtime error: {diagnostics.get('run_error')}\n")

        verification_summary = str(verification.get("summary", "")).strip()
        if verification_summary:
            lines.append(f"Проверка требований: {verification_summary}\n")

        if saved_to:
            lines.append(f"Сохранено в: `{saved_to}`\n")
            if saved_jsonstring_to:
                lines.append(f"JsonString сохранен в: `{saved_jsonstring_to}`\n")
        elif save_error:
            lines.append(f"Сохранение не удалось: {save_error}\n")

        lines.append(
            "```json\n"
            f"{format_lowcode_json_payload(code, compiled_request=compiled_request if isinstance(compiled_request, dict) else None, target_path=saved_to or state.get('target_path', ''))}\n"
            "```"
        )

        run_output = diagnostics.get("run_output", "").strip()
        if run_output:
            lines.append(f"\nRuntime output:\n```\n{run_output}\n```")

        if isinstance(explanation, dict):
            summary = str(explanation.get("summary", "")).strip()
            what_is_in_code = explanation.get("what_is_in_code", [])
            how_it_works = explanation.get("how_it_works", [])
            if summary:
                lines.append(f"\nЧто сделано: {summary}")
            if isinstance(what_is_in_code, list) and what_is_in_code:
                lines.append("\nЧто есть в коде:")
                for item in what_is_in_code[:5]:
                    item_text = str(item).strip()
                    if item_text:
                        lines.append(f"- {item_text}")
            if isinstance(how_it_works, list) and how_it_works:
                lines.append("\nКак это работает:")
                for item in how_it_works[:5]:
                    item_text = str(item).strip()
                    if item_text:
                        lines.append(f"- {item_text}")

        if isinstance(suggested_changes, list) and suggested_changes:
            lines.append("\nЧто можно улучшить:")
            for index, suggestion in enumerate(suggested_changes[:3], 1):
                suggestion_text = str(suggestion).strip()
                if suggestion_text:
                    lines.append(f"{index}. {suggestion_text}")

        if isinstance(clarifying_questions, list) and clarifying_questions:
            lines.append("\nУточняющие вопросы:")
            for index, question in enumerate(clarifying_questions[:3], 1):
                question_text = str(question).strip()
                if question_text:
                    lines.append(f"{index}. {question_text}")

        response_text = "\n".join(lines)
        logger.info(
            f"[{_AGENT_PREPARE_RESPONSE}] completed",
            response_len=len(response_text),
            response_type="code",
        )
        return {
            "response": response_text,
            "response_type": "code",
            "current_code": code,
        }

    return {
        "resolve_target": resolve_target,
        "route_intent": route_intent,
        "prepare_generation_context": prepare_generation_context,
        "generate_code": generate_code,
        "refine_code": refine_code,
        "validate_code": validate_code,
        "fix_code": fix_code,
        "verify_requirements": verify_requirements,
        "save_code": save_code,
        "explain_solution": explain_solution,
        "answer_question": answer_question,
        "prepare_response": prepare_response,
    }
