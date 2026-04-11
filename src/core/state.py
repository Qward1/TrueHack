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
    fix_iterations: int
    max_fix_iterations: int

    # Verification / e2e / save state
    verification: dict[str, Any]
    verification_passed: bool
    e2e_suite: dict[str, Any]
    e2e_results: dict[str, Any]
    e2e_passed: bool
    save_success: bool
    save_error: str
    saved_to: str
    saved_jsonstring_to: str

    # Explanation for user
    explanation: dict[str, Any]
    suggested_changes: list[str]
    clarifying_questions: list[str]

    # User-visible output
    response: str
    response_type: str
