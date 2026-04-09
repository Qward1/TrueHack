"""Pydantic request/response schemas for the FastAPI layer."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field


# ── Requests ──────────────────────────────────────────────────────────────

class CreateChatRequest(BaseModel):
    title: str = "Новый чат"


class SendMessageRequest(BaseModel):
    content: str


class ExecuteCodeRequest(BaseModel):
    code: str


# ── Responses ─────────────────────────────────────────────────────────────

class ChatResponse(BaseModel):
    id: str
    title: str
    created_at: datetime
    updated_at: datetime


class MessageResponse(BaseModel):
    id: str
    role: str
    content: str
    timestamp: datetime
    metadata: dict[str, Any] | None = None


class AgentResponse(BaseModel):
    response: str
    response_type: str                          # "text" | "code" | "clarification"
    code: str | None = None
    validation_results: dict[str, Any] | None = None


class ExecuteResponse(BaseModel):
    success: bool
    stdout: str
    stderr: str
    timed_out: bool


class HealthResponse(BaseModel):
    status: str                 # "ok" | "degraded"
    llm_available: bool
    models_loaded: bool
