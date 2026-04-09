"""Conditional edge functions for the LangGraph agent graph."""

from __future__ import annotations

from src.core.state import AgentState

_GENERATE_INTENTS = {"generate_clear", "generate_unclear"}
_REFINE_INTENTS = {"refine", "fix_error"}
_ANSWER_INTENTS = {"question", "explain", "general"}


def route_by_intent(state: AgentState) -> str:
    """Map routing intent to next node group.

    Returns one of: ``"plan"`` | ``"refine"`` | ``"answer"``
    """
    intent = state.get("intent", "")
    if intent in _GENERATE_INTENTS:
        return "plan"
    if intent in _REFINE_INTENTS:
        return "refine"
    # question / explain / general — and any unknown intent
    return "answer"


def check_plan_result(state: AgentState) -> str:
    """After planning: send clarification questions or start generating.

    Returns: ``"respond"`` | ``"generate"``
    """
    if state.get("clarification_questions"):
        return "respond"
    return "generate"


def has_more_tasks(state: AgentState) -> str:
    """After generating one task: loop or move to validation.

    Returns: ``"generate"`` | ``"validate"``
    """
    plan: list[dict] = state.get("plan") or []
    idx: int = state.get("current_task_index", 0)
    if idx < len(plan):
        return "generate"
    return "validate"


def check_validation(state: AgentState, max_fix_iterations: int = 3) -> str:
    """After validation: assemble, fix, or force-assemble.

    Returns: ``"assemble"`` | ``"fix"`` | ``"force_assemble"``
    """
    if state.get("validation_passed"):
        return "assemble"
    fix_iterations: int = state.get("fix_iterations", 0)
    if fix_iterations < max_fix_iterations:
        return "fix"
    return "force_assemble"
