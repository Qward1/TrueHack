"""LangGraph pipeline state definition."""

from __future__ import annotations

from typing import Any, TypedDict


class Message(TypedDict):
    role: str       # "user" | "assistant" | "system"
    content: str


class PipelineState(TypedDict):
    """Full state that flows through every LangGraph node."""

    # ── Conversation ─────────────────────────────────────────────────
    chat_id: int
    messages: list[Message]
    user_input: str

    # ── Routing ──────────────────────────────────────────────────────
    intent: str             # "create" | "change" | "inspect" | "retry" | "question"

    # ── Task context ─────────────────────────────────────────────────
    base_prompt: str        # original task description (accumulated)
    change_requests: list[str]
    output_path: str        # where to save the Lua file
    artifact_type: str      # "lua" | "readme" | "text"

    # ── Code ─────────────────────────────────────────────────────────
    current_code: str       # existing code before this turn
    generated_code: str     # new/edited code from LLM this turn

    # ── Validation ───────────────────────────────────────────────────
    diagnostics: dict[str, Any]
    validation_passed: bool

    # ── Fix loop ─────────────────────────────────────────────────────
    fix_iterations: int
    max_fix_iterations: int

    # ── Verification ─────────────────────────────────────────────────
    verification: dict[str, Any]
    verification_passed: bool

    # ── Response ─────────────────────────────────────────────────────
    response: str
    response_type: str      # "code" | "text" | "error"

    # ── Metadata ─────────────────────────────────────────────────────
    metadata: dict[str, Any]
