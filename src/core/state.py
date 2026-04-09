"""Shared agent state definition for the LangGraph pipeline."""

from __future__ import annotations

from typing import Any, NotRequired, TypedDict


class Message(TypedDict):
    role: str   # "user" | "assistant" | "system"
    content: str


class AgentState(TypedDict):
    # ── Conversation ──────────────────────────────────────────────
    chat_id: str
    messages: list[Message]          # full chat history (trimmed to max_chat_history)
    user_input: str                  # latest user message

    # ── Planning / routing ────────────────────────────────────────
    intent: str                      # router output: see router.txt categories
    plan: list[dict]                 # structured tasks from planner: [{id, description, function_name, signature, dependencies}]
    current_task_index: int          # which task in plan is being processed
    task_description: str            # description of the current task (for prompts)
    clarification_questions: NotRequired[list[str]]  # from planner_clarify

    # ── Code generation ───────────────────────────────────────────
    generated_code: str              # current Lua code candidate (latest)
    generated_codes: dict[str, str]  # task_id -> generated Lua code
    assembled_code: str              # final assembled module from planner.assemble()
    fix_iterations: int              # how many fix attempts have been made

    # ── Validation ────────────────────────────────────────────────
    validation_passed: bool
    validation_errors: str           # combined error string (syntax + LLM review)

    # ── RAG context ───────────────────────────────────────────────
    rag_context: str                 # retrieved Lua documentation snippets

    # ── Final answer ──────────────────────────────────────────────
    response: str                    # text response sent back to the user
    response_type: str               # "text" | "code" | "clarification"

    # ── Metadata ──────────────────────────────────────────────────
    metadata: dict[str, Any]         # arbitrary extra data (agent diagnostics, etc.)
