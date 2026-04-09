"""Pydantic models for storage layer entities."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field


class Chat(BaseModel):
    id: str
    title: str
    created_at: datetime
    updated_at: datetime


class Message(BaseModel):
    id: str
    chat_id: str
    role: str          # "user" | "assistant" | "system"
    content: str
    timestamp: datetime
    metadata: dict[str, Any] | None = None


class CodeArtifact(BaseModel):
    id: str
    chat_id: str
    message_id: str
    code: str
    language: str = "lua"
    validation_status: str     # "pending" | "passed" | "failed"
    test_results: dict[str, Any] | None = None
    created_at: datetime
