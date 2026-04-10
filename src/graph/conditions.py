"""Conditional edge functions for the LangGraph pipeline."""

from __future__ import annotations

from src.core.state import PipelineState


def route_by_intent(state: PipelineState) -> str:
    """After intent classification -> choose the next high-level step.

    Returns: "generate" | "refine" | "answer"
    """
    intent = state.get("intent", "")
    if intent == "create":
        return "generate"
    if intent in ("change", "retry"):
        return "refine"
    # question / inspect / general
    return "answer"


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
    """After requirement verification -> continue to e2e, fix, or fail."""
    verification = state.get("verification", {})

    # If semantic verification is unavailable, continue to e2e gate.
    if verification.get("error"):
        return "e2e"

    passed = verification.get("passed", False)
    score = verification.get("score", 0)
    if passed or score >= 70:
        return "e2e"

    fix_iter = state.get("fix_iterations", 0)
    max_fix = state.get("max_fix_iterations", 5)
    if fix_iter < max_fix:
        return "fix"
    return "force_respond"


def check_e2e(state: PipelineState) -> str:
    """After e2e execution -> save, fix, or respond with failure."""
    if state.get("e2e_passed"):
        return "save"

    e2e_results = state.get("e2e_results", {})
    if not e2e_results.get("retryable", True):
        return "force_respond"

    fix_iter = state.get("fix_iterations", 0)
    max_fix = state.get("max_fix_iterations", 5)
    if fix_iter < max_fix:
        return "fix"
    return "force_respond"
