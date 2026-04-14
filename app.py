#!/usr/bin/env python3
from dotenv import load_dotenv
load_dotenv()

import argparse
import asyncio
import json
import os
import re
import sqlite3
import threading
import webbrowser
from concurrent.futures import CancelledError as FutureCancelledError
from concurrent.futures import TimeoutError as FutureTimeoutError
from contextlib import closing
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import structlog

from console_utils import configure_console_utf8
from src.core.llm import LLMProvider
from src.core.logging_runtime import configure_logging, new_turn_id, write_runtime_audit
from src.graph.engine import PipelineEngine
from src.tools.target_tools import build_chat_title

logger = structlog.get_logger(__name__)

# ── Defaults (Ollama runtime) ─────────────────────────────────────────
DEFAULT_URL = "http://127.0.0.1:11434/v1"
DEFAULT_MODEL = "qwen2.5-coder:7b-instruct"
DEFAULT_MAX_ATTEMPTS = 3
DEFAULT_REQUEST_TIMEOUT = 600.0


CHAT_DB_NAME = ".lua_console_chats.db"
MAX_CHAT_TITLE_LENGTH = 72
AGENT_LABELS_RU = {
    "TaskPlanner": "Планировщик задачи",
    "IntentRouter": "Маршрутизатор интента",
    "CodeGenerator": "Генератор кода",
    "CodeRefiner": "Редактор кода",
    "CodeValidator": "Валидатор кода",
    "ValidationFixer": "Исправление валидации",
    "VerificationFixer": "Исправление требований",
    "RequirementsVerifier": "Проверка требований",
    "SolutionExplainer": "Объяснение решения",
    "QuestionAnswerer": "Ответ на вопрос",
    "TargetResolver": "Определение пути",
    "CodeSaver": "Сохранение файла",
    "ResponseAssembler": "Сборка ответа",
}


class PipelineCancelledError(RuntimeError):
    """Raised when the active pipeline turn is cancelled by the user."""


def utc_now_iso() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _empty_state_dict(workspace_root: str | None = None) -> dict:
    """Return a fresh empty state dict for serialization."""
    normalized_workspace = os.path.abspath(workspace_root or os.getcwd())
    return {
        "base_prompt": "",
        "change_requests": [],
        "current_code": "",
        "target_path": "",
        "workspace_root": normalized_workspace,
        "last_intent": "",
        "last_saved_path": "",
        "last_saved_jsonstring_path": "",
        "last_suggested_changes": [],
        "last_clarifying_questions": [],
        "last_explanation": {},
        "awaiting_planner_clarification": False,
        "planner_pending_questions": [],
        "planner_original_input": "",
        "planner_clarification_attempts": 0,
    }


def _normalize_state_dict(state_dict: dict | None, workspace_root: str | None = None) -> dict:
    """Normalize persisted chat state and migrate legacy keys."""
    normalized = _empty_state_dict(workspace_root)
    if not isinstance(state_dict, dict):
        return normalized

    target_path = str(
        state_dict.get("target_path")
        or state_dict.get("output_path")
        or ""
    ).strip()
    if target_path:
        target_path = os.path.abspath(target_path)

    normalized["base_prompt"] = str(state_dict.get("base_prompt", "") or "").strip()
    normalized["change_requests"] = [
        str(item).strip()
        for item in state_dict.get("change_requests", [])
        if str(item).strip()
    ]
    normalized["current_code"] = str(state_dict.get("current_code", "") or "")
    normalized["target_path"] = target_path
    normalized["workspace_root"] = os.path.abspath(
        str(
            state_dict.get("workspace_root")
            or (os.path.dirname(target_path) if target_path else normalized["workspace_root"])
        )
    )
    normalized["last_intent"] = str(state_dict.get("last_intent", "") or "")
    normalized["last_saved_path"] = str(
        state_dict.get("last_saved_path")
        or target_path
        or ""
    ).strip()
    normalized["last_saved_jsonstring_path"] = str(
        state_dict.get("last_saved_jsonstring_path")
        or ""
    ).strip()
    normalized["last_suggested_changes"] = [
        str(item).strip()
        for item in state_dict.get("last_suggested_changes", [])
        if str(item).strip()
    ]
    normalized["last_clarifying_questions"] = [
        str(item).strip()
        for item in state_dict.get("last_clarifying_questions", [])
        if str(item).strip()
    ]
    explanation = state_dict.get("last_explanation", {})
    normalized["last_explanation"] = explanation if isinstance(explanation, dict) else {}
    normalized["awaiting_planner_clarification"] = bool(
        state_dict.get("awaiting_planner_clarification", False)
    )
    normalized["planner_pending_questions"] = [
        str(item).strip()
        for item in state_dict.get("planner_pending_questions", []) or []
        if str(item).strip()
    ]
    normalized["planner_original_input"] = str(
        state_dict.get("planner_original_input", "") or ""
    )
    try:
        normalized["planner_clarification_attempts"] = int(
            state_dict.get("planner_clarification_attempts", 0) or 0
        )
    except (TypeError, ValueError):
        normalized["planner_clarification_attempts"] = 0
    return normalized


def _derive_title(base_prompt: str, target_path: str = "", fallback: str = "Новый чат") -> str:
    """Build a short chat title from the base prompt."""
    derived = build_chat_title(base_prompt, target_path=target_path, fallback=fallback)
    if len(derived) <= MAX_CHAT_TITLE_LENGTH:
        return derived
    return f"{derived[: MAX_CHAT_TITLE_LENGTH - 3].rstrip()}..."


def _extract_suggestion_indexes(text: str) -> list[int]:
    lowered = text.lower()
    indexes: set[int] = set()

    for match in re.finditer(r"(?:предложени[ея]|suggestion)\s*#?\s*(\d+)", lowered):
        try:
            value = int(match.group(1))
        except ValueError:
            continue
        if value > 0:
            indexes.add(value - 1)

    if not indexes and ("все предложения" in lowered or "all suggestions" in lowered):
        return [-1]
    return sorted(indexes)


def _expand_suggestion_followup(text: str, suggestions: list[str]) -> str:
    if not suggestions:
        return text

    indexes = _extract_suggestion_indexes(text)
    selected: list[str] = []
    if indexes == [-1]:
        selected = suggestions
    else:
        for idx in indexes:
            if 0 <= idx < len(suggestions):
                selected.append(suggestions[idx])

    lowered = text.lower()
    weak_apply_signal = any(
        token in lowered
        for token in (
            "примени предложение",
            "применяй предложение",
            "согласен с предложением",
            "apply suggestion",
        )
    )
    if not selected and weak_apply_signal and len(suggestions) == 1:
        selected = [suggestions[0]]

    if not selected:
        return text

    merged = "\n".join(f"- {item}" for item in selected)
    return (
        f"{text}\n\n"
        "Считай, что я согласовал следующие улучшения и их нужно применить:\n"
        f"{merged}\n"
        "Выполни изменения в текущем Lua-файле и сохрани в тот же target."
    )


class ChatStore:
    """SQLite storage for chats and messages (kept from original, simplified state)."""

    def __init__(self, db_path: str) -> None:
        self.db_path = db_path
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.db_path)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        return connection

    def _init_db(self) -> None:
        with closing(self._connect()) as connection, connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS chats (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    title TEXT NOT NULL,
                    state_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS chat_messages (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    chat_id INTEGER NOT NULL,
                    role TEXT NOT NULL,
                    title TEXT NOT NULL,
                    content TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    FOREIGN KEY(chat_id) REFERENCES chats(id) ON DELETE CASCADE
                );
                """
            )

    def list_chats(self) -> list[dict]:
        with closing(self._connect()) as connection:
            rows = connection.execute(
                """
                SELECT c.id, c.title, c.updated_at, c.created_at,
                       (SELECT COUNT(*) FROM chat_messages m WHERE m.chat_id = c.id) AS message_count
                FROM chats c
                ORDER BY c.updated_at DESC, c.id DESC
                """
            ).fetchall()
        return [dict(row) for row in rows]

    def create_chat(self, state_dict: dict, title: str = "Новый чат") -> int:
        snapshot = json.dumps(state_dict, ensure_ascii=False)
        now = utc_now_iso()
        with closing(self._connect()) as connection, connection:
            cursor = connection.execute(
                "INSERT INTO chats (title, state_json, created_at, updated_at) VALUES (?, ?, ?, ?)",
                (title, snapshot, now, now),
            )
            return int(cursor.lastrowid)

    def get_chat(self, chat_id: int) -> dict | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                "SELECT id, title, state_json, created_at, updated_at FROM chats WHERE id = ?",
                (chat_id,),
            ).fetchone()
        return dict(row) if row else None

    def load_state_dict(self, chat_id: int) -> dict:
        chat = self.get_chat(chat_id)
        if not chat:
            raise KeyError(f"Chat {chat_id} not found.")
        try:
            return json.loads(chat["state_json"])
        except (json.JSONDecodeError, TypeError):
            return _empty_state_dict()

    def load_messages(self, chat_id: int) -> list[dict]:
        with closing(self._connect()) as connection:
            rows = connection.execute(
                "SELECT role, title, content, created_at FROM chat_messages WHERE chat_id = ? ORDER BY id ASC",
                (chat_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    def save_chat_state(self, chat_id: int, state_dict: dict, title: str | None = None) -> None:
        current = self.get_chat(chat_id)
        if not current:
            raise KeyError(f"Chat {chat_id} not found.")
        snapshot = json.dumps(state_dict, ensure_ascii=False)
        effective_title = title or current["title"]
        with closing(self._connect()) as connection, connection:
            connection.execute(
                "UPDATE chats SET title = ?, state_json = ?, updated_at = ? WHERE id = ?",
                (effective_title, snapshot, utc_now_iso(), chat_id),
            )

    def add_message(self, chat_id: int, role: str, title: str, content: str) -> None:
        current = self.get_chat(chat_id)
        if not current:
            raise KeyError(f"Chat {chat_id} not found.")
        with closing(self._connect()) as connection, connection:
            connection.execute(
                "INSERT INTO chat_messages (chat_id, role, title, content, created_at) VALUES (?, ?, ?, ?, ?)",
                (chat_id, role, title, content, utc_now_iso()),
            )
            connection.execute(
                "UPDATE chats SET updated_at = ? WHERE id = ?",
                (utc_now_iso(), chat_id),
            )

    def delete_chat(self, chat_id: int) -> None:
        with closing(self._connect()) as connection, connection:
            connection.execute("DELETE FROM chat_messages WHERE chat_id = ?", (chat_id,))
            connection.execute("DELETE FROM chats WHERE id = ?", (chat_id,))

HTML_PAGE = """<!doctype html>
<html lang="ru">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>LowCode Lua Script Builder</title>
  <style>
    :root {
      --bg: #f3ecdf;
      --panel: #fffaf0;
      --panel-strong: #f8f1e4;
      --line: #d8c7aa;
      --ink: #2f2618;
      --muted: #776652;
      --accent: #b5552d;
      --accent-dark: #8d3e1c;
      --user: #efe2cc;
      --assistant: #fffdf8;
      --shadow: 0 20px 50px rgba(73, 50, 23, 0.12);
    }

    * {
      box-sizing: border-box;
    }

    html,
    body {
      height: 100%;
    }

    body {
      margin: 0;
      min-height: 100vh;
      min-height: 100dvh;
      color: var(--ink);
      background:
        radial-gradient(circle at top left, rgba(181, 85, 45, 0.12), transparent 32%),
        radial-gradient(circle at bottom right, rgba(109, 76, 37, 0.12), transparent 30%),
        linear-gradient(180deg, #f7f1e7 0%, var(--bg) 100%);
      font-family: "Trebuchet MS", "Segoe UI", sans-serif;
      overflow: hidden;
    }

    .shell {
      max-width: 1560px;
      margin: 0 auto;
      padding: 10px 18px;
      display: grid;
      grid-template-columns: minmax(0, 1.78fr) minmax(340px, 0.82fr);
      gap: 18px;
      height: 100vh;
      height: 100dvh;
    }

    .panel {
      background: rgba(255, 250, 240, 0.92);
      border: 1px solid var(--line);
      border-radius: 24px;
      box-shadow: var(--shadow);
      backdrop-filter: blur(10px);
      min-height: 0;
    }

    .chat-panel {
      padding: 22px 24px;
      display: flex;
      flex-direction: column;
      min-height: 0;
      height: 100%;
      overflow: hidden;
    }

    .side-panel {
      padding: 16px;
      display: flex;
      flex-direction: column;
      gap: 12px;
      min-height: 0;
      height: 100%;
      overflow: auto;
    }

    .hero {
      display: flex;
      justify-content: space-between;
      align-items: flex-start;
      gap: 12px;
      padding-bottom: 10px;
      border-bottom: 1px solid rgba(216, 199, 170, 0.7);
      flex-shrink: 0;
    }

    .hero h1 {
      margin: 0;
      font-size: 28px;
      line-height: 1;
      font-family: "Palatino Linotype", Georgia, serif;
      letter-spacing: 0.02em;
    }

    .hero p {
      margin: 6px 0 0;
      color: var(--muted);
      max-width: 720px;
      font-size: 14px;
    }

    .badge {
      padding: 8px 12px;
      border-radius: 999px;
      border: 1px solid rgba(181, 85, 45, 0.25);
      background: rgba(181, 85, 45, 0.08);
      color: var(--accent-dark);
      font-weight: 700;
      white-space: nowrap;
      font-size: 13px;
    }

    .toolbar {
      display: flex;
      flex-wrap: wrap;
      gap: 8px;
      padding: 10px 0 8px;
      flex-shrink: 0;
    }

    button {
      border: 1px solid var(--line);
      background: var(--panel-strong);
      color: var(--ink);
      border-radius: 14px;
      padding: 8px 12px;
      font: inherit;
      cursor: pointer;
      transition: transform 0.15s ease, border-color 0.15s ease, background 0.15s ease;
    }

    button.primary {
      background: linear-gradient(180deg, #c16236 0%, var(--accent) 100%);
      border-color: rgba(141, 62, 28, 0.7);
      color: #fff9f3;
      font-weight: 700;
    }

    button:hover:not(:disabled) {
      transform: translateY(-1px);
      border-color: var(--accent);
    }

    button:disabled {
      opacity: 0.55;
      cursor: progress;
    }

    .timeline {
      flex: 1;
      overflow: auto;
      min-height: 0;
      padding-right: 6px;
      padding-bottom: 10px;
      display: flex;
      flex-direction: column;
      gap: 14px;
    }

    .message {
      border-radius: 20px;
      padding: 16px 18px;
      border: 1px solid rgba(216, 199, 170, 0.86);
      white-space: pre-wrap;
      line-height: 1.45;
      animation: rise 0.18s ease;
    }

    .message.user {
      background: var(--user);
      align-self: flex-end;
      max-width: min(82%, 760px);
    }

    .message.assistant {
      background: var(--assistant);
      align-self: flex-start;
      max-width: min(92%, 920px);
    }

    .message.system {
      background: rgba(248, 241, 228, 0.86);
      align-self: center;
      max-width: 100%;
      color: var(--muted);
    }

    .message.thinking {
      position: relative;
      overflow: hidden;
      border-style: dashed;
      border-color: rgba(181, 85, 45, 0.42);
      background:
        linear-gradient(180deg, rgba(255, 255, 255, 0.96), rgba(255, 250, 240, 0.9));
      align-self: flex-start;
      max-width: min(92%, 920px);
      flex-shrink: 0;
    }

    .message.thinking::after {
      content: "";
      position: absolute;
      inset: 0;
      background: linear-gradient(
        110deg,
        transparent 0%,
        rgba(181, 85, 45, 0.05) 40%,
        rgba(181, 85, 45, 0.11) 50%,
        rgba(181, 85, 45, 0.05) 60%,
        transparent 100%
      );
      transform: translateX(-100%);
      animation: shimmer 1.7s linear infinite;
      pointer-events: none;
    }

    .thinking-meta {
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 12px;
    }

    .thinking-badge {
      display: inline-flex;
      align-items: center;
      gap: 8px;
      padding: 4px 10px;
      border-radius: 999px;
      background: rgba(181, 85, 45, 0.1);
      border: 1px solid rgba(181, 85, 45, 0.2);
      color: var(--accent-dark);
      font-size: 11px;
      letter-spacing: 0.05em;
    }

    .thinking-badge::before {
      content: "";
      width: 8px;
      height: 8px;
      border-radius: 50%;
      background: var(--accent);
      box-shadow: 0 0 0 rgba(181, 85, 45, 0.55);
      animation: pulse 1.25s ease infinite;
    }

    .thinking-body {
      display: grid;
      gap: 8px;
      position: relative;
      z-index: 1;
    }

    .thinking-line {
      font-weight: 600;
      color: var(--ink);
    }

    .thinking-hint {
      font-size: 12px;
      color: var(--muted);
    }

    .meta {
      margin-bottom: 8px;
      font-size: 12px;
      font-weight: 700;
      letter-spacing: 0.08em;
      text-transform: uppercase;
      color: var(--accent-dark);
    }

    .composer {
      display: grid;
      grid-template-columns: 1fr auto;
      gap: 12px;
      padding-top: 8px;
      margin-top: 6px;
      border-top: 1px solid rgba(216, 199, 170, 0.7);
      flex-shrink: 0;
      background: rgba(255, 250, 240, 0.96);
    }

    textarea {
      width: 100%;
      min-height: 60px;
      max-height: 220px;
      resize: vertical;
      border-radius: 18px;
      border: 1px solid var(--line);
      background: #fffdf8;
      padding: 12px 14px;
      color: var(--ink);
      font: inherit;
      line-height: 1.5;
    }

    .send-box {
      display: flex;
      flex-direction: column;
      gap: 10px;
      justify-content: flex-end;
    }

    .hint {
      margin: 0;
      color: var(--muted);
      font-size: 12px;
    }

    .side-card {
      background: rgba(255, 255, 255, 0.55);
      border: 1px solid rgba(216, 199, 170, 0.92);
      border-radius: 18px;
      padding: 16px 18px;
    }

    .session-card {
      padding: 12px 14px;
    }

    .chat-list-card {
      padding: 12px 14px;
      display: flex;
      flex-direction: column;
      min-height: 0;
    }

    .chat-list-head {
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 10px;
      margin-bottom: 10px;
    }

    .chat-list-head h2 {
      margin: 0;
    }

    .chat-list {
      display: flex;
      flex-direction: column;
      gap: 8px;
      overflow: auto;
      min-height: 0;
      max-height: 42vh;
      padding-right: 4px;
    }

    .chat-row {
      display: grid;
      grid-template-columns: minmax(0, 1fr) auto;
      gap: 8px;
      align-items: stretch;
    }

    .chat-item {
      width: 100%;
      text-align: left;
      padding: 10px 12px;
      border-radius: 14px;
      background: rgba(255, 253, 248, 0.92);
    }

    .chat-item.active {
      border-color: rgba(141, 62, 28, 0.7);
      background: rgba(181, 85, 45, 0.12);
    }

    .chat-delete {
      min-width: 44px;
      padding: 8px 10px;
      border-radius: 14px;
      color: #7a2a18;
      background: rgba(181, 85, 45, 0.08);
    }

    .chat-item-title {
      display: block;
      font-weight: 700;
      color: var(--ink);
      margin-bottom: 4px;
      white-space: nowrap;
      overflow: hidden;
      text-overflow: ellipsis;
    }

    .chat-item-meta {
      display: block;
      font-size: 12px;
      color: var(--muted);
    }

    .side-card h2 {
      margin: 0 0 10px;
      font-size: 15px;
      text-transform: uppercase;
      letter-spacing: 0.08em;
      color: var(--accent-dark);
    }

    .side-card-head {
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 10px;
      margin-bottom: 10px;
    }

    .side-card-head h2 {
      margin: 0;
    }

    .ghost-button {
      min-width: 88px;
      padding: 8px 12px;
      border-radius: 12px;
      background: rgba(181, 85, 45, 0.08);
      color: var(--accent-dark);
    }

    .facts {
      display: grid;
      gap: 10px;
    }

    .session-card .facts {
      gap: 7px;
    }

    .fact {
      display: grid;
      gap: 4px;
    }

    .session-card .fact {
      gap: 2px;
    }

    .fact-label {
      font-size: 12px;
      text-transform: uppercase;
      letter-spacing: 0.08em;
      color: var(--muted);
    }

    .fact-value {
      word-break: break-word;
      font-weight: 600;
    }

    .session-card .fact-label {
      font-size: 11px;
    }

    .session-card .fact-value {
      font-size: 13px;
      font-weight: 600;
    }

    .code-box {
      min-height: 180px;
      max-height: 48vh;
      overflow: auto;
      padding: 14px 16px;
      border-radius: 16px;
      background: #2d2418;
      color: #f8e7d1;
      border: 1px solid rgba(56, 40, 22, 0.9);
      font-family: Consolas, "Courier New", monospace;
      font-size: 13px;
      line-height: 1.45;
      white-space: pre-wrap;
    }

    .ghost {
      color: var(--muted);
      font-style: italic;
    }

    @keyframes rise {
      from {
        opacity: 0;
        transform: translateY(6px);
      }
      to {
        opacity: 1;
        transform: translateY(0);
      }
    }

    @keyframes shimmer {
      from {
        transform: translateX(-100%);
      }
      to {
        transform: translateX(100%);
      }
    }

    @keyframes pulse {
      0% {
        transform: scale(0.92);
        box-shadow: 0 0 0 0 rgba(181, 85, 45, 0.34);
      }
      70% {
        transform: scale(1);
        box-shadow: 0 0 0 10px rgba(181, 85, 45, 0);
      }
      100% {
        transform: scale(0.92);
        box-shadow: 0 0 0 0 rgba(181, 85, 45, 0);
      }
    }

    @media (max-width: 1080px) {
      body {
        overflow: auto;
      }

      .shell {
        grid-template-columns: 1fr;
        height: auto;
      }

      .chat-panel,
      .side-panel {
        min-height: auto;
        height: auto;
        overflow: visible;
      }
    }
  </style>
</head>
<body>
  <div class="shell">
    <section class="panel chat-panel">
      <div class="hero">
        <div>
          <h1>LowCode Lua Script Builder</h1>
          <p>Локальный чат для генерации, редактирования и проверки workflow/LUS Lua-скриптов. Runtime проверяет код в LowCode harness, сохраняет `.lua` и JsonString sidecar, а затем объясняет сделанное и предлагает улучшения.</p>
        </div>
        <div class="badge" id="statusBadge">Локально</div>
      </div>

      <div class="toolbar">
        <button type="button" data-action="retry">Повторить Проверку</button>
        <button type="button" data-action="status">Статус</button>
        <button type="button" data-action="path">Путь</button>
        <button type="button" data-action="prompt">Текущий Промпт</button>
        <button type="button" data-action="code">Показать Код</button>
        <button type="button" data-action="help">Помощь</button>
      </div>

      <div class="timeline" id="timeline"></div>

      <form class="composer" id="composer">
        <div>
          <textarea id="messageInput" placeholder="Опиши задачу, укажи папку или конкретный .lua файл..."></textarea>
          <p class="hint">Enter отправляет сообщение, Shift+Enter переносит строку.</p>
        </div>
        <div class="send-box">
          <button class="primary" id="sendButton" type="submit">Отправить</button>
          <button type="button" id="stopButton" disabled>Стоп</button>
        </div>
      </form>
    </section>

    <aside class="panel side-panel">
      <div class="side-card chat-list-card">
        <div class="chat-list-head">
          <h2>Чаты</h2>
          <button type="button" id="newChatButton">Новый Чат</button>
        </div>
        <div class="chat-list" id="chatList"></div>
      </div>

      <div class="side-card">
        <div class="side-card-head">
          <h2>Последний Код</h2>
          <button type="button" class="ghost-button" id="copyCodeButton">Копировать</button>
        </div>
        <div class="code-box" id="codeBox"><span class="ghost">Код появится после первой генерации или команды “Показать Код”.</span></div>
      </div>
    </aside>
  </div>

  <script>
    const timeline = document.getElementById("timeline");
    const composer = document.getElementById("composer");
    const input = document.getElementById("messageInput");
    const sendButton = document.getElementById("sendButton");
    const stopButton = document.getElementById("stopButton");
    const codeBox = document.getElementById("codeBox");
    const copyCodeButton = document.getElementById("copyCodeButton");
    const statusBadge = document.getElementById("statusBadge");
    const chatList = document.getElementById("chatList");
    const newChatButton = document.getElementById("newChatButton");
    let activeChatId = null;
    let stopThinkingIndicator = null;
    let cancelRequested = false;
    let activeStatePollTimer = null;

    function addMessage(kind, title, text) {
      const card = document.createElement("div");
      card.className = `message ${kind}`;
      const meta = document.createElement("div");
      meta.className = "meta";
      meta.textContent = title;
      const body = document.createElement("div");
      body.textContent = text;
      card.append(meta, body);
      timeline.appendChild(card);
      timeline.scrollTop = timeline.scrollHeight;
    }

    function clearTimeline() {
      timeline.innerHTML = "";
    }

    function clearThinkingIndicator() {
      if (stopThinkingIndicator) {
        stopThinkingIndicator();
        stopThinkingIndicator = null;
      }
      if (activeStatePollTimer) {
        window.clearInterval(activeStatePollTimer);
        activeStatePollTimer = null;
      }
    }

    function buildThinkingStages(text) {
      const normalized = (text || "").trim().toLowerCase();
      if (!normalized) {
        return [
          "Думает над запросом",
          "Готовит ответ",
        ];
      }

      if (normalized.startsWith("/status")) {
        return [
          "Собирает состояние чата",
          "Готовит краткую сводку",
        ];
      }

      if (normalized.startsWith("/path")) {
        return [
          "Проверяет активный файл",
          "Готовит путь и рабочую папку",
        ];
      }

      if (normalized.startsWith("/code") || normalized.startsWith("/show")) {
        return [
          "Достаёт текущий код",
          "Готовит содержимое файла",
        ];
      }

      if (normalized.startsWith("/prompt")) {
        return [
          "Собирает требования чата",
          "Готовит текущий промпт",
        ];
      }

      if (normalized.startsWith("/retry")) {
        return [
          "Поднимает текущий контекст",
          "Перепроверяет файл",
          "Сравнивает замечания",
          "Готовит итоговый лог",
        ];
      }

      if (
        normalized.includes("объясн")
        || normalized.includes("обьясн")
        || normalized.includes("расскажи")
        || normalized.includes("что делает")
        || normalized.includes("разбери")
      ) {
        return [
          "Читает целевой файл",
          "Выделяет основную логику",
          "Готовит понятное объяснение",
        ];
      }

      if (
        normalized.includes(".lua")
        || normalized.includes("\\\\")
        || normalized.includes("/")
        || normalized.includes("папк")
        || normalized.includes("директор")
        || normalized.includes("каталог")
      ) {
        return [
          "Определяет целевой путь",
          "Подбирает Lua target",
          "Готовит содержимое файла",
          "Проверяет и сохраняет результат",
        ];
      }

      return [
        "Анализирует запрос",
        "Определяет тип задачи",
        "Подбирает рабочий файл",
        "Готовит решение",
        "Проверяет результат",
      ];
    }

    function showThinkingIndicator(text) {
      clearThinkingIndicator();

      const stages = buildThinkingStages(text);
      const card = document.createElement("div");
      card.className = "message assistant thinking";

      const meta = document.createElement("div");
      meta.className = "meta thinking-meta";

      const metaTitle = document.createElement("span");
      metaTitle.textContent = "Ассистент";

      const badge = document.createElement("span");
      badge.className = "thinking-badge";
      badge.textContent = "думает";

      meta.append(metaTitle, badge);

      const body = document.createElement("div");
      body.className = "thinking-body";

      const line = document.createElement("div");
      line.className = "thinking-line";

      const hint = document.createElement("div");
      hint.className = "thinking-hint";
      hint.textContent = "";

      body.append(line, hint);
      card.append(meta, body);
      timeline.appendChild(card);
      timeline.scrollTop = timeline.scrollHeight;

      let stageIndex = 0;
      let dots = 1;
      const render = () => {
        line.textContent = `${stages[stageIndex]}${".".repeat(dots)}`;
        dots += 1;
        if (dots > 3) {
          dots = 1;
          stageIndex = (stageIndex + 1) % stages.length;
        }
      };

      render();
      const timer = window.setInterval(render, 900);
      const updateAgentHint = async () => {
        try {
          const response = await fetch("/api/state");
          const data = await response.json();
          const label = data && data.state ? (data.state.active_agent_label || "") : "";
          hint.textContent = label ? `Сейчас работает: ${label}.` : "";
        } catch (error) {
          // Ignore polling errors while the request is in-flight.
        }
      };
      updateAgentHint();
      activeStatePollTimer = window.setInterval(updateAgentHint, 1000);
      stopThinkingIndicator = () => {
        window.clearInterval(timer);
        if (activeStatePollTimer) {
          window.clearInterval(activeStatePollTimer);
          activeStatePollTimer = null;
        }
        if (card.isConnected) {
          card.remove();
        }
      };
    }

    function renderMessages(messages, state) {
      clearThinkingIndicator();
      clearTimeline();
      if (!messages || !messages.length) {
        if (state && state.has_project) {
          addMessage("system", "Контекст Восстановлен", `Чат ${state.chat_id || ""} готов к продолжению.`);
        } else {
          addMessage("system", "Новый Чат", "Контекст пуст. Опиши задачу внизу, и приложение создаст или обновит Lua-файл в нужном пути.");
        }
        return;
      }
      for (const item of messages) {
        addMessage(item.role || "system", item.title || "Сообщение", item.content || "");
      }
    }

    function setBusy(isBusy) {
      sendButton.disabled = isBusy;
      stopButton.disabled = !isBusy || cancelRequested;
      newChatButton.disabled = isBusy;
      document.querySelectorAll("[data-action]").forEach((button) => {
        button.disabled = isBusy;
      });
      document.querySelectorAll(".chat-item, .chat-delete").forEach((button) => {
        button.disabled = isBusy;
      });
      statusBadge.textContent = isBusy ? "Выполняется" : "Локально";
    }

    function updateSidebar(state) {
      if (state.current_code && state.current_code.trim()) {
        codeBox.textContent = state.current_code;
        codeBox.dataset.filled = "yes";
        copyCodeButton.disabled = false;
      } else {
        delete codeBox.dataset.filled;
        codeBox.innerHTML = '<span class="ghost">Код появится после первой генерации или команды “Показать Код”.</span>';
        copyCodeButton.disabled = true;
      }
    }

    async function copyLatestCode() {
      const code = codeBox.textContent || "";
      if (!codeBox.dataset.filled || !code.trim()) {
        return;
      }

      const originalLabel = copyCodeButton.textContent;
      try {
        await navigator.clipboard.writeText(code);
        copyCodeButton.textContent = "Скопировано";
        window.setTimeout(() => {
          copyCodeButton.textContent = originalLabel;
        }, 1400);
      } catch (error) {
        copyCodeButton.textContent = "Ошибка";
        window.setTimeout(() => {
          copyCodeButton.textContent = originalLabel;
        }, 1400);
      }
    }

    function renderChatList(chats, selectedChatId) {
      chatList.innerHTML = "";
      for (const chat of chats || []) {
        const row = document.createElement("div");
        row.className = "chat-row";

        const button = document.createElement("button");
        button.type = "button";
        button.className = `chat-item${chat.id === selectedChatId ? " active" : ""}`;
        button.dataset.chatId = String(chat.id);
        button.innerHTML = `
          <span class="chat-item-title">${chat.title || "Новый чат"}</span>
          <span class="chat-item-meta">${chat.updated_at || ""}</span>
        `;
        button.addEventListener("click", async () => {
          if (chat.id === activeChatId) {
            return;
          }
          setBusy(true);
          try {
            const data = await callApi("/api/chats/switch", { chat_id: chat.id });
            activeChatId = data.active_chat_id;
            renderChatList(data.chats, data.active_chat_id);
            updateSidebar(data.state);
            renderMessages(data.messages, data.state);
          } catch (error) {
            addMessage("assistant", "Ошибка", String(error));
          } finally {
            setBusy(false);
          }
        });

        const deleteButton = document.createElement("button");
        deleteButton.type = "button";
        deleteButton.className = "chat-delete";
        deleteButton.title = "Удалить чат";
        deleteButton.textContent = "✕";
        deleteButton.addEventListener("click", async (event) => {
          event.stopPropagation();
          if (!window.confirm(`Удалить чат "${chat.title || "Новый чат"}"?`)) {
            return;
          }
          setBusy(true);
          try {
            const data = await callApi("/api/chats/delete", { chat_id: chat.id });
            activeChatId = data.active_chat_id;
            renderChatList(data.chats, data.active_chat_id);
            updateSidebar(data.state);
            renderMessages(data.messages, data.state);
          } catch (error) {
            addMessage("assistant", "Ошибка", String(error));
          } finally {
            setBusy(false);
          }
        });

        row.append(button, deleteButton);
        chatList.appendChild(row);
      }
    }

    async function callApi(path, payload) {
      const response = await fetch(path, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload || {}),
      });

      if (!response.ok) {
        const text = await response.text();
        throw new Error(text || `HTTP ${response.status}`);
      }

      return response.json();
    }

    async function refreshState(initial = false) {
      const response = await fetch("/api/state");
      const data = await response.json();
      activeChatId = data.active_chat_id;
      updateSidebar(data.state);
      renderChatList(data.chats, data.active_chat_id);
      renderMessages(data.messages, data.state);
    }

    async function submitMessage(text, title = "Пользователь") {
      if (!text.trim()) {
        return;
      }

      cancelRequested = false;
      addMessage("user", title, text.trim());
      showThinkingIndicator(text);
      setBusy(true);
      try {
        const data = await callApi("/api/message", { message: text });
        activeChatId = data.active_chat_id;
        updateSidebar(data.state);
        renderChatList(data.chats, data.active_chat_id);
        renderMessages(data.messages, data.state);
      } catch (error) {
        clearThinkingIndicator();
        addMessage("assistant", "Ошибка", String(error));
      } finally {
        clearThinkingIndicator();
        setBusy(false);
      }
    }

    stopButton.addEventListener("click", async () => {
      if (stopButton.disabled) {
        return;
      }
      cancelRequested = true;
      stopButton.disabled = true;
      statusBadge.textContent = "Останавливается";
      try {
        await callApi("/api/cancel", {});
      } catch (error) {
        cancelRequested = false;
        setBusy(true);
        addMessage("assistant", "Ошибка Остановки", String(error));
      }
    });

    copyCodeButton.addEventListener("click", async () => {
      await copyLatestCode();
    });

    composer.addEventListener("submit", async (event) => {
      event.preventDefault();
      const text = input.value;
      if (!text.trim()) {
        return;
      }
      input.value = "";
      await submitMessage(text, "Пользователь");
    });

    input.addEventListener("keydown", async (event) => {
      if (event.key === "Enter" && !event.shiftKey) {
        event.preventDefault();
        composer.requestSubmit();
      }
    });

    document.querySelectorAll("[data-action]").forEach((button) => {
      button.addEventListener("click", async () => {
        const action = button.dataset.action;
        const labelMap = {
          retry: "Повторить Проверку",
          status: "Статус",
          path: "Путь",
          prompt: "Текущий Промпт",
          code: "Показать Код",
          help: "Помощь",
        };
        const messageMap = {
          retry: "/retry",
          status: "/status",
          path: "/path",
          prompt: "/prompt",
          code: "/code",
          help: "/help",
        };
        await submitMessage(messageMap[action], labelMap[action] || "Команда");
      });
    });

    newChatButton.addEventListener("click", async () => {
      setBusy(true);
      try {
        const data = await callApi("/api/chats/new", {});
        activeChatId = data.active_chat_id;
        updateSidebar(data.state);
        renderChatList(data.chats, data.active_chat_id);
        renderMessages(data.messages, data.state);
      } catch (error) {
        addMessage("assistant", "Ошибка", String(error));
      } finally {
        setBusy(false);
      }
    });

    refreshState(true).catch((error) => {
      addMessage("assistant", "Ошибка Инициализации", String(error));
    });
  </script>
</body>
</html>
"""


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="LowCode Lua Script Builder — canonical web runtime.")
    parser.add_argument("--host", default="127.0.0.1", help="Host to bind the local web UI.")
    parser.add_argument("--port", type=int, default=8765, help="Port for the local web UI.")
    parser.add_argument("--no-browser", action="store_true", help="Do not auto-open the browser.")
    parser.add_argument(
        "--workspace",
        default=os.getcwd(),
        help="Default workspace root used when the prompt does not name a target path.",
    )
    parser.add_argument(
        "--model",
        default=os.getenv("OLLAMA_MODEL", DEFAULT_MODEL),
        help="Ollama model name (e.g. qwen2.5-coder:7b-instruct, qwen2.5-coder:3b-instruct).",
    )
    parser.add_argument(
        "--url",
        default=os.getenv("OLLAMA_BASE_URL", DEFAULT_URL),
        help="Base URL of the Ollama runtime (default: http://127.0.0.1:11434/v1).",
    )
    parser.add_argument("--max-attempts", type=int, default=DEFAULT_MAX_ATTEMPTS, help="Max validation/fix iterations.")
    parser.add_argument(
        "--request-timeout",
        type=float,
        default=DEFAULT_REQUEST_TIMEOUT,
        help="Seconds to wait for the local runtime.",
    )
    parser.add_argument(
        "--log-dir",
        default=os.getenv("APP_LOG_DIR", "logs"),
        help="Directory for runtime logs (default: logs).",
    )
    parser.add_argument(
        "--log-level",
        default=os.getenv("APP_LOG_LEVEL", "INFO"),
        help="Logging level (DEBUG, INFO, WARNING, ERROR).",
    )
    return parser.parse_args()


class AppRuntime:
    """Web UI runtime backed by the LangGraph PipelineEngine."""

    def __init__(self, args: argparse.Namespace) -> None:
        self.args = args
        self.lock = threading.Lock()
        self.default_workspace = os.path.abspath(getattr(args, "workspace", os.getcwd()) or os.getcwd())
        self.store = ChatStore(os.path.join(os.getcwd(), CHAT_DB_NAME))
        self._active_future_lock = threading.Lock()
        self._active_pipeline_future = None
        self._active_pipeline_chat_id = 0
        self._active_pipeline_turn_id = ""
        self._active_agent_name = ""
        self._active_agent_label = ""

        # Create the async event loop for the engine
        self._loop = asyncio.new_event_loop()
        self._loop_thread = threading.Thread(target=self._loop.run_forever, daemon=True)
        self._loop_thread.start()

        # LangGraph pipeline engine
        llm = LLMProvider(
            base_url=getattr(args, "url", DEFAULT_URL),
            model=getattr(args, "model", DEFAULT_MODEL),
            timeout=getattr(args, "request_timeout", DEFAULT_REQUEST_TIMEOUT),
            status_callback=self._handle_llm_status,
        )
        self.engine = PipelineEngine(
            llm=llm,
            max_fix_iterations=getattr(args, "max_attempts", DEFAULT_MAX_ATTEMPTS),
        )

        # Chat state (simple dict now)
        self.state_dict: dict = _empty_state_dict(self.default_workspace)
        chats = self.store.list_chats()
        if chats:
            self.current_chat_id = int(chats[0]["id"])
            self.state_dict = _normalize_state_dict(
                self.store.load_state_dict(self.current_chat_id),
                self.default_workspace,
            )
        else:
            self.current_chat_id = self.store.create_chat(self.state_dict, "Новый чат")

    def _set_active_pipeline(self, future, *, chat_id: int, turn_id: str) -> None:
        with self._active_future_lock:
            self._active_pipeline_future = future
            self._active_pipeline_chat_id = chat_id
            self._active_pipeline_turn_id = turn_id
            self._active_agent_name = ""
            self._active_agent_label = ""

    def _clear_active_pipeline(self, future) -> None:
        with self._active_future_lock:
            if self._active_pipeline_future is future:
                self._active_pipeline_future = None
                self._active_pipeline_chat_id = 0
                self._active_pipeline_turn_id = ""
                self._active_agent_name = ""
                self._active_agent_label = ""

    def _handle_llm_status(self, *, event: str, agent_name: str = "", model: str = "", call_kind: str = "") -> None:
        with self._active_future_lock:
            if event == "start":
                self._active_agent_name = str(agent_name or "").strip()
                self._active_agent_label = AGENT_LABELS_RU.get(
                    self._active_agent_name,
                    self._active_agent_name or "LLM-агент",
                )
            elif event == "finish" and self._active_agent_name == str(agent_name or "").strip():
                self._active_agent_name = ""
                self._active_agent_label = ""

    def _run_async(self, coro, *, track_pipeline: bool = False, chat_id: int = 0, turn_id: str = "") -> any:
        """Run an async coroutine from sync context via the background event loop."""
        future = asyncio.run_coroutine_threadsafe(coro, self._loop)
        if track_pipeline:
            self._set_active_pipeline(future, chat_id=chat_id, turn_id=turn_id)
        try:
            return future.result(timeout=660)  # generous timeout
        except FutureCancelledError as exc:
            raise PipelineCancelledError("Генерация остановлена пользователем.") from exc
        except FutureTimeoutError as exc:
            future.cancel()
            raise TimeoutError("Pipeline timed out after 660 seconds") from exc
        finally:
            if track_pipeline:
                self._clear_active_pipeline(future)

    def cancel_active_pipeline(self) -> dict:
        with self._active_future_lock:
            future = self._active_pipeline_future
            chat_id = self._active_pipeline_chat_id
            turn_id = self._active_pipeline_turn_id

        if future is None or future.done():
            return {
                "cancel_requested": False,
                "message": "Нет активной генерации.",
            }

        cancelled = future.cancel()
        write_runtime_audit(
            "chat_pipeline_cancel_requested",
            chat_id=chat_id,
            turn_id=turn_id,
            cancel_requested=cancelled,
        )
        return {
            "cancel_requested": cancelled,
            "message": "Остановка запрошена." if cancelled else "Не удалось остановить генерацию.",
        }

    def _save_current_chat(self, title: str | None = None) -> None:
        self._ensure_current_chat_exists()
        effective_title = title or _derive_title(
            self.state_dict.get("base_prompt", ""),
            self.state_dict.get("target_path", ""),
        )
        self.store.save_chat_state(self.current_chat_id, self.state_dict, effective_title)

    def _ensure_current_chat_exists(self) -> None:
        if self.current_chat_id and self.store.get_chat(self.current_chat_id):
            return
        effective_title = _derive_title(
            self.state_dict.get("base_prompt", ""),
            self.state_dict.get("target_path", ""),
        )
        self.current_chat_id = self.store.create_chat(self.state_dict, effective_title)

    def build_messages_payload(self) -> list[dict]:
        self._ensure_current_chat_exists()
        return self.store.load_messages(self.current_chat_id)

    def build_state_payload(self) -> dict:
        sd = self.state_dict
        return {
            "chat_id": self.current_chat_id,
            "has_project": bool(
                sd.get("current_code", "").strip()
                or sd.get("base_prompt", "").strip()
                or sd.get("target_path", "").strip()
            ),
            "base_prompt": sd.get("base_prompt", ""),
            "change_requests": sd.get("change_requests", []),
            "current_code": sd.get("current_code", ""),
            "target_path": sd.get("target_path", ""),
            "workspace_root": sd.get("workspace_root", self.default_workspace),
            "last_intent": sd.get("last_intent", ""),
            "last_saved_path": sd.get("last_saved_path", ""),
            "last_saved_jsonstring_path": sd.get("last_saved_jsonstring_path", ""),
            "change_requests_count": len(sd.get("change_requests", [])),
            "suggested_changes": sd.get("last_suggested_changes", []),
            "clarifying_questions": sd.get("last_clarifying_questions", []),
            "pipeline_running": self._active_pipeline_future is not None and not self._active_pipeline_future.done(),
            "active_agent_name": self._active_agent_name,
            "active_agent_label": self._active_agent_label,
        }

    def build_full_payload(self) -> dict:
        self._ensure_current_chat_exists()
        return {
            "active_chat_id": self.current_chat_id,
            "state": self.build_state_payload(),
            "messages": self.build_messages_payload(),
            "chats": self.store.list_chats(),
        }

    def create_new_chat(self) -> dict:
        with self.lock:
            self.state_dict = _empty_state_dict(self.default_workspace)
            self.current_chat_id = self.store.create_chat(self.state_dict, "Новый чат")
            return self.build_full_payload()

    def switch_chat(self, chat_id: int) -> dict:
        with self.lock:
            self.current_chat_id = chat_id
            self.state_dict = _normalize_state_dict(
                self.store.load_state_dict(chat_id),
                self.default_workspace,
            )
            return self.build_full_payload()

    def delete_chat(self, chat_id: int) -> dict:
        with self.lock:
            self.store.delete_chat(chat_id)
            chats = self.store.list_chats()
            if not chats:
                self.state_dict = _empty_state_dict(self.default_workspace)
                self.current_chat_id = self.store.create_chat(self.state_dict, "Новый чат")
                return self.build_full_payload()
            self.current_chat_id = int(chats[0]["id"])
            self.state_dict = _normalize_state_dict(
                self.store.load_state_dict(self.current_chat_id),
                self.default_workspace,
            )
            return self.build_full_payload()

    def handle_message(self, message: str) -> dict:
        text = message.strip()
        if not text:
            return self.build_full_payload()

        with self.lock:
            self._ensure_current_chat_exists()
            write_runtime_audit(
                "chat_user_message_received",
                chat_id=self.current_chat_id,
                chars=len(text),
                is_command=text.startswith("/"),
                message=text,
            )
            # Save user message
            try:
                self.store.add_message(self.current_chat_id, "user", "Пользователь", text)
            except KeyError:
                self._ensure_current_chat_exists()
                self.store.add_message(self.current_chat_id, "user", "Пользователь", text)

            if text.startswith("/"):
                response_text = self._handle_command(text)
            else:
                response_text = self._process_via_pipeline(text)

            # Save assistant response
            if response_text.strip():
                self.store.add_message(
                    self.current_chat_id, "assistant", "Ответ", response_text.strip()
                )
            if response_text.strip():
                write_runtime_audit(
                    "chat_assistant_message_saved",
                    chat_id=self.current_chat_id,
                    chars=len(response_text.strip()),
                )
            self._save_current_chat()
            return self.build_full_payload()

    def _process_via_pipeline(self, text: str) -> str:
        """Run the LangGraph pipeline for user text and return response."""
        sd = self.state_dict
        effective_text = _expand_suggestion_followup(
            text,
            sd.get("last_suggested_changes", []),
        )
        if effective_text != text:
            write_runtime_audit(
                "chat_prompt_expanded_from_suggestion",
                chat_id=self.current_chat_id,
                original_chars=len(text),
                effective_chars=len(effective_text),
                original_message=text,
                effective_message=effective_text,
            )
        turn_id = new_turn_id()
        write_runtime_audit(
            "chat_pipeline_dispatch",
            chat_id=self.current_chat_id,
            turn_id=turn_id,
            has_current_code=bool(sd.get("current_code", "").strip()),
            has_base_prompt=bool(sd.get("base_prompt", "").strip()),
            target_path=sd.get("target_path", ""),
        )

        try:
            result = self._run_async(
                self.engine.process_message(
                    chat_id=self.current_chat_id,
                    turn_id=turn_id,
                    user_input=effective_text,
                    current_code=sd.get("current_code", ""),
                    base_prompt=sd.get("base_prompt", ""),
                    change_requests=sd.get("change_requests", []),
                    workspace_root=sd.get("workspace_root", self.default_workspace),
                    target_path=sd.get("target_path", ""),
                    awaiting_planner_clarification=sd.get("awaiting_planner_clarification", False),
                    planner_pending_questions=sd.get("planner_pending_questions", []),
                    planner_original_input=sd.get("planner_original_input", ""),
                    planner_clarification_attempts=sd.get("planner_clarification_attempts", 0),
                    active_clarifying_questions=sd.get("last_clarifying_questions", []),
                ),
                track_pipeline=True,
                chat_id=self.current_chat_id,
                turn_id=turn_id,
            )
        except PipelineCancelledError:
            write_runtime_audit(
                "chat_pipeline_cancelled",
                chat_id=self.current_chat_id,
                turn_id=turn_id,
            )
            logger.info(
                "pipeline_cancelled",
                chat_id=self.current_chat_id,
                turn_id=turn_id,
            )
            return "Генерация остановлена пользователем."
        except Exception as exc:
            error_text = str(exc) or repr(exc) or type(exc).__name__
            write_runtime_audit(
                "chat_pipeline_failed",
                chat_id=self.current_chat_id,
                turn_id=turn_id,
                error=error_text,
                error_type=type(exc).__name__,
            )
            logger.error(
                "pipeline_error",
                error=error_text,
                error_type=type(exc).__name__,
                error_repr=repr(exc),
            )
            return f"Ошибка: {error_text}"

        write_runtime_audit(
            "chat_pipeline_result",
            chat_id=self.current_chat_id,
            turn_id=turn_id,
            intent=result.get("intent", ""),
            response_type=result.get("response_type", ""),
            save_success=result.get("save_success", False),
            validation_passed=result.get("validation_passed", False),
            verification_passed=bool(result.get("verification", {}).get("passed", False)),
        )

        # Update state from pipeline result
        sd["current_code"] = result.get("current_code", sd.get("current_code", ""))
        sd["base_prompt"] = result.get("base_prompt", sd.get("base_prompt", ""))
        sd["change_requests"] = result.get("change_requests", sd.get("change_requests", []))
        sd["last_intent"] = result.get("intent", "")
        sd["workspace_root"] = result.get("workspace_root", sd.get("workspace_root", self.default_workspace))
        sd["target_path"] = result.get("target_path", sd.get("target_path", ""))
        sd["awaiting_planner_clarification"] = bool(result.get("awaiting_planner_clarification", False))
        sd["planner_pending_questions"] = list(result.get("planner_pending_questions", []) or [])
        sd["planner_original_input"] = str(result.get("planner_original_input", "") or "")
        try:
            sd["planner_clarification_attempts"] = int(result.get("planner_clarification_attempts", 0) or 0)
        except (TypeError, ValueError):
            sd["planner_clarification_attempts"] = 0
        if result.get("saved_to", "").strip():
            sd["last_saved_path"] = result["saved_to"].strip()
        if result.get("saved_jsonstring_to", "").strip():
            sd["last_saved_jsonstring_path"] = result["saved_jsonstring_to"].strip()
        if result.get("response_type") == "code":
            sd["last_suggested_changes"] = [
                str(item).strip()
                for item in result.get("suggested_changes", [])
                if str(item).strip()
            ]
            sd["last_clarifying_questions"] = [
                str(item).strip()
                for item in result.get("clarifying_questions", [])
                if str(item).strip()
            ]
            explanation = result.get("explanation", {})
            sd["last_explanation"] = explanation if isinstance(explanation, dict) else {}
        else:
            sd["last_suggested_changes"] = []
            sd["last_clarifying_questions"] = []
            sd["last_explanation"] = {}

        return result.get("response", "")

    def _handle_command(self, text: str) -> str:
        command, _, argument = text.partition(" ")
        argument = argument.strip()
        command = command.lower()

        if command in ("/code", "/show"):
            code = self.state_dict.get("current_code", "")
            if code.strip():
                return f"```lua\n{code}\n```"
            return "Нет сгенерированного кода."

        if command == "/status":
            sd = self.state_dict
            lines = [
                f"Задача: {sd.get('base_prompt', '(не задана)')}",
                f"Правки: {len(sd.get('change_requests', []))}",
                f"Код: {'есть' if sd.get('current_code', '').strip() else 'нет'}",
                f"Активный Lua target: {sd.get('target_path', '(не выбран)') or '(не выбран)'}",
                f"Workspace: {sd.get('workspace_root', self.default_workspace)}",
                f"Последнее сохранение: {sd.get('last_saved_path', '(ещё не было)') or '(ещё не было)'}",
                f"Последний JsonString: {sd.get('last_saved_jsonstring_path', '(ещё не было)') or '(ещё не было)'}",
                f"Предложений от системы: {len(sd.get('last_suggested_changes', []))}",
                f"Уточняющих вопросов: {len(sd.get('last_clarifying_questions', []))}",
                f"Последний intent: {sd.get('last_intent', '(не определён)')}",
            ]
            return "\n".join(lines)

        if command == "/path":
            sd = self.state_dict
            target_path = sd.get("target_path", "").strip()
            if not target_path:
                return (
                    f"Активный Lua target ещё не выбран.\n"
                    f"Workspace: {sd.get('workspace_root', self.default_workspace)}"
                )
            return (
                f"Активный Lua target:\n{target_path}\n\n"
                f"Workspace:\n{sd.get('workspace_root', self.default_workspace)}"
            )

        if command == "/prompt":
            sd = self.state_dict
            lines = [f"Задача: {sd.get('base_prompt', '(не задана)')}"]
            for i, cr in enumerate(sd.get("change_requests", []), 1):
                lines.append(f"Правка {i}: {cr}")
            for i, suggestion in enumerate(sd.get("last_suggested_changes", []), 1):
                lines.append(f"Предложение {i}: {suggestion}")
            return "\n".join(lines)

        if command == "/new":
            if not argument:
                return "Использование: /new <задача>"
            # Start a fresh chat context and let the pipeline resolve a new target.
            self.state_dict["current_code"] = ""
            self.state_dict["base_prompt"] = ""
            self.state_dict["change_requests"] = []
            self.state_dict["target_path"] = ""
            self.state_dict["last_saved_path"] = ""
            self.state_dict["last_saved_jsonstring_path"] = ""
            self.state_dict["last_suggested_changes"] = []
            self.state_dict["last_clarifying_questions"] = []
            self.state_dict["last_explanation"] = {}
            self.state_dict["awaiting_planner_clarification"] = False
            self.state_dict["planner_pending_questions"] = []
            self.state_dict["planner_original_input"] = ""
            self.state_dict["planner_clarification_attempts"] = 0
            self.state_dict["workspace_root"] = self.default_workspace
            return self._process_via_pipeline(argument)

        if command == "/edit":
            if not argument:
                return "Использование: /edit <изменение>"
            return self._process_via_pipeline(argument)

        if command == "/retry":
            if not self.state_dict.get("current_code", "").strip():
                return "Нет текущего Lua-кода для повторной проверки."
            retry_request = (
                "Проверь текущий Lua код ещё раз, исправь найденные проблемы, "
                "снова запусти локальную валидацию и проверку требований, "
                "а затем сохрани его в тот же целевой файл, если проверки пройдены."
            )
            return self._process_via_pipeline(retry_request)

        if command == "/help":
            return (
                "Команды:\n"
                "  /new <задача> — новый проект\n"
                "  /edit <изменение> — изменить текущий код\n"
                "  /retry — повторно проверить и поправить текущий код\n"
                "  /code — показать текущий код\n"
                "  /path — показать активный Lua target\n"
                "  /status — статус\n"
                "  /prompt — текущее задание\n"
                "  /help — эта справка\n\n"
                "Обычный текст можно дополнять путём к .lua файлу или директорией, где нужно создать проект.\n"
                "Если система предложила улучшения, можно написать: «примени предложение 1»."
            )

        return "Неизвестная команда. Используй /help."


def make_handler(runtime: AppRuntime):
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            if self.path == "/":
                self.respond_html(HTML_PAGE)
                return
            if self.path == "/api/state":
                self.respond_json(runtime.build_full_payload())
                return
            self.send_error(HTTPStatus.NOT_FOUND, "Not Found")

        def do_POST(self) -> None:
            content_length = int(self.headers.get("Content-Length", "0"))
            raw_body = self.rfile.read(content_length) if content_length else b"{}"
            try:
                payload = json.loads(raw_body.decode("utf-8"))
            except json.JSONDecodeError:
                self.send_error(HTTPStatus.BAD_REQUEST, "Invalid JSON")
                return

            if self.path == "/api/message":
                message = str(payload.get("message", ""))
                response = runtime.handle_message(message)
                self.respond_json(response)
                return

            if self.path == "/api/cancel":
                self.respond_json(runtime.cancel_active_pipeline())
                return

            if self.path == "/api/chats/new":
                self.respond_json(runtime.create_new_chat())
                return

            if self.path == "/api/chats/switch":
                try:
                    chat_id = int(payload.get("chat_id", 0))
                except (TypeError, ValueError):
                    self.send_error(HTTPStatus.BAD_REQUEST, "Invalid chat_id")
                    return
                try:
                    self.respond_json(runtime.switch_chat(chat_id))
                except KeyError:
                    self.send_error(HTTPStatus.NOT_FOUND, "Chat not found")
                return

            if self.path == "/api/chats/delete":
                try:
                    chat_id = int(payload.get("chat_id", 0))
                except (TypeError, ValueError):
                    self.send_error(HTTPStatus.BAD_REQUEST, "Invalid chat_id")
                    return
                try:
                    self.respond_json(runtime.delete_chat(chat_id))
                except KeyError:
                    self.send_error(HTTPStatus.NOT_FOUND, "Chat not found")
                return

            self.send_error(HTTPStatus.NOT_FOUND, "Not Found")

        def log_message(self, format: str, *args) -> None:
            return

        def respond_html(self, body: str) -> None:
            encoded = body.encode("utf-8")
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(encoded)))
            self.end_headers()
            self.wfile.write(encoded)

        def respond_json(self, payload: dict) -> None:
            encoded = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(encoded)))
            self.end_headers()
            self.wfile.write(encoded)

    return Handler


def main() -> int:
    configure_console_utf8()
    args = parse_args()
    if args.max_attempts < 1:
        raise SystemExit("--max-attempts must be at least 1")

    logging_meta = configure_logging(log_dir=args.log_dir, level=args.log_level)
    write_runtime_audit(
        "runtime_logging_configured",
        log_level=logging_meta["log_level"],
        log_dir=logging_meta["log_dir"],
        runtime_log=logging_meta["runtime_log_path"],
        llm_prompt_log=logging_meta["llm_prompt_log_path"],
    )

    runtime = AppRuntime(args)
    server = ThreadingHTTPServer((args.host, args.port), make_handler(runtime))
    url = f"http://{args.host}:{args.port}/"
    print(f"LowCode Lua Script Builder (LangGraph) запущен: {url}")
    print("Для остановки нажми Ctrl+C")

    if not args.no_browser:
        webbrowser.open(url)

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nОстановка UI...")
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
