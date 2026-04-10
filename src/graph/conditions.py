"""Conditional edge functions for the LangGraph pipeline."""

from __future__ import annotations

from src.core.state import PipelineState


def route_by_intent(state: PipelineState) -> str:
    """After intent classification → choose next step.

    Returns: "generate" | "refine" | "answer" | "retry"
    """
    intent = state.get("intent", "")
    if intent == "create":
        return "generate"
    if intent in ("change", "retry"):
        return "refine"
    # question / inspect / general
    return "answer"


def check_validation(state: PipelineState) -> str:
    """After validation → decide what to do next.

    Returns: "verify" | "fix" | "force_respond"
    """
    if state.get("validation_passed"):
        return "verify"
    fix_iter = state.get("fix_iterations", 0)
    max_fix = state.get("max_fix_iterations", 5)
    if fix_iter < max_fix:
        return "fix"
    return "force_respond"


def check_verification(state: PipelineState) -> str:
    """After verification → respond or fix.

    Returns: "respond" | "fix"
    """
    verification = state.get("verification", {})
    if verification.get("error"):
        return "respond"

    passed = verification.get("passed", False)
    score = verification.get("score", 0)

    # Soft-pass: accept if score >= 70 even when passed=False
    if passed or score >= 70:
        return "respond"

    fix_iter = state.get("fix_iterations", 0)
    max_fix = state.get("max_fix_iterations", 5)
    if fix_iter < max_fix:
        return "fix"
    return "respond"
