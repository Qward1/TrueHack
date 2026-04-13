"""LangGraph node functions for the canonical Lua generation pipeline."""

from __future__ import annotations

import json
import os
import re
from typing import Any, Callable

import structlog

from src.core.llm import LLMProvider
from src.core.state import PipelineState
from src.tools.lua_tools import (
    LOWCODE_CONTRACT_TEXT,
    LOWCODE_JSONSTRING_OPEN,
    LOWCODE_LUA_VERSION,
    LOWCODE_RESPONSE_FORMAT_REQUIREMENT,
    async_run_diagnostics,
    async_verify_requirements,
    compile_lowcode_request,
    extract_function_names,
    format_lowcode_json_payload,
    format_lowcode_jsonstring,
    is_truncated_lowcode_response,
    restore_lost_functions,
    normalize_lua_code,
    validate_lowcode_llm_output,
    analyze_lua_response,
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
_AGENT_FIX_VALIDATION_CODE = "ValidationFixer"
_AGENT_FIX_VERIFICATION_CODE = "VerificationFixer"
_AGENT_VERIFY_REQUIREMENTS = "RequirementsVerifier"
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

LOWCODE_LUA_RULES = (
    "LowCode Lua platform rules: "
    "Use Lua 5.5. "
    "The script must be returned in JsonString form as lua{...}lua. "
    "Do not use JsonPath. Access workflow data only by direct paths. "
    "All declared workflow variables are stored in wf.vars. "
    "All input variables passed at workflow start are stored in wf.initVariables. "

    "Allowed data types: nil, boolean, number, string, table, function, and array. "
    "When a new array is needed, create it with _utils.array.new(), populate items explicitly, "
    "then call _utils.array.markAsArray(arr). "
    "When an existing table must be treated as an array, use _utils.array.markAsArray(arr). "

    "Allowed control flow constructs: if/then/else, while/do/end, for/do/end, repeat/until."
)

_GENERATE_SYSTEM = (
    "You write Lua 5.5 workflow scripts for LowCode. "
    
    + LOWCODE_LUA_RULES +

    " Use direct workflow access only: wf.vars.* and wf.initVariables.*. "
    "Never recreate provided workflow data as local demo tables. "
    "Never use print(), io.write(), io.read(), console prompts, apps, APIs, or wrappers. "
    "Use return and/or explicit wf.vars updates only when requested. "

    "Do not assume hidden parsers or unsupported APIs. "
    "For datetime conversion from strings, parse components explicitly. "
    "Do not pass ISO 8601 strings directly into os.time(...). "
    "If timezone offset exists, handle it explicitly. "

    "Use exact workflow paths from the task or provided context. "
    "Do not invent new workflow paths unless the task explicitly names a target wf.vars path. "

    "Output nothing except lua{...}lua."
)

_REFINE_SYSTEM = (
    "You modify existing Lua workflow scripts according to the user's request. "
    "Return the complete updated script. "
    "Preserve existing functions unless explicitly asked to remove them. "
    f"{LOWCODE_CONTRACT_TEXT}"
    "Keep useful existing structure, but expand the script when the new behavior needs additional guards, loops, helpers, or multi-step transformation logic.\n\n"
    f"{LOWCODE_RESPONSE_FORMAT_REQUIREMENT}"
)

_VALIDATOR_HINT_SYSTEM = (
    "You are a Lua error analyst. "
    "State: 1) the exact error type and message, 2) the failing line number, "
    "3) the exact fix (which variable or call to change and how). "
    "Do not paraphrase the error. Copy key identifiers verbatim from the error message. "
    "Plain text only."
)

_FIX_VALIDATION_SYSTEM = (
    f"You fix {LOWCODE_LUA_VERSION} workflow scripts that fail during execution. "
    "You receive the broken script, the Lua runtime error, and an expert analysis of the root cause. "
    "Return the corrected script.\n"
    f"{LOWCODE_CONTRACT_TEXT}"
    "Repair exactly the failing area identified in the error analysis. "
    "Preserve all working logic and structure. "
    "Do not convert the script into a console app.\n\n"
    f"{LOWCODE_RESPONSE_FORMAT_REQUIREMENT}"
)

_FIX_VERIFICATION_SYSTEM = (
    f"You fix {LOWCODE_LUA_VERSION} workflow scripts that fail requirement verification. "
    "You receive the broken script, a verification summary, and a list of unmet requirements. "
    "Return the corrected script.\n"
    f"{LOWCODE_CONTRACT_TEXT}"
    "Implement the missing or incorrect logic so every listed requirement is satisfied. "
    "Do not change parts of the script that already work correctly. "
    "Pay close attention to the real data types and shapes used by the workflow values. "
    "If the task, workflow anchor, verification summary, or workflow context indicates array/object/scalar expectations, requested item keys, or semantic expectations, preserve and enforce those type constraints explicitly in the fixed script.\n\n"
    f"{LOWCODE_RESPONSE_FORMAT_REQUIREMENT}"
)

_CONTINUATION_SYSTEM = (
    "You are completing a Lua script that was cut short. "
    "Output ONLY the remaining Lua code lines that finish the script from where it was cut. "
    "Do not repeat any code already written. "
    "Do not add any wrapper, prefix, or markdown. "
    "End with the appropriate Lua closing statements."
)

_ANSWER_SYSTEM = (
    "You are a helpful Lua programming assistant. "
    "Answer in the same language as the user's message."
)

_EXPLAIN_SYSTEM = (
    "You explain generated Lua code for the user. "
    "Return strict JSON only with keys: summary, what_is_in_code, how_it_works, "
    "suggested_changes, clarifying_questions. "
    "`summary` must be a string. "
    "`what_is_in_code`, `how_it_works`, `suggested_changes`, and `clarifying_questions` must each be JSON arrays of strings, even when there is only one item. "
    "Keep those arrays short. "
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
- If the task is shape-sensitive, explicitly distinguish object-like tables from array-like tables with numeric keys instead of relying only on `type(x) == "table"`, `next(x)`, or empty/non-empty tests.
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
    if isinstance(value, str):
        text = str(value).strip()
        if not text:
            return []
        normalized = [
            re.sub(r"^\s*(?:[-*]|\d+[.)])\s*", "", line).strip()
            for line in text.splitlines()
            if re.sub(r"^\s*(?:[-*]|\d+[.)])\s*", "", line).strip()
        ]
        if not normalized:
            normalized = [text]
        return normalized[:limit]
    if not isinstance(value, list):
        return []
    normalized = [str(item).strip() for item in value if str(item).strip()]
    return normalized[:limit]


def _resolve_workflow_path_value(snapshot: object, dotted_path: str) -> tuple[bool, object]:
    current = snapshot
    path = str(dotted_path or "").strip()
    if not path:
        return False, None
    for segment in path.split("."):
        if isinstance(current, dict) and segment in current:
            current = current[segment]
            continue
        return False, None
    return True, current


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


def _format_planner_section(compiled_request: dict[str, Any]) -> str:
    """Render planner_result as a prompt section.

    Pulled from compiled_request["planner_result"] so the existing prompt
    builders can stay single-argument.
    """
    if not isinstance(compiled_request, dict):
        return ""
    planner = compiled_request.get("planner_result") or {}
    if not isinstance(planner, dict) or not planner:
        return ""

    parts: list[str] = []
    reformulated = str(planner.get("reformulated_task", "") or "").strip()
    if reformulated:
        parts.append(f"Reformulated task:\n{reformulated}")
    paths = [
        str(p).strip()
        for p in planner.get("identified_workflow_paths", []) or []
        if str(p).strip()
    ]
    if paths:
        parts.append(f"Planner-identified workflow paths: {', '.join(paths)}")
    expected_action = str(planner.get("expected_result_action", "") or "").strip()
    if expected_action:
        parts.append(f"Expected result action: {expected_action}")
    return "\n\n".join(parts)


def _compact_json_for_prompt(value: object, limit: int = 4000) -> str:
    try:
        rendered = json.dumps(value, ensure_ascii=False, indent=2)
    except Exception:
        rendered = str(value)
    if len(rendered) <= limit:
        return rendered
    return rendered[: limit - 3].rstrip() + "..."


def _build_verification_extra_context(
    compiled_request: dict[str, Any],
    diagnostics: dict[str, Any],
) -> str:
    if not isinstance(compiled_request, dict):
        compiled_request = {}
    if not isinstance(diagnostics, dict):
        diagnostics = {}

    sections: list[str] = []
    planner_section = _format_planner_section(compiled_request)
    if planner_section:
        sections.append("Planner/task analysis:\n" + planner_section)

    planner_result = compiled_request.get("planner_result") or {}

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
    verifier_instructions: list[str] = []
    selected_operation = str(compiled_request.get("selected_operation", "") or "").strip().lower()
    if selected_operation == "filter":
        verifier_instructions.append(
            "This is a filter/select task: every returned item must satisfy the requested condition."
        )
        verifier_instructions.append(
            "For 'has value' / 'non-empty' conditions, treat nil, empty strings, and whitespace-only strings as empty values."
        )
        verifier_instructions.append(
            "If any returned item violates the requested filter rule, set passed=false and name the violating item by identifiers like id, SKU, email, or name when available."
        )
    if requested_item_keys:
        verifier_instructions.append(
            "Relevant workflow item fields to inspect: " + ", ".join(requested_item_keys)
        )
    if selected_type:
        verifier_instructions.append(
            "Treat this workflow path type hint as mandatory during verification: "
            + selected_type
        )
    if semantic_expectations:
        verifier_instructions.append(
            "Treat these semantic/type expectations as mandatory during verification: "
            + ", ".join(semantic_expectations)
        )
    if selected_type or semantic_expectations or requested_item_keys:
        verifier_instructions.append(
            "Pay extra attention to whether the code and the observed runtime/workflow-state result preserve the required data types and shapes."
        )
    if verifier_instructions:
        sections.append("Verification instructions:\n- " + "\n- ".join(verifier_instructions))

    parsed_context = compiled_request.get("parsed_context")
    if compiled_request.get("has_parseable_context") and parsed_context is not None:
        sections.append(
            "Parsed workflow context used during validation:\n"
            + _compact_json_for_prompt(parsed_context)
        )

    workflow_state = diagnostics.get("workflow_state")
    selected_path = str(compiled_request.get("selected_primary_path", "") or "").strip()
    if not selected_path and isinstance(planner_result, dict):
        identified_paths = [
            str(path).strip()
            for path in planner_result.get("identified_workflow_paths", []) or []
            if str(path).strip()
        ]
        if identified_paths:
            selected_path = identified_paths[0]
    if workflow_state is not None:
        if compiled_request.get("has_parseable_context") and parsed_context is not None:
            sections.append(
                "Original workflow state before execution:\n"
                + _compact_json_for_prompt(parsed_context)
            )
            if selected_path:
                original_found, original_value = _resolve_workflow_path_value(parsed_context, selected_path)
                if original_found:
                    sections.append(
                        f"Original workflow value at {selected_path} before execution:\n"
                        + _compact_json_for_prompt(original_value)
                    )
        sections.append(
            "Updated workflow state after execution:\n"
            + _compact_json_for_prompt(workflow_state)
        )
        if selected_path:
            found, selected_value = _resolve_workflow_path_value(workflow_state, selected_path)
            if found:
                sections.append(
                    f"Updated workflow value at {selected_path} after execution:\n"
                    + _compact_json_for_prompt(selected_value)
                )
        sections.append(
            "Compare the original workflow state before execution with the updated workflow state after execution. "
            "Judge whether the observed before/after change matches the user request. "
            "The script may satisfy the task either by returning a value or by updating wf.vars / wf.initVariables. "
            "If the return value is null, verify against the workflow-state difference above."
        )

    return "\n\n".join(section for section in sections if section.strip())


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
        analysis = analyze_lua_response(candidate)
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
    active_clarifying_questions: list[str] | None = None,
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
    active_questions = [
        str(question).strip()
        for question in (active_clarifying_questions or [])
        if str(question).strip()
    ]
    has_active_code_clarification = bool(has_existing_code and active_questions)

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
        "active_clarifying_questions": active_questions,
        "has_active_code_clarification": has_active_code_clarification,
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
    has_active_code_clarification = bool(features.get("has_active_code_clarification"))

    if has_retry_signal:
        return ("retry", "retry_with_code") if effective_has_code else ("create", "retry_without_code")

    if has_active_code_clarification and effective_has_code:
        return "change", "active_code_clarification_followup"

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


def _normalize_runtime_candidate(code: str) -> str:
    normalized = normalize_lua_code(str(code or ""))
    return normalized if normalized else str(code or "")


def _format_numbered_code_block(code: str) -> str:
    lines = str(code or "").splitlines() or [str(code or "")]
    return "\n".join(f"{index + 1:>3} | {line}" for index, line in enumerate(lines))


def _clean_run_error(run_error: str) -> str:
    """Strip Lua stack traceback, return only the primary error line."""
    text = str(run_error or "").strip()
    if not text:
        return text
    parts = re.split(r"\nstack traceback:", text, maxsplit=1)
    first_part = parts[0].strip()
    for line in first_part.splitlines():
        cleaned = line.strip()
        if cleaned:
            return cleaned
    return first_part or text


def _extract_runtime_line_number(run_error: str) -> int | None:
    text = str(run_error or "").strip()
    if not text:
        return None
    match = re.search(r":(?P<line>\d+):", text)
    if not match:
        return None
    try:
        return int(match.group("line"))
    except (TypeError, ValueError):
        return None


def _extract_runtime_line_hint(run_error: str) -> str:
    line_number = _extract_runtime_line_number(run_error)
    if line_number is None:
        return ""
    return f"Likely failing Lua line: {line_number}"


def _format_code_context_window(code: str, line_number: int | None, radius: int = 2) -> str:
    if line_number is None:
        return ""
    lines = str(code or "").splitlines()
    if not lines:
        return ""
    requested_line = max(1, line_number)
    effective_line = min(requested_line, len(lines))
    start = max(1, effective_line - radius)
    end = min(len(lines), effective_line + radius)
    prefix = ""
    if requested_line != effective_line:
        prefix = (
            f"Runtime points to line {requested_line}, but normalized code has {len(lines)} lines; "
            "showing the nearest available context.\n"
        )
    excerpt = [
        f"{index:>3} | {lines[index - 1]}"
        for index in range(start, end + 1)
    ]
    return prefix + "\n".join(excerpt)


def _workflow_context_for_validation(compiled_request: dict[str, Any] | None) -> dict[str, Any] | None:
    if not isinstance(compiled_request, dict):
        return None
    workflow_context = compiled_request.get("parsed_context")
    return workflow_context if isinstance(workflow_context, dict) else None


async def _run_diagnostics_with_optional_context(
    code: str,
    compiled_request: dict[str, Any] | None,
) -> dict[str, Any]:
    workflow_context = _workflow_context_for_validation(compiled_request)
    if workflow_context is None:
        return await async_run_diagnostics(code)
    try:
        return await async_run_diagnostics(code, workflow_context=workflow_context)
    except TypeError as exc:
        if "workflow_context" not in str(exc):
            raise
        return await async_run_diagnostics(code)


def _format_runtime_context(runtime_context: object, limit: int = 8) -> str:
    if not isinstance(runtime_context, dict):
        return ""

    locals_payload = runtime_context.get("locals", [])
    if not isinstance(locals_payload, list):
        locals_payload = []

    lines: list[str] = []
    frame_line = runtime_context.get("line")
    frame_name = str(runtime_context.get("function", "") or "").strip()
    frame_source = str(runtime_context.get("source", "") or "").strip()
    if frame_line:
        lines.append(f"Runtime frame line: {frame_line}")
    if frame_name:
        lines.append(f"Runtime frame function: {frame_name}")
    if frame_source:
        lines.append(f"Runtime frame source: {frame_source}")

    rendered_locals: list[str] = []
    for item in locals_payload:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name", "") or "").strip()
        value_type = str(item.get("type", "") or "").strip() or "unknown"
        value = str(item.get("value", "") or "").strip()
        if not name or name in {"wf", "_utils"}:
            continue
        rendered_locals.append(f"- {name} [{value_type}] = {value or '<empty>'}")
        if len(rendered_locals) >= limit:
            break

    if rendered_locals:
        if lines:
            lines.append("")
        lines.append("Runtime locals at failure:")
        lines.extend(rendered_locals)

    return "\n".join(lines).strip()


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
    candidate = _normalize_runtime_candidate(candidate_code)
    original = _normalize_runtime_candidate(original_code)
    if not candidate:
        return ["The fix attempt returned empty code."]

    if _code_signature(candidate) == _code_signature(original):
        reasons.append("The fix attempt did not materially change the code.")

    analysis = analyze_lua_response(candidate)
    if not analysis.get("valid"):
        reasons.append(str(analysis.get("reason", "") or "The fix attempt does not look like a valid standalone Lua file."))

    if failure_stage == "requirements":
        previous = [
            str(item).strip()
            for item in verification.get("missing_requirements", [])
            if str(item).strip()
        ]
        if previous and _code_signature(candidate) == _code_signature(original):
            reasons.append(
                "The fix did not address the previous requirement failures: " + ", ".join(previous)
            )
    if failure_stage == "validation" and diagnostics.get("run_error"):
        runtime_hints = [
            str(item).strip()
            for item in diagnostics.get("runtime_fix_hints", [])
            if str(item).strip()
        ]
        if runtime_hints and _code_signature(candidate) == _code_signature(original):
            reasons.append("The fix did not address the runtime diagnostics or runtime fix hints.")

    deduped: list[str] = []
    seen: set[str] = set()
    for reason in reasons:
        if reason not in seen:
            seen.add(reason)
            deduped.append(reason)
    return deduped


def _format_planner_section_compact(compiled_request: dict[str, Any]) -> str:
    planner = compiled_request.get("planner_analysis") or {}
    if not planner:
        return ""

    reformulated = planner.get("reformulated_task", "")
    paths = planner.get("identified_workflow_paths", [])
    operation = planner.get("target_operation", "")
    action = planner.get("expected_result_action", "")
    data_types = planner.get("data_types", {})

    parts = []
    if reformulated:
        parts.append(f"- reformulated_task: {reformulated}")
    if operation:
        parts.append(f"- target_operation: {operation}")
    if action:
        parts.append(f"- expected_result_action: {action}")
    if paths:
        parts.append(f"- workflow_paths: {', '.join(paths)}")
    if data_types:
        typed = ", ".join(f"{k}={v}" for k, v in data_types.items())
        parts.append(f"- data_types: {typed}")

    return "\n".join(parts)


def _build_generation_prompt(compiled_request: dict[str, Any]) -> str:
    task = str(compiled_request.get("task_text", "") or "").strip()
    provided_context = str(compiled_request.get("raw_context", "") or "").strip()
    clarification_text = str(compiled_request.get("clarification_text", "") or "").strip()
    prompt_context = _format_prompt_workflow_context(compiled_request)
    planner_section = _format_planner_section_compact(compiled_request)

    sections = [
        f"Task:\n{task}",
        f"Clarification:\n{clarification_text}" if clarification_text else "",
        f"Workflow anchor:\n{prompt_context}" if prompt_context else "",
        f"Planner hint:\n{planner_section}" if planner_section else "",
        f"Workflow context:\n{provided_context}" if provided_context else "",
        "Decision rules:\n"
        "1. Prefer explicit task text and workflow context over planner hints.\n"
        "2. Use only explicit workflow paths.\n"
        "3. Do not invent demo input tables.\n"
        "4. For datetime conversion, parse components explicitly.\n"
        "5. Do not pass ISO 8601 strings directly to os.time(...).\n"
        "6. Handle timezone offsets explicitly when present.\n"
        "7. Save to wf.vars only if explicitly requested.\n"
        "8. Output only lua{...}lua."
    ]
    return _join_prompt_sections(*sections)


def _build_refine_prompt(
    *,
    function_list: str,
    code: str,
    compiled_request: dict[str, Any],
) -> str:
    # task_text = planner-processed change request; original_task = original create task
    task_text = str(compiled_request.get("task_text", "") or "").strip()
    original_task = str(compiled_request.get("original_task", "") or "").strip()
    provided_context = str(compiled_request.get("raw_context", "") or "").strip()
    clarification_text = str(compiled_request.get("clarification_text", "") or "").strip()
    prompt_context = _format_prompt_workflow_context(compiled_request)
    planner_section = _format_planner_section(compiled_request)
    sections = [
        f"Original task:\n{original_task}" if original_task else "",
        (
            "Original workflow context:\n"
            f"{provided_context}"
        ) if provided_context else "",
        f"Change clarification:\n{clarification_text}" if clarification_text else "",
        f"Workflow anchor:\n{prompt_context}" if prompt_context else "",
        f"Planner analysis:\n{planner_section}" if planner_section else "",
        (
            "Existing functions you must preserve unless the user explicitly removes them:\n"
            f"{function_list}"
        ),
        f"Current code:\n{code}",
        f"Change request:\n{task_text}",
        _PROMPT_STYLE_RULES,
        _PROMPT_SYNTHESIS_GUIDANCE,
        LOWCODE_RESPONSE_FORMAT_REQUIREMENT,
    ]
    return _join_prompt_sections(*sections)


def _build_fix_validation_prompt(
    *,
    code: str,
    run_error: str,
    llm_fix_hint: str,
    compiled_request: dict[str, Any],
) -> str:
    """Prompt for fixing Lua runtime/syntax errors (validation failures)."""
    task_text = str(compiled_request.get("task_text", "") or "").strip()
    provided_context = str(compiled_request.get("raw_context", "") or "").strip()
    if len(provided_context) > 600:
        provided_context = provided_context[:600].rstrip() + "..."
    prompt_context = _format_prompt_workflow_context(compiled_request)
    planner_section = _format_planner_section(compiled_request)
    runtime_line_number = _extract_runtime_line_number(run_error)
    runtime_line_hint = _extract_runtime_line_hint(run_error)
    runtime_code_context = _format_code_context_window(code, runtime_line_number)
    sections = [
        f"Task:\n{task_text}" if task_text else "",
        f"Original workflow context:\n{provided_context}" if provided_context else "",
        f"Workflow anchor:\n{prompt_context}" if prompt_context else "",
        f"Planner analysis:\n{planner_section}" if planner_section else "",
        f"Runtime error:\n{run_error}" if str(run_error or "").strip() else "",
        runtime_line_hint,
        f"Failing code context:\n{runtime_code_context}" if runtime_code_context else "",
        f"Error analysis:\n{llm_fix_hint}" if llm_fix_hint else "",
        f"Current code with line numbers:\n{_format_numbered_code_block(code)}",
        "Fix the runtime error identified in the error analysis.",
        "Use the numbered code to repair the exact failing area.",
        "Preserve working logic and change only what is needed to resolve the error.",
        _PROMPT_STYLE_RULES,
        LOWCODE_RESPONSE_FORMAT_REQUIREMENT,
    ]
    return _join_prompt_sections(*sections)


def _build_fix_verification_prompt(
    *,
    code: str,
    verification_summary: str,
    missing_requirements: list,
    runtime_result: str,
    workflow_state: str,
    compiled_request: dict[str, Any],
) -> str:
    """Prompt for fixing logic/requirement failures (verification failures)."""
    task_text = str(compiled_request.get("task_text", "") or "").strip()
    provided_context = str(compiled_request.get("raw_context", "") or "").strip()
    if len(provided_context) > 600:
        provided_context = provided_context[:600].rstrip() + "..."
    prompt_context = _format_prompt_workflow_context(compiled_request)
    planner_section = _format_planner_section(compiled_request)
    summary_text = str(verification_summary or "").strip()
    missing_str = "\n".join(f"- {r}" for r in (missing_requirements or []) if str(r).strip())
    sections = [
        f"Task:\n{task_text}" if task_text else "",
        f"Original workflow context:\n{provided_context}" if provided_context else "",
        f"Workflow anchor:\n{prompt_context}" if prompt_context else "",
        f"Planner analysis:\n{planner_section}" if planner_section else "",
        f"Verification summary:\n{summary_text}" if summary_text else "",
        f"Unmet requirements:\n{missing_str}" if missing_str else "",
        f"Runtime result during validation:\n{runtime_result}" if runtime_result else "",
        f"Updated workflow state after execution:\n{workflow_state}" if workflow_state else "",
        f"Current code with line numbers:\n{_format_numbered_code_block(code)}",
        "Fix the logic so all listed requirements are satisfied.",
        "Do not change parts of the script that already work correctly.",
        "Ensure the workflow paths used match the task and the expected paths in the planner analysis.",
        _PROMPT_STYLE_RULES,
        LOWCODE_RESPONSE_FORMAT_REQUIREMENT,
    ]
    return _join_prompt_sections(*sections)


def create_nodes(llm: LLMProvider) -> dict[str, Callable]:
    """Build node callables from a pre-constructed LLM provider."""

    async def _attempt_continuation(raw: str, agent_name: str) -> str:
        """Try to complete a truncated lua{...}lua response.

        Returns the assembled full response (still in lua{...}lua wrapper) on
        success, or empty string when continuation fails or produces invalid Lua.
        """
        if not is_truncated_lowcode_response(raw):
            return ""
        cleaned = raw.strip()
        if not cleaned.lower().startswith(LOWCODE_JSONSTRING_OPEN.lower()):
            return ""
        partial_body = cleaned[len(LOWCODE_JSONSTRING_OPEN):].lstrip("\n")
        if not partial_body.strip():
            return ""

        continuation_prompt = (
            "The following Lua script was cut short. "
            "Complete it from where it was cut.\n"
            "Output ONLY the remaining code lines that finish the script. "
            "Do not repeat any code already written.\n\n"
            f"Code cut short:\n{partial_body}"
        )
        logger.info(f"[{agent_name}/continuation] calling", partial_len=len(partial_body))
        try:
            continuation = await llm.generate(
                prompt=continuation_prompt,
                system=_CONTINUATION_SYSTEM,
                temperature=0.0,
                agent_name=agent_name,
            )
            continuation = continuation.strip()
            if not continuation:
                return ""

            cont_lower = continuation.lower()
            if cont_lower.startswith(LOWCODE_JSONSTRING_OPEN.lower()):
                # Model re-wrapped — use as-is, append close if missing
                assembled = continuation
                if not cont_lower.endswith("}lua"):
                    assembled = continuation + "\n}lua"
            else:
                joined_body = partial_body.rstrip() + "\n" + continuation
                assembled = LOWCODE_JSONSTRING_OPEN + "\n" + joined_body + "\n}lua"

            assembled_analysis = validate_lowcode_llm_output(assembled)
            if assembled_analysis["valid"]:
                logger.info(
                    f"[{agent_name}/continuation] success",
                    assembled_len=len(assembled_analysis["normalized"]),
                )
                return assembled
            logger.warning(
                f"[{agent_name}/continuation] assembled but invalid",
                reason=assembled_analysis.get("reason", ""),
            )
            return ""
        except Exception as exc:
            logger.warning(f"[{agent_name}/continuation] failed", error=str(exc))
            return ""

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
        active_clarifying_questions = [
            str(question).strip()
            for question in state.get("active_clarifying_questions", []) or []
            if str(question).strip()
        ]
        features = _collect_intent_features(
            user_input=state["user_input"],
            has_existing_code=has_code,
            base_prompt=state.get("base_prompt", ""),
            active_clarifying_questions=active_clarifying_questions,
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
            result = await llm.generate_json(
                prompt,
                system=_ROUTE_SYSTEM,
                agent_name=_AGENT_ROUTE_INTENT,
            )
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
        )

        # Soft enrichment from planner: re-compile with the reformulated task
        # if the original compile did not lock onto a workflow path.
        planner_result = state.get("planner_result", {}) or {}
        reformulated_task = ""
        if isinstance(planner_result, dict):
            reformulated_task = str(planner_result.get("reformulated_task", "") or "").strip()
        if (
            reformulated_task
            and reformulated_task != (task_text or task_source_prompt).strip()
            and not compiled_request.get("has_parseable_context")
        ):
            compiled_request_v2 = compile_lowcode_request(
                task_text=reformulated_task,
                raw_context=raw_context,
                clarification_text=clarification_text,
            )
            current_paths = len(compiled_request.get("expected_workflow_paths", []) or [])
            v2_paths = len(compiled_request_v2.get("expected_workflow_paths", []) or [])
            if v2_paths > current_paths:
                compiled_request = compiled_request_v2

        compiled_request["planner_result"] = planner_result if isinstance(planner_result, dict) else {}

        # Always use planner's reformulated_task as the canonical task description for
        # generation/refine/fix prompts, so raw user_input never reaches LLM code agents.
        if reformulated_task:
            compiled_request["task_text"] = reformulated_task

        # Store the original create-task text (without pasted JSON) so refine/fix prompts
        # can show it as background context alongside the current change request.
        orig_task_text, _ = split_task_and_context(existing_base_prompt or user_input)
        compiled_request["original_task"] = orig_task_text or (existing_base_prompt or user_input).strip()

        # Verifier must see the planner-processed task, never raw user_input/base_prompt.
        # `task_text` was overwritten above with planner's reformulated_task when available;
        # otherwise it holds the deterministic compiler's parsed task.
        verification_prompt = str(compiled_request.get("task_text", "") or "").strip()
        if not verification_prompt:
            # Hard fallback when planner is disabled AND compiler produced nothing.
            verification_prompt = str(compiled_request.get("original_task", "") or "").strip()
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

        prompt = _build_generation_prompt(compiled_request)
        generation_temperature = _generation_temperature(compiled_request if isinstance(compiled_request, dict) else {})

        logger.info(
            f"[{_AGENT_GENERATE_CODE}/llm.generate] calling",
            prompt_len=len(prompt),
            target_path=target_path,
            temperature=generation_temperature,
        )
        raw = await llm.generate(
            prompt=prompt,
            system=_GENERATE_SYSTEM,
            temperature=generation_temperature,
            agent_name=_AGENT_GENERATE_CODE,
        )
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
            continuation_succeeded = False
            if is_truncated_lowcode_response(raw):
                continued = await _attempt_continuation(raw, _AGENT_GENERATE_CODE)
                if continued:
                    cont_analysis = validate_lowcode_llm_output(continued)
                    if cont_analysis["normalized"]:
                        code = cont_analysis["normalized"]
                        continuation_succeeded = True

            if not continuation_succeeded:
                strict_prompt = (
                    f"{prompt}\n\n"
                    f"Previous response issue: {analysis['reason']}\n"
                    f"{LOWCODE_RESPONSE_FORMAT_REQUIREMENT}"
                )
                logger.info(
                    f"[{_AGENT_GENERATE_CODE}/llm.generate] retry calling",
                    temperature=0.0,
                )
                raw_retry = await llm.generate(
                    prompt=strict_prompt,
                    system=_GENERATE_SYSTEM,
                    temperature=0.0,
                    agent_name=_AGENT_GENERATE_CODE,
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
            code = normalize_lua_code(raw)

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
        raw = await llm.generate(
            prompt=prompt,
            system=_REFINE_SYSTEM,
            temperature=refine_temperature,
            agent_name=_AGENT_REFINE_CODE,
        )
        analysis = validate_lowcode_llm_output(raw)
        code = analysis["normalized"]
        logger.info(
            f"[{_AGENT_REFINE_CODE}/llm.generate] done",
            raw_len=len(raw),
            normalized_len=len(code),
            valid=analysis["valid"],
            reason=analysis.get("reason", ""),
        )
        if not code and is_truncated_lowcode_response(raw):
            continued = await _attempt_continuation(raw, _AGENT_REFINE_CODE)
            if continued:
                cont_analysis = validate_lowcode_llm_output(continued)
                if cont_analysis["normalized"]:
                    code = cont_analysis["normalized"]
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
        original_code = state.get("generated_code", "")
        code = _normalize_runtime_candidate(original_code)
        compiled_request = state.get("compiled_request", {})
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
        diagnostics = await _run_diagnostics_with_optional_context(
            code,
            compiled_request if isinstance(compiled_request, dict) else {},
        )
        passed = diagnostics.get("success", False)
        logger.info(
            f"[{_AGENT_VALIDATE_CODE}/async_run_diagnostics] done",
            passed=passed,
            program_mode=diagnostics.get("program_mode", ""),
            failure_kind=diagnostics.get("failure_kind", ""),
            run_error=diagnostics.get("run_error", "") or "none",
            timed_out=diagnostics.get("timed_out", False),
        )
        # On failure: ask LLM to analyse the error and produce a concrete fix hint.
        # This replaces the old deterministic infer_runtime_fix_hints approach.
        if not passed and str(diagnostics.get("run_error", "") or "").strip():
            try:
                workflow_context = _workflow_context_for_validation(
                    compiled_request if isinstance(compiled_request, dict) else {},
                )
                workflow_context_section = ""
                if workflow_context is not None:
                    workflow_context_section = (
                        "Workflow context used during validation:\n"
                        f"{_compact_json_for_prompt(workflow_context)}\n\n"
                    )
                elif isinstance(compiled_request, dict):
                    raw_context = str(compiled_request.get("raw_context", "") or "").strip()
                    if raw_context:
                        workflow_context_section = (
                            "Workflow context used during validation:\n"
                            f"{raw_context}\n\n"
                        )
                hint_prompt = (
                    f"Script with line numbers:\n{_format_numbered_code_block(code)}\n\n"
                    f"The context in which the solution was tested, analyze what happened in the code with this context, and why the error occurred:\n{workflow_context_section}\n\n"
                    f"Runtime error:\n{_clean_run_error(diagnostics['run_error'])}\n\n"
                    "What is the error, which line causes it, and what is the exact fix needed?"
                )
                logger.info(
                    f"[{_AGENT_VALIDATE_CODE}/llm.generate] hint calling",
                    code_len=len(code),
                )
                llm_hint = await llm.generate(
                    prompt=hint_prompt,
                    system=_VALIDATOR_HINT_SYSTEM,
                    temperature=0.05,
                    agent_name=_AGENT_VALIDATE_CODE,
                )
                diagnostics["llm_fix_hint"] = llm_hint.strip()
                logger.info(
                    f"[{_AGENT_VALIDATE_CODE}/llm.generate] hint done",
                    hint_len=len(diagnostics["llm_fix_hint"]),
                )
            except Exception as _hint_exc:
                logger.warning(
                    f"[{_AGENT_VALIDATE_CODE}] hint call failed",
                    error=str(_hint_exc),
                )
                diagnostics["llm_fix_hint"] = ""
        else:
            diagnostics["llm_fix_hint"] = ""

        logger.info(
            f"[{_AGENT_VALIDATE_CODE}] completed",
            passed=passed,
            failure_stage="" if passed else "validation",
        )
        return {
            "generated_code": code,
            "validation_passed": passed,
            "failure_stage": "" if passed else "validation",
            "diagnostics": diagnostics,
        }

    async def fix_validation_code(state: PipelineState) -> dict:
        """Fix Lua runtime/syntax errors (called after validate_code failure)."""
        code = _normalize_runtime_candidate(state.get("generated_code", ""))
        diagnostics = state.get("diagnostics", {})
        fix_iter = state.get("fix_iterations", 0)
        compiled_request = state.get("compiled_request", {}) if isinstance(state.get("compiled_request"), dict) else {}

        run_error = str(diagnostics.get("run_error", "") or "").strip()
        llm_fix_hint = str(diagnostics.get("llm_fix_hint", "") or "").strip()

        logger.info(
            f"[{_AGENT_FIX_VALIDATION_CODE}] started",
            iteration=fix_iter + 1,
            failure_kind=diagnostics.get("failure_kind", "unknown"),
            code_len=len(code),
            target_path=state.get("target_path", "(not set)"),
        )

        prompt = _build_fix_validation_prompt(
            code=code,
            run_error=run_error,
            llm_fix_hint=llm_fix_hint,
            compiled_request=compiled_request,
        )
        messages = [
            {"role": "system", "content": _FIX_VALIDATION_SYSTEM},
            {"role": "user", "content": prompt},
        ]
        logger.info(f"[{_AGENT_FIX_VALIDATION_CODE}/llm.chat] calling", temperature=0.0)
        raw = await llm.chat(
            messages,
            temperature=0.05,
            agent_name=_AGENT_FIX_VALIDATION_CODE,
        )
        fixed_analysis = validate_lowcode_llm_output(raw)
        fixed = fixed_analysis["normalized"]
        if not fixed and is_truncated_lowcode_response(raw):
            continued = await _attempt_continuation(raw, _AGENT_FIX_VALIDATION_CODE)
            if continued:
                cont_analysis = validate_lowcode_llm_output(continued)
                if cont_analysis["normalized"]:
                    fixed = cont_analysis["normalized"]
        if not fixed:
            fixed = code
        logger.info(
            f"[{_AGENT_FIX_VALIDATION_CODE}/llm.chat] done",
            raw_len=len(raw),
            fixed_len=len(fixed),
            valid=fixed_analysis["valid"],
        )

        # Internal retry: run diagnostics on the candidate; if still failing,
        # get a new LLM hint and make one more repair attempt.
        if fixed.strip() and _code_signature(fixed) != _code_signature(code):
            normalized_fixed = _normalize_runtime_candidate(fixed)
            candidate_diag = await _run_diagnostics_with_optional_context(
                normalized_fixed, compiled_request,
            )
            if not candidate_diag.get("success", False):
                candidate_error = str(candidate_diag.get("run_error", "") or "").strip()
                new_hint = llm_fix_hint
                if candidate_error:
                    try:
                        hint_prompt = (
                            f"Script with line numbers:\n{_format_numbered_code_block(normalized_fixed)}\n\n"
                            f"Runtime error:\n{_clean_run_error(candidate_error)}\n\n"
                            "What is the error, which line causes it, and what is the exact fix needed?"
                        )
                        new_hint_raw = await llm.generate(
                            prompt=hint_prompt,
                            system=_VALIDATOR_HINT_SYSTEM,
                            temperature=0.0,
                            agent_name=_AGENT_VALIDATE_CODE,
                        )
                        new_hint = new_hint_raw.strip()
                    except Exception:
                        pass
                retry_prompt = _build_fix_validation_prompt(
                    code=normalized_fixed,
                    run_error=candidate_error,
                    llm_fix_hint=new_hint,
                    compiled_request=compiled_request,
                )
                retry_messages = [
                    {"role": "system", "content": _FIX_VALIDATION_SYSTEM},
                    {"role": "user", "content": retry_prompt},
                ]
                logger.info(f"[{_AGENT_FIX_VALIDATION_CODE}/llm.chat] retry calling", temperature=0.0)
                retry_raw = await llm.chat(
                    retry_messages,
                    temperature=0.0,
                    agent_name=_AGENT_FIX_VALIDATION_CODE,
                )
                retry_fixed = validate_lowcode_llm_output(retry_raw)["normalized"]
                if retry_fixed:
                    fixed = retry_fixed
        elif _code_signature(fixed) == _code_signature(code):
            # Code did not change — one more attempt with explicit note.
            retry_prompt = _build_fix_validation_prompt(
                code=code,
                run_error=run_error,
                llm_fix_hint=(llm_fix_hint + "\n\nNote: the previous fix attempt returned unchanged code.").strip(),
                compiled_request=compiled_request,
            )
            logger.info(f"[{_AGENT_FIX_VALIDATION_CODE}/llm.chat] retry calling (unchanged)", temperature=0.0)
            retry_raw = await llm.chat(
                [{"role": "system", "content": _FIX_VALIDATION_SYSTEM}, {"role": "user", "content": retry_prompt}],
                temperature=0.0,
                agent_name=_AGENT_FIX_VALIDATION_CODE,
            )
            retry_fixed = validate_lowcode_llm_output(retry_raw)["normalized"]
            if retry_fixed:
                fixed = retry_fixed

        logger.info(f"[{_AGENT_FIX_VALIDATION_CODE}] completed", iteration=fix_iter + 1, code_len=len(fixed))
        return {
            "generated_code": fixed,
            "compiled_request": compiled_request,
            "fix_iterations": fix_iter + 1,
            "failure_stage": "",
            "validation_passed": False,
            "verification_passed": False,
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

    async def fix_verification_code(state: PipelineState) -> dict:
        """Fix logic/requirements failures (called after verify_requirements failure)."""
        code = _normalize_runtime_candidate(state.get("generated_code", ""))
        verification = state.get("verification", {})
        diagnostics = state.get("diagnostics", {})
        fix_iter = state.get("fix_verification_iterations", 0)
        compiled_request = state.get("compiled_request", {}) if isinstance(state.get("compiled_request"), dict) else {}

        missing_requirements = verification.get("missing_requirements", []) if isinstance(verification, dict) else []
        verification_summary = str(verification.get("summary", "") or "").strip() if isinstance(verification, dict) else ""
        runtime_result = str(diagnostics.get("result_preview", "") or "").strip()
        workflow_state_value = diagnostics.get("workflow_state")
        workflow_state = ""
        if workflow_state_value is not None:
            workflow_state = _compact_json_for_prompt(workflow_state_value)

        logger.info(
            f"[{_AGENT_FIX_VERIFICATION_CODE}] started",
            iteration=fix_iter + 1,
            missing_count=len(missing_requirements),
            code_len=len(code),
        )

        prompt = _build_fix_verification_prompt(
            code=code,
            verification_summary=verification_summary,
            missing_requirements=missing_requirements,
            runtime_result=runtime_result,
            workflow_state=workflow_state,
            compiled_request=compiled_request,
        )
        messages = [
            {"role": "system", "content": _FIX_VERIFICATION_SYSTEM},
            {"role": "user", "content": prompt},
        ]
        logger.info(f"[{_AGENT_FIX_VERIFICATION_CODE}/llm.chat] calling", temperature=0.0)
        raw = await llm.chat(
            messages,
            temperature=0.05,
            agent_name=_AGENT_FIX_VERIFICATION_CODE,
        )
        fixed_analysis = validate_lowcode_llm_output(raw)
        fixed = fixed_analysis["normalized"]
        if not fixed and is_truncated_lowcode_response(raw):
            continued = await _attempt_continuation(raw, _AGENT_FIX_VERIFICATION_CODE)
            if continued:
                cont_analysis = validate_lowcode_llm_output(continued)
                if cont_analysis["normalized"]:
                    fixed = cont_analysis["normalized"]
        if not fixed:
            fixed = code
        logger.info(
            f"[{_AGENT_FIX_VERIFICATION_CODE}/llm.chat] done",
            raw_len=len(raw),
            fixed_len=len(fixed),
            valid=fixed_analysis["valid"],
        )

        # Internal retry if the candidate is empty or unchanged.
        if not fixed.strip() or _code_signature(fixed) == _code_signature(code):
            note = (
                "The previous fix attempt returned unchanged code."
                if fixed.strip()
                else "The previous fix attempt returned empty code."
            )
            retry_prompt = _build_fix_verification_prompt(
                code=code,
                verification_summary=verification_summary,
                missing_requirements=missing_requirements + [note],
                runtime_result=runtime_result,
                workflow_state=workflow_state,
                compiled_request=compiled_request,
            )
            logger.info(f"[{_AGENT_FIX_VERIFICATION_CODE}/llm.chat] retry calling", temperature=0.0)
            retry_raw = await llm.chat(
                [{"role": "system", "content": _FIX_VERIFICATION_SYSTEM}, {"role": "user", "content": retry_prompt}],
                temperature=0.0,
                agent_name=_AGENT_FIX_VERIFICATION_CODE,
            )
            retry_fixed = validate_lowcode_llm_output(retry_raw)["normalized"]
            if retry_fixed:
                fixed = retry_fixed

        logger.info(f"[{_AGENT_FIX_VERIFICATION_CODE}] completed", iteration=fix_iter + 1, code_len=len(fixed))
        return {
            "generated_code": fixed,
            "compiled_request": compiled_request,
            "fix_verification_iterations": fix_iter + 1,
            "failure_stage": "",
            "validation_passed": True,
            "verification_passed": False,
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
        diagnostics = state.get("diagnostics", {})
        compiled_request = state.get("compiled_request", {})
        # The verifier must never see raw user_input/base_prompt. Use planner-processed
        # task_text via compiled_request["verification_prompt"]; fall back only to other
        # compiled_request fields (never to state["user_input"]).
        verification_prompt = ""
        if isinstance(compiled_request, dict):
            verification_prompt = str(compiled_request.get("verification_prompt", "") or "").strip()
            if not verification_prompt:
                verification_prompt = str(compiled_request.get("task_text", "") or "").strip()
            if not verification_prompt:
                verification_prompt = str(compiled_request.get("original_task", "") or "").strip()
        verification_extra_context = _build_verification_extra_context(
            compiled_request if isinstance(compiled_request, dict) else {},
            diagnostics if isinstance(diagnostics, dict) else {},
        )

        logger.info(
            f"[{_AGENT_VERIFY_REQUIREMENTS}] started",
            code_len=len(code),
            verification_prompt_len=len(verification_prompt),
            selected_operation=compiled_request.get("selected_operation", "") if isinstance(compiled_request, dict) else "",
            selected_primary_path=compiled_request.get("selected_primary_path", "") if isinstance(compiled_request, dict) else "",
        )

        try:
            logger.info(
                f"[{_AGENT_VERIFY_REQUIREMENTS}/async_verify_requirements] calling",
                prompt_len=len(verification_prompt),
                has_run_output=bool(diagnostics.get("run_output", "")),
                has_runtime_result=bool(isinstance(diagnostics, dict) and diagnostics.get("result_preview", "")),
            )
            verification = await async_verify_requirements(
                llm,
                prompt=verification_prompt,
                code=code,
                run_output=diagnostics.get("run_output", ""),
                extra_context=verification_extra_context,
            )
            logger.info(
                f"[{_AGENT_VERIFY_REQUIREMENTS}/async_verify_requirements] done",
                passed=verification.get("passed"),
                missing=verification.get("missing_requirements", []),
            )
            verification["error"] = False
        except Exception as exc:
            logger.warning(
                f"[{_AGENT_VERIFY_REQUIREMENTS}/async_verify_requirements] failed",
                error=str(exc),
            )
            verification = {
                "passed": False,
                "summary": f"LLM verification unavailable: {exc}",
                "missing_requirements": ["Semantic verification unavailable or invalid response."],
                "warnings": ["verification_unavailable"],
                "error": True,
            }

        llm_passed = bool(verification.get("passed"))
        combined_missing = [
            str(item).strip()
            for item in verification.get("missing_requirements", [])
            if str(item).strip()
        ]
        combined_warnings = [
            str(item).strip()
            for item in verification.get("warnings", [])
            if str(item).strip()
        ]
        llm_summary = str(verification.get("summary", "")).strip()
        compiled_request_summary = ""
        if isinstance(compiled_request, dict) and compiled_request:
            compiled_request_summary = (
                "Compiled request: "
                f"parseable_context={bool(compiled_request.get('has_parseable_context', False))}, "
                f"selected_operation={compiled_request.get('selected_operation', 'llm')}, "
                f"selected_path={compiled_request.get('selected_primary_path', 'none') or 'none'}, "
                f"needs_clarification={bool(compiled_request.get('needs_clarification', False))}"
            )
        summary_parts = [part for part in (compiled_request_summary, llm_summary) if part]
        combined_summary = " ".join(part for part in summary_parts if part).strip()
        passed = not verification.get("error") and llm_passed and not combined_missing
        verification = {
            **verification,
            "passed": passed,
            "summary": combined_summary or llm_summary or "Verification completed.",
            "missing_requirements": combined_missing,
            "warnings": combined_warnings,
            "selected_operation": compiled_request.get("selected_operation", "") if isinstance(compiled_request, dict) else "",
            "selected_primary_path": compiled_request.get("selected_primary_path", "") if isinstance(compiled_request, dict) else "",
            "needs_clarification": compiled_request.get("needs_clarification", False) if isinstance(compiled_request, dict) else False,
            "has_parseable_context": compiled_request.get("has_parseable_context", False) if isinstance(compiled_request, dict) else False,
        }
        diagnostics_updated = dict(diagnostics)
        diagnostics_updated["verification_summary"] = verification.get("summary", "")
        diagnostics_updated["verification_checked"] = not verification.get("error", False)
        diagnostics_updated["verification_passed"] = passed
        diagnostics_updated["selected_operation"] = verification.get("selected_operation", "")
        diagnostics_updated["selected_primary_path"] = verification.get("selected_primary_path", "")
        diagnostics_updated["has_parseable_context"] = verification.get("has_parseable_context", False)
        diagnostics_updated["failure_kind"] = "requirements" if not passed else diagnostics.get("failure_kind", "")

        logger.info(
            f"[{_AGENT_VERIFY_REQUIREMENTS}] completed",
            passed=passed,
            failure_stage="" if passed else "requirements",
        )
        return {
            "verification": verification,
            "verification_passed": passed,
            "compiled_request": compiled_request,
            "failure_stage": "" if passed else "requirements",
            "diagnostics": diagnostics_updated,
        }

    async def save_code(state: PipelineState) -> dict:
        code = _normalize_runtime_candidate(state.get("generated_code", ""))
        target_path = state.get("target_path", "")
        compiled_request = state.get("compiled_request", {})
        validation_passed = bool(state.get("validation_passed", False))
        verification_passed = bool(state.get("verification_passed", False))

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
        if not validation_passed:
            logger.warning(f"[{_AGENT_SAVE_CODE}] validation failed — skipping save")
            return {
                "current_code": code,
                "save_success": False,
                "save_skipped": True,
                "save_skip_reason": "Код показан в ответе, но не сохранен в файл, потому что не прошел валидацию.",
                "save_error": "",
                "saved_to": "",
                "saved_jsonstring_to": "",
            }
        if not verification_passed:
            logger.warning(f"[{_AGENT_SAVE_CODE}] verification failed — skipping save")
            return {
                "current_code": code,
                "save_success": False,
                "save_skipped": True,
                "save_skip_reason": "Код показан в ответе, но не сохранен в файл, потому что не прошел проверку требований.",
                "save_error": "",
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
        compiled_request = state.get("compiled_request", {})
        # Explainer should stay close to the user's original wording and language.
        user_request_text = ""
        if isinstance(compiled_request, dict):
            user_request_text = str(compiled_request.get("original_task", "") or "").strip()
            if not user_request_text:
                user_request_text = str(compiled_request.get("task_text", "") or "").strip()
        if not user_request_text:
            # Pre-planner fallback: only hit when pipeline skipped both planner and compiler.
            user_request_text = user_input

        explain_prompt = (
            f"user_request: {user_request_text}\n\n"
            "Explain the current Lua script for the user.\n"
            "Use concise, concrete phrasing tied to the actual code.\n"
            "If validation or verification reported problems, describe the current script as-is instead of pretending it is final.\n"
            "Return JSON only.\n\n"
            "Runtime validation summary:\n"
            f"- run_error: {diagnostics.get('run_error', 'none')}\n"
            f"- verification_summary: {verification.get('summary', 'none')}\n\n"
            "Code:\n"
            f"{format_lowcode_jsonstring(code)}\n\n"
            "Return JSON only. Write all text values in the same language as user_request."
        )

        try:
            logger.info(
                f"[{_AGENT_EXPLAIN}/llm.generate_json] calling",
                prompt_len=len(explain_prompt),
                temperature=0.1,
            )
            raw = await llm.generate_json(
                explain_prompt,
                system=_EXPLAIN_SYSTEM,
                temperature=0.1,
                agent_name=_AGENT_EXPLAIN,
            )
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
        answer = await llm.generate(
            prompt=prompt,
            system=_ANSWER_SYSTEM,
            agent_name=_AGENT_ANSWER,
        )
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
        code = _normalize_runtime_candidate(state.get("generated_code", ""))
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

        if save_skipped and not (state.get("validation_passed") and state.get("verification_passed")):
            if lines:
                lines[0] = "Код подготовлен, но финальные условия сохранения не выполнены.\n"
            if failure_stage:
                stage_line = f"Этап с проблемой: {failure_stage}\n"
                if stage_line not in lines:
                    lines.insert(1 if lines else 0, stage_line)

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
        "fix_validation_code": fix_validation_code,
        "fix_verification_code": fix_verification_code,
        "verify_requirements": verify_requirements,
        "save_code": save_code,
        "explain_solution": explain_solution,
        "answer_question": answer_question,
        "prepare_response": prepare_response,
    }
