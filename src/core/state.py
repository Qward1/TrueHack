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

    # Code
    current_code: str
    generated_code: str

    # Validation / fix loop
    diagnostics: dict[str, Any]
    validation_passed: bool
    fix_iterations: int
    max_fix_iterations: int

    # Verification / save state
    verification: dict[str, Any]
    verification_passed: bool
    save_success: bool
    save_error: str
    saved_to: str

    # User-visible output
    response: str
    response_type: str
