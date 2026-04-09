"""Shared agent state definition for the LangGraph pipeline."""

from __future__ import annotations

from typing import Any, TypedDict


class Message(TypedDict):
    role: str   # "user" | "assistant" | "system"
    content: str


class AgentState(TypedDict):
    # ── Conversation ──────────────────────────────────────────────
    chat_id: str
    messages: list[Message]          # full chat history (trimmed to max_chat_history)
    user_input: str                  # latest user message

    # ── Planning / routing ────────────────────────────────────────
    intent: str                      # determined by router: "generate" | "explain" | "fix" | "qa"
    plan: str                        # high-level plan produced by planner agent

    # ── Code generation ───────────────────────────────────────────
    generated_code: str              # current Lua code candidate
    fix_iterations: int              # how many fix attempts have been made

    # ── Validation ────────────────────────────────────────────────
    validation_passed: bool
    validation_errors: str           # stderr / error message from lua54

    # ── RAG context ───────────────────────────────────────────────
    rag_context: str                 # retrieved Lua documentation snippets

    # ── Final answer ──────────────────────────────────────────────
    response: str                    # text response sent back to the user

    # ── Metadata ──────────────────────────────────────────────────
    metadata: dict[str, Any]         # arbitrary extra data (agent diagnostics, etc.)
