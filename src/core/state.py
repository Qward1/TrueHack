"""LangGraph pipeline state definition for the canonical Lua runtime."""

from __future__ import annotations

from typing import Any, TypedDict


class PipelineState(TypedDict):
    """Full state that flows through every LangGraph node."""

    # Conversation
    chat_id: int
    user_input: str

    # Lua target resolution
    workspace_root: str
    target_path: str
    target_directory: str
    target_explicit: bool

    # Routing and task context
    intent: str
    base_prompt: str
    change_requests: list[str]
    compiled_request: dict[str, Any]

    # Code
    current_code: str
    generated_code: str

    # Validation / fix loop
    failure_stage: str
    diagnostics: dict[str, Any]
    validation_passed: bool
    fix_iterations: int          # validation-fix counter
    fix_verification_iterations: int  # verification-fix counter
    max_fix_iterations: int

    # Verification / save state
    verification: dict[str, Any]
    verification_passed: bool
    save_success: bool
    save_skipped: bool
    save_skip_reason: str
    save_error: str
    saved_to: str
    saved_jsonstring_to: str

    # Explanation for user
    explanation: dict[str, Any]
    suggested_changes: list[str]
    clarifying_questions: list[str]

    # Standalone verification chain preparation state
    previous_fix_attempts: list[dict[str, Any]]
    verification_chain_current_verifier: str
    verification_chain_current_node: str
    verification_chain_current_index: int
    verification_chain_current_failure_stage: str
    verification_chain_next_verifier: str
    verification_chain_next_node: str
    verification_chain_last_transition: str
    verification_chain_stage_fix_counts: dict[str, int]
    verification_chain_stage_fix_limits: dict[str, int]
    verification_chain_stage_results: dict[str, dict[str, Any]]
    verification_chain_history: list[dict[str, Any]]

    # User-visible output
    response: str
    response_type: str

    # Planner state
    planner_result: dict[str, Any]
    planner_skipped: bool
    awaiting_planner_clarification: bool
    planner_pending_questions: list[str]
    planner_original_input: str
    planner_clarification_attempts: int
