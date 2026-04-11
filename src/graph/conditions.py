"""Conditional edge functions for the LangGraph pipeline."""

from __future__ import annotations

from src.core.state import PipelineState


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
    if intent in ("change", "retry"):
        return "refine"
    return "generate"


def check_validation(state: PipelineState) -> str:
    """After local validation -> verify, fix, or respond with failure."""
    if state.get("validation_passed"):
        return "verify"

    fix_iter = state.get("fix_iterations", 0)
    max_fix = state.get("max_fix_iterations", 5)
    if fix_iter < max_fix:
        return "fix"
    return "force_respond"


def check_verification(state: PipelineState) -> str:
    """After requirement verification -> save, fix, or fail."""
    verification = state.get("verification", {})
    missing_requirements = verification.get("missing_requirements", [])
    fix_iter = state.get("fix_iterations", 0)
    max_fix = state.get("max_fix_iterations", 5)

    # If semantic verification is unavailable, allow saving only when deterministic guards passed.
    if verification.get("error"):
        if not missing_requirements:
            return "save"
        if fix_iter < max_fix:
            return "fix"
        return "force_respond"

    if missing_requirements:
        if fix_iter < max_fix:
            return "fix"
        return "force_respond"

    passed = verification.get("passed", False)
    score = verification.get("score", 0)
    if passed or score >= 70:
        return "save"

    if fix_iter < max_fix:
        return "fix"
    return "force_respond"
