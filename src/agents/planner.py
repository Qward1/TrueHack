"""Standalone TaskPlanner agent for LLM-based request analysis and reformulation.

This module is NOT connected to the main LangGraph pipeline.
It can be toggled on/off via PLANNER_ENABLED env var or constructor param.

== Future integration ==

Insert as a node between route_intent and prepare_generation_context:

1. state.py: add planner_result (dict) and planner_skipped (bool)
2. nodes.py create_nodes(): add nodes["plan_request"] = create_planner_node(llm)
3. builder.py: route_intent -> plan_request -> prepare_generation_context
4. conditions.py: add route_after_planning (clarify | continue)
5. engine.py: add planner_result and planner_skipped to initial_state and output

The planner receives state after route_intent (user_input, intent, current_code,
base_prompt) and produces planner_result dict consumed by prepare_generation_context.
"""

from __future__ import annotations

import os
import re
from typing import Any, Callable, TypedDict

import structlog

from src.core.llm import LLMProvider

logger = structlog.get_logger(__name__)

# ---------------------------------------------------------------------------
# Toggle
# ---------------------------------------------------------------------------

_ENV_KEY = "PLANNER_ENABLED"


def _is_planner_enabled() -> bool:
    """Read PLANNER_ENABLED env var (default: false)."""
    return os.getenv(_ENV_KEY, "false").strip().lower() in ("true", "1", "yes")


# ---------------------------------------------------------------------------
# Prompt templates
# ---------------------------------------------------------------------------

_PLANNER_SYSTEM = (
    "You are a task analyst for a Lua workflow script generator. "
    "Your job is to analyze the user's request and reformulate it into a clear, "
    "unambiguous task description that a code generator can follow precisely. "
    "The target platform is LowCode with Lua 5.5 workflow scripts using wf.vars "
    "and wf.initVariables. "
    "If the request is unclear or ambiguous, identify what needs clarification. "
    "Always respond in the same language as the user's request. "
    "Return JSON only."
)

_PLANNER_USER = """Analyze this task request for Lua workflow code generation.

User request: {user_input}
Workflow context provided: {has_context}
Workflow paths found in request: {workflow_paths}
Has existing code to modify: {has_code}

Analyze and return JSON with these fields:
- "reformulated_task": rewrite the task as a clear, specific instruction for the code generator. Include exact workflow paths (wf.vars.X / wf.initVariables.X), the operation to perform, and how to return/store the result. If the task is already clear, keep it close to the original but add any implicit details.
- "identified_workflow_paths": list of wf.vars.* / wf.initVariables.* paths relevant to this task
- "target_operation": one of "extract", "transform", "filter", "increment", "convert", "validate", "filter_keys", "remove_keys", "custom"
- "key_entities": important field names, operations, or concepts from the request
- "data_types": map of workflow path to detected type ("string", "number", "array_string", "array_object", "object", "unknown")
- "expected_result_action": "return" or "save_to_wf_vars"
- "needs_clarification": true if the request is too ambiguous to generate correct code
- "clarification_questions": list of 1-3 questions if clarification is needed (empty list otherwise)
- "confidence": 0.0-1.0 confidence in the analysis

JSON only:"""


# ---------------------------------------------------------------------------
# Agent name for logging
# ---------------------------------------------------------------------------

_AGENT_NAME = "TaskPlanner"

# Maximum clarification rounds before planner forcibly continues into the pipeline.
MAX_CLARIFICATION_ATTEMPTS = 2


# ---------------------------------------------------------------------------
# Output types
# ---------------------------------------------------------------------------

class PlannerOutput(TypedDict, total=False):
    """Output contract for the planner node.

    When integrated into PipelineState, add these two fields:
        planner_result: dict[str, Any]
        planner_skipped: bool
    """

    planner_result: dict[str, Any]
    planner_skipped: bool


# Valid operations
_VALID_OPERATIONS = frozenset({
    "extract", "transform", "filter", "increment", "convert",
    "validate", "filter_keys", "remove_keys", "custom",
})

# Valid result actions
_VALID_RESULT_ACTIONS = frozenset({"return", "save_to_wf_vars"})


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_WF_PATH_RE = re.compile(r"wf\.(?:vars|initVariables)(?:\.\w+)+")


def _extract_workflow_paths_from_text(text: str) -> list[str]:
    """Extract wf.vars.* / wf.initVariables.* paths from user text."""
    if not text:
        return []
    return sorted(set(_WF_PATH_RE.findall(text)))


def _normalize_planner_result(raw: dict, user_input: str) -> dict[str, Any]:
    """Defensively normalize LLM output into a well-typed planner result.

    If the LLM returns garbage, falls back to safe defaults with
    the original user_input as reformulated_task.
    """
    if not isinstance(raw, dict):
        raw = {}

    reformulated = str(raw.get("reformulated_task", "") or "").strip()
    if not reformulated:
        reformulated = user_input.strip()

    paths = raw.get("identified_workflow_paths", [])
    if not isinstance(paths, list):
        paths = []
    paths = [str(p) for p in paths if isinstance(p, str) and p.strip()]

    operation = str(raw.get("target_operation", "custom") or "custom").strip().lower()
    if operation not in _VALID_OPERATIONS:
        operation = "custom"

    entities = raw.get("key_entities", [])
    if not isinstance(entities, list):
        entities = []
    entities = [str(e) for e in entities if isinstance(e, str) and e.strip()]

    data_types = raw.get("data_types", {})
    if not isinstance(data_types, dict):
        data_types = {}
    data_types = {str(k): str(v) for k, v in data_types.items() if isinstance(k, str) and isinstance(v, str)}

    result_action = str(raw.get("expected_result_action", "return") or "return").strip().lower()
    if result_action not in _VALID_RESULT_ACTIONS:
        result_action = "return"

    needs_clarification = bool(raw.get("needs_clarification", False))

    questions = raw.get("clarification_questions", [])
    if not isinstance(questions, list):
        questions = []
    questions = [str(q) for q in questions if isinstance(q, str) and q.strip()]
    # Cap at 3 questions max
    questions = questions[:3]

    try:
        confidence = float(raw.get("confidence", 0.0))
        confidence = max(0.0, min(1.0, confidence))
    except (TypeError, ValueError):
        confidence = 0.0

    return {
        "reformulated_task": reformulated,
        "identified_workflow_paths": paths,
        "target_operation": operation,
        "key_entities": entities,
        "data_types": data_types,
        "expected_result_action": result_action,
        "needs_clarification": needs_clarification,
        "clarification_questions": questions,
        "confidence": confidence,
    }


def _build_clarification_response(planner_result: dict[str, Any]) -> str:
    """Build a user-facing clarification response from planner output."""
    questions = planner_result.get("clarification_questions", [])
    if not questions:
        return "Уточните, пожалуйста, вашу задачу подробнее."

    parts = ["Для точной генерации кода мне нужно уточнить:"]
    for i, q in enumerate(questions, 1):
        parts.append(f"{i}. {q}")
    return "\n".join(parts)


# ---------------------------------------------------------------------------
# PlannerAgent
# ---------------------------------------------------------------------------

class PlannerAgent:
    """LLM-based task analyst and reformulator.

    Always runs for code generation tasks (create/change/retry).
    If the request is clear, reformulates it with added precision.
    If ambiguous, returns clarification questions.
    """

    def __init__(self, llm: LLMProvider, *, enabled: bool | None = None) -> None:
        self._llm = llm
        self._enabled = enabled if enabled is not None else _is_planner_enabled()

    @property
    def enabled(self) -> bool:
        return self._enabled

    async def plan(
        self,
        *,
        user_input: str,
        has_context: bool = False,
        workflow_paths: list[str] | None = None,
        intent: str = "",
        current_code: str = "",
    ) -> dict[str, Any]:
        """Analyze user request and produce a structured planning result.

        Returns a dict with keys: planner_result, planner_skipped.

        When disabled, returns planner_skipped=True immediately with no LLM call.
        Otherwise, always calls the LLM to analyze the request — never skipped
        for code generation intents.
        """
        if not self._enabled:
            logger.info(f"[{_AGENT_NAME}] disabled, skipping")
            return {"planner_result": {}, "planner_skipped": True}

        logger.info(
            f"[{_AGENT_NAME}] started",
            user_input_len=len(user_input),
            intent=intent,
            has_context=has_context,
            has_code=bool(current_code.strip() if current_code else False),
        )

        # Extract workflow paths from user text if not provided
        detected_paths = workflow_paths or _extract_workflow_paths_from_text(user_input)

        prompt = _PLANNER_USER.format(
            user_input=user_input,
            has_context=str(has_context).lower(),
            workflow_paths=", ".join(detected_paths) if detected_paths else "none",
            has_code=str(bool(current_code.strip() if current_code else False)).lower(),
        )

        logger.info(
            f"[{_AGENT_NAME}/llm.generate_json] calling",
            prompt_len=len(prompt),
            system_len=len(_PLANNER_SYSTEM),
        )

        raw = await self._llm.generate_json(prompt, system=_PLANNER_SYSTEM)

        logger.info(
            f"[{_AGENT_NAME}/llm.generate_json] done",
            raw_keys=list(raw.keys()) if raw else [],
            raw_confidence=raw.get("confidence", "n/a"),
        )

        result = _normalize_planner_result(raw, user_input)

        logger.info(
            f"[{_AGENT_NAME}] completed",
            needs_clarification=result["needs_clarification"],
            target_operation=result["target_operation"],
            confidence=result["confidence"],
            num_paths=len(result["identified_workflow_paths"]),
            num_questions=len(result["clarification_questions"]),
        )

        return {"planner_result": result, "planner_skipped": False}


# ---------------------------------------------------------------------------
# LangGraph node factory
# ---------------------------------------------------------------------------

def create_planner_node(llm: LLMProvider) -> Callable:
    """Factory that returns a LangGraph-compatible async node function.

    The returned callable accepts PipelineState and returns a dict
    with planner_result and planner_skipped keys.

    When the planner detects that clarification is needed, it also sets
    response, response_type, failure_stage, and clarifying_questions so
    the graph can route directly to prepare_response -> END.
    """
    agent = PlannerAgent(llm, enabled=None)  # reads env var

    async def plan_request(state: dict) -> dict:
        """LangGraph node: analyze and reformulate the user's task.

        Supports a clarification follow-up flow:
          - If state.awaiting_planner_clarification is True, the user's current
            input is treated as an answer to previously posed questions. The
            planner receives a merged input combining the original task,
            previous questions, and the user's answer.
          - When attempts hit MAX_CLARIFICATION_ATTEMPTS, planner forcibly
            continues into the rest of the pipeline.
        """
        user_input = state.get("user_input", "")
        intent = state.get("intent", "")
        current_code = state.get("current_code", "")
        compiled_request = state.get("compiled_request", {})

        awaiting = bool(state.get("awaiting_planner_clarification"))
        original_input = str(state.get("planner_original_input", "") or "")
        pending_questions = list(state.get("planner_pending_questions", []) or [])
        attempts = int(state.get("planner_clarification_attempts", 0) or 0)

        # Build the input the planner actually sees.
        if awaiting and original_input:
            question_block = "\n".join(f"- {q}" for q in pending_questions if q)
            effective_input = (
                f"Исходная задача: {original_input}\n\n"
                f"Уточняющие вопросы:\n{question_block}\n\n"
                f"Ответ пользователя: {user_input}"
            ).strip()
        else:
            effective_input = user_input

        has_context = bool(
            isinstance(compiled_request, dict)
            and compiled_request.get("has_parseable_context")
        )

        plan_output = await agent.plan(
            user_input=effective_input,
            has_context=has_context,
            intent=intent,
            current_code=current_code,
        )

        planner_result = plan_output.get("planner_result", {})
        planner_skipped = plan_output.get("planner_skipped", False)

        forced_continue = attempts >= MAX_CLARIFICATION_ATTEMPTS
        wants_clarification = (
            not planner_skipped
            and planner_result.get("needs_clarification")
            and not forced_continue
        )

        if wants_clarification:
            response = _build_clarification_response(planner_result)
            preserved_original = original_input or user_input
            logger.info(
                f"[{_AGENT_NAME}] clarification_requested",
                attempts=attempts + 1,
                question_count=len(planner_result.get("clarification_questions", [])),
            )
            return {
                "planner_result": planner_result,
                "planner_skipped": False,
                "awaiting_planner_clarification": True,
                "planner_pending_questions": planner_result.get("clarification_questions", []),
                "planner_original_input": preserved_original,
                "planner_clarification_attempts": attempts + 1,
                "response": response,
                "response_type": "text",
                "failure_stage": "clarification",
                "clarifying_questions": planner_result.get("clarification_questions", []),
                "verification": {},
                "verification_passed": False,
                "save_success": False,
                "save_skipped": False,
                "save_skip_reason": "",
                "save_error": "",
            }

        if forced_continue and planner_result.get("needs_clarification"):
            logger.info(
                f"[{_AGENT_NAME}] forced_continue_after_max_attempts",
                attempts=attempts,
            )
            planner_result = dict(planner_result)
            planner_result["needs_clarification"] = False
            planner_result["clarification_questions"] = []

        # When continuing (either no clarification, or forced continue), rewrite
        # state.user_input to the merged effective input so the rest of the
        # pipeline (compile_lowcode_request, etc.) sees the full task context.
        updates: dict[str, Any] = {
            "planner_result": planner_result,
            "planner_skipped": planner_skipped,
            "awaiting_planner_clarification": False,
            "planner_pending_questions": [],
            "planner_original_input": "",
            "planner_clarification_attempts": 0,
        }
        if awaiting and original_input:
            updates["user_input"] = effective_input
        return updates

    return plan_request
