"""Persistent audit logging helpers without altering console log formatting."""

from __future__ import annotations

import json
import logging
import os
import uuid
from contextvars import ContextVar
from datetime import datetime, timezone
from logging.handlers import RotatingFileHandler
from typing import Any

DEFAULT_LOG_DIR = "logs"
DEFAULT_LOG_LEVEL = "INFO"
DEFAULT_LOG_MAX_BYTES = 10 * 1024 * 1024
DEFAULT_LOG_BACKUP_COUNT = 5
RUNTIME_LOG_NAME = "runtime.jsonl"
LLM_PROMPT_LOG_NAME = "llm_prompts.jsonl"

_runtime_audit_logger = logging.getLogger("runtime_audit")
_llm_audit_logger = logging.getLogger("llm_prompt_audit")
_audit_context: ContextVar[dict[str, Any]] = ContextVar("audit_log_context", default={})


def _parse_level(level: str | int) -> int:
    if isinstance(level, int):
        return level
    normalized = str(level or DEFAULT_LOG_LEVEL).strip().upper()
    return getattr(logging, normalized, logging.INFO)


def _parse_positive_int(raw: str | None, default: int) -> int:
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        return default
    return value if value > 0 else default


def configure_logging(log_dir: str | None = None, level: str | int | None = None) -> dict[str, Any]:
    """Configure rotating JSONL audit files without changing console handlers."""
    resolved_dir = os.path.abspath(log_dir or os.getenv("APP_LOG_DIR", DEFAULT_LOG_DIR))
    os.makedirs(resolved_dir, exist_ok=True)

    resolved_level = _parse_level(level or os.getenv("APP_LOG_LEVEL", DEFAULT_LOG_LEVEL))
    max_bytes = _parse_positive_int(os.getenv("APP_LOG_MAX_BYTES"), DEFAULT_LOG_MAX_BYTES)
    backup_count = _parse_positive_int(os.getenv("APP_LOG_BACKUP_COUNT"), DEFAULT_LOG_BACKUP_COUNT)

    runtime_log_path = os.path.join(resolved_dir, RUNTIME_LOG_NAME)
    llm_prompt_log_path = os.path.join(resolved_dir, LLM_PROMPT_LOG_NAME)

    runtime_handler = RotatingFileHandler(
        runtime_log_path,
        maxBytes=max_bytes,
        backupCount=backup_count,
        encoding="utf-8",
    )
    runtime_handler.setLevel(resolved_level)
    runtime_handler.setFormatter(logging.Formatter("%(message)s"))
    _runtime_audit_logger.handlers.clear()
    _runtime_audit_logger.setLevel(resolved_level)
    _runtime_audit_logger.propagate = False
    _runtime_audit_logger.addHandler(runtime_handler)

    llm_handler = RotatingFileHandler(
        llm_prompt_log_path,
        maxBytes=max_bytes,
        backupCount=backup_count,
        encoding="utf-8",
    )
    llm_handler.setLevel(resolved_level)
    llm_handler.setFormatter(logging.Formatter("%(message)s"))
    _llm_audit_logger.handlers.clear()
    _llm_audit_logger.setLevel(resolved_level)
    _llm_audit_logger.propagate = False
    _llm_audit_logger.addHandler(llm_handler)

    clear_log_context()

    return {
        "log_dir": resolved_dir,
        "log_level": logging.getLevelName(resolved_level),
        "runtime_log_path": runtime_log_path,
        "llm_prompt_log_path": llm_prompt_log_path,
        "max_bytes": max_bytes,
        "backup_count": backup_count,
    }


def _context_payload() -> dict[str, Any]:
    values = _audit_context.get() or {}
    if not isinstance(values, dict):
        return {}
    return {str(k): v for k, v in values.items() if v is not None}


def _build_audit_payload(event: str, fields: dict[str, Any]) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "timestamp": datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z"),
        "event": event,
    }
    payload.update(_context_payload())
    payload.update({k: v for k, v in fields.items() if v is not None})
    return payload


def write_runtime_audit(event: str, **fields: Any) -> None:
    """Write a runtime audit event to `runtime.jsonl`."""
    payload = _build_audit_payload(event, fields)
    _runtime_audit_logger.info(json.dumps(payload, ensure_ascii=False))


def write_llm_prompt_audit(event: str, **fields: Any) -> None:
    """Write an LLM prompt audit event to `llm_prompts.jsonl`."""
    payload = _build_audit_payload(event, fields)
    _llm_audit_logger.info(json.dumps(payload, ensure_ascii=False))


def bind_log_context(**fields: Any) -> None:
    """Bind context fields to audit log writes in this context."""
    payload = dict(_audit_context.get() or {})
    payload.update({k: v for k, v in fields.items() if v is not None})
    _audit_context.set(payload)


def clear_log_context() -> None:
    """Clear audit context variables to avoid leaking between turns/threads."""
    _audit_context.set({})


def new_turn_id() -> str:
    """Generate a short per-turn identifier for trace correlation."""
    return uuid.uuid4().hex[:12]
