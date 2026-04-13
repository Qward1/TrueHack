"""Conditional edge functions for the LangGraph pipeline."""

from __future__ import annotations

import structlog

from src.core.state import PipelineState

logger = structlog.get_logger(__name__)


def route_from_start(state: PipelineState) -> str:
    """START router: bypass resolve_target/route_intent if planner awaits clarification."""
    if state.get("awaiting_planner_clarification"):
        return "planner_followup"
    return "normal"


def route_after_planning(state: PipelineState) -> str:
    """After plan_request -> respond with clarification or continue to compiler."""
    if state.get("awaiting_planner_clarification"):
        return "clarify"
    return "continue"


def route_by_intent(state: PipelineState) -> str:
    """After intent classification -> choose the next high-level step.

    Returns: "prepare" | "answer"
    """
    intent = state.get("intent", "")
    if intent == "create":
        return "prepare"
    if intent in ("change", "retry"):
        return "prepare"
    # question / inspect / general
    return "answer"


def route_after_preparation(state: PipelineState) -> str:
    """After generation-context compilation -> clarify, generate, or refine."""
    compiled_request = state.get("compiled_request", {})
    if isinstance(compiled_request, dict) and compiled_request.get("needs_clarification"):
        return "clarify"

    intent = state.get("intent", "")
    has_existing_code = bool(str(state.get("current_code", "") or "").strip())
    if intent in ("change", "retry") and has_existing_code:
        return "refine"
    return "generate"


def check_validation(state: PipelineState) -> str:
    """After local validation:
    - pass  -> verify
    - fail, iterations left -> fix_validation
    - fail, max iterations reached -> log and continue to save anyway
    """
    if state.get("validation_passed"):
        return "verify"

    fix_iter = state.get("fix_iterations", 0)
    max_fix = state.get("max_fix_iterations", 3)
    if fix_iter < max_fix:
        return "fix_validation"

    logger.warning(
        "[ValidationFixLoop] max_iterations_reached_continuing_to_save",
        fix_iterations=fix_iter,
        failure_kind=state.get("diagnostics", {}).get("failure_kind", "unknown"),
    )
    return "save"
