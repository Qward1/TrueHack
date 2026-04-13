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
    "You are a task analyst for a LowCode Lua 5.5 workflow script generator. "
    "Convert the user's request into a precise JSON task description for code generation. "

    "Platform rules: "
    "workflow input variables are in wf.initVariables; "
    "workflow variables are in wf.vars; "
    "access data only through direct paths like wf.vars.x.y or wf.initVariables.x; "
    "never use JsonPath. "

    "Strict rules: "
    "Never invent new workflow paths, variable names, or output fields. "
    "Use only paths explicitly present in the request or provided JSON context. "
    "If no destination path is explicitly provided, do not create one. "
    "For extract/transform/convert requests without a destination path, prefer returning the result. "
    "Use save_to_wf_vars only when saving/updating is explicitly requested or an explicit target wf.vars path is given. "
    "Set needs_clarification to true only when required information is missing and code cannot be generated correctly. "
    "identified_workflow_paths must include only explicitly found workflow paths. "
    "If assumptions are needed, reduce confidence. "

    "Always answer in the same language as the user's request. "
    "Return valid JSON only."
)

_PLANNER_USER = """Analyze this task request for Lua workflow code generation.

User request:
{user_input}

Metadata:
- Workflow context provided: {has_context}
- Workflow paths found in request: {workflow_paths}
- Has existing code to modify: {has_code}
- Active clarification questions for current code: {active_clarifying_questions}

Return JSON with exactly these fields:
- "reformulated_task": rewrite the task as a clear instruction for the code generator; include only explicit workflow paths; do not invent destination fields
- "identified_workflow_paths": only explicitly present wf.vars.* / wf.initVariables.* paths relevant to the task
- "target_operation": one of "extract", "transform", "filter", "increment", "convert", "validate", "filter_keys", "remove_keys", "custom"
- "key_entities": important field names, operations, or concepts from the request
- "data_types": map of workflow path to detected type; values only "string", "number", "array_string", "array_object", "object", "unknown"
- "expected_result_action": "return" if no explicit destination path is given; "save_to_wf_vars" only if saving/updating is explicitly requested or a target wf.vars path is explicitly given
- "followup_action": one of "none", "refine_existing_code", "start_new_generation"
- "needs_clarification": true only if required information is missing and correct code generation is impossible
- "clarification_questions": 1-3 short questions if clarification is needed, otherwise []
- "confidence": number from 0.0 to 1.0; reduce it if any assumption is needed

Rules:
1. Do not invent new paths.
2. Do not invent new output field names.
3. If destination is missing, prefer "return".
4. Use only information from the request and provided context.
5. If there is existing code and active clarification questions, decide whether the user message is:
   - an answer that should refine the existing code, or
   - a logically new request that should start a fresh generation.
6. Use "refine_existing_code" only when the user is clearly answering or narrowing the previous clarification questions for the current code.
7. Use "start_new_generation" only when the user is clearly asking for a new logical task rather than refining the current code.
5. JSON only.

JSON only."""


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


# Valid result actions
_VALID_RESULT_ACTIONS = frozenset({"return", "save_to_wf_vars"})
_VALID_FOLLOWUP_ACTIONS = frozenset({"none", "refine_existing_code", "start_new_generation"})


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

    entities = raw.get("key_entities", [])
    if not isinstance(entities, list):
        entities = []
    entities = [str(e) for e in entities if isinstance(e, str) and e.strip()]

    result_action = str(raw.get("expected_result_action", "return") or "return").strip().lower()
    if result_action not in _VALID_RESULT_ACTIONS:
        result_action = "return"

    followup_action = str(raw.get("followup_action", "none") or "none").strip().lower()
    if followup_action not in _VALID_FOLLOWUP_ACTIONS:
        followup_action = "none"

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
        "key_entities": entities,
        "expected_result_action": result_action,
        "followup_action": followup_action,
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
        active_clarifying_questions: list[str] | None = None,
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
        active_questions = [
            str(question).strip()
            for question in (active_clarifying_questions or [])
            if str(question).strip()
        ]

        prompt = _PLANNER_USER.format(
            user_input=user_input,
            has_context=str(has_context).lower(),
            workflow_paths=", ".join(detected_paths) if detected_paths else "none",
            has_code=str(bool(current_code.strip() if current_code else False)).lower(),
            active_clarifying_questions=" | ".join(active_questions) if active_questions else "none",
        )

        logger.info(
            f"[{_AGENT_NAME}/llm.generate_json] calling",
            prompt_len=len(prompt),
            system_len=len(_PLANNER_SYSTEM),
        )

        raw = await self._llm.generate_json(
            prompt,
            system=_PLANNER_SYSTEM,
            agent_name=_AGENT_NAME,
        )

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
            followup_action=result["followup_action"],
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
        active_clarifying_questions = [
            str(question).strip()
            for question in state.get("active_clarifying_questions", []) or []
            if str(question).strip()
        ]

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
            active_clarifying_questions=active_clarifying_questions,
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
        followup_action = str(planner_result.get("followup_action", "none") or "none").strip().lower()
        if followup_action == "refine_existing_code" and current_code.strip():
            updates["intent"] = "change"
        elif followup_action == "start_new_generation":
            updates["intent"] = "create"
            updates["base_prompt"] = ""
            updates["change_requests"] = []
        if awaiting and original_input:
            updates["user_input"] = effective_input
        return updates

    return plan_request
