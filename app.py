#!/usr/bin/env python3
import argparse
import asyncio
import json
import os
import sqlite3
import threading
import webbrowser
from contextlib import closing
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import structlog

from console_utils import configure_console_utf8
from src.core.llm import LLMProvider
from src.graph.engine import PipelineEngine

logger = structlog.get_logger(__name__)

# ── Defaults (kept compatible with CLI args) ─────────────────────────
DEFAULT_URL = "http://127.0.0.1:1234/v1"
DEFAULT_MODEL = "local-model"
DEFAULT_OUTPUT = "generated.lua"
DEFAULT_MAX_ATTEMPTS = 5
DEFAULT_TEMPERATURE = 0.2
DEFAULT_REQUEST_TIMEOUT = 600.0


CHAT_DB_NAME = ".lua_console_chats.db"
MAX_CHAT_TITLE_LENGTH = 72


def utc_now_iso() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _empty_state_dict() -> dict:
    """Return a fresh empty state dict for serialization."""
    return {
        "base_prompt": "",
        "change_requests": [],
        "current_code": "",
        "output_path": DEFAULT_OUTPUT,
        "last_intent": "",
    }


def _derive_title(base_prompt: str, fallback: str = "Новый чат") -> str:
    """Build a short chat title from the base prompt."""
    if not base_prompt.strip():
        return fallback
    single_line = " ".join(base_prompt.split())
    if len(single_line) <= MAX_CHAT_TITLE_LENGTH:
        return single_line
    return f"{single_line[: MAX_CHAT_TITLE_LENGTH - 3].rstrip()}..."


class ChatStore:
    """SQLite storage for chats and messages (kept from original, simplified state)."""

    def __init__(self, db_path: str) -> None:
        self.db_path = db_path
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.db_path)
        connection.row_factory = sqlite3.Row
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
  <title>Lua Console Builder</title>
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
          <h1>Lua Console Builder</h1>
          <p>Локальный чат для генерации, редактирования и проверки Lua-скриптов. Внутри остается та же логика: запуск через <code>lua</code>, проверка через <code>luacheck</code>, автопочинка и контекст чата.</p>
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
        <h2>Последний Код</h2>
        <div class="code-box" id="codeBox"><span class="ghost">Код появится после первой генерации или команды “Показать Код”.</span></div>
      </div>
    </aside>
  </div>

  <script>
    const timeline = document.getElementById("timeline");
    const composer = document.getElementById("composer");
    const input = document.getElementById("messageInput");
    const sendButton = document.getElementById("sendButton");
    const codeBox = document.getElementById("codeBox");
    const statusBadge = document.getElementById("statusBadge");
    const chatList = document.getElementById("chatList");
    const newChatButton = document.getElementById("newChatButton");
    let activeChatId = null;
    let stopThinkingIndicator = null;

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
        normalized.includes("readme")
        || normalized.includes("документац")
        || normalized.includes("инструкц")
        || normalized.includes("руководств")
      ) {
        return [
          "Определяет тип документа",
          "Собирает контекст проекта",
          "Пишет структуру README",
          "Проверяет итоговый текст",
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
      hint.textContent = "Показываются этапы работы, а не скрытые внутренние рассуждения.";

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
      stopThinkingIndicator = () => {
        window.clearInterval(timer);
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
          addMessage("system", "Новый Чат", "Контекст пуст. Опиши задачу внизу, и приложение создаст или обновит Lua-файл по тем же правилам, что и в консоли.");
        }
        return;
      }
      for (const item of messages) {
        addMessage(item.role || "system", item.title || "Сообщение", item.content || "");
      }
    }

    function setBusy(isBusy) {
      sendButton.disabled = isBusy;
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
      } else {
        delete codeBox.dataset.filled;
        codeBox.innerHTML = '<span class="ghost">Код появится после первой генерации или команды “Показать Код”.</span>';
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
    parser = argparse.ArgumentParser(description="Lua Console Builder — LangGraph edition.")
    parser.add_argument("--host", default="127.0.0.1", help="Host to bind the local web UI.")
    parser.add_argument("--port", type=int, default=8765, help="Port for the local web UI.")
    parser.add_argument("--no-browser", action="store_true", help="Do not auto-open the browser.")
    parser.add_argument("--output", default=DEFAULT_OUTPUT, help="Fallback output Lua file path.")
    parser.add_argument("--model", default=os.getenv("LMSTUDIO_MODEL", DEFAULT_MODEL), help="Model name loaded in LM Studio.")
    parser.add_argument("--url", default=os.getenv("LMSTUDIO_URL", DEFAULT_URL), help="LM Studio base URL.")
    parser.add_argument("--max-attempts", type=int, default=DEFAULT_MAX_ATTEMPTS, help="Max validation/fix iterations.")
    parser.add_argument("--request-timeout", type=float, default=DEFAULT_REQUEST_TIMEOUT, help="Seconds to wait for LM Studio.")
    return parser.parse_args()


class AppRuntime:
    """Web UI runtime backed by the LangGraph PipelineEngine."""

    def __init__(self, args: argparse.Namespace) -> None:
        self.args = args
        self.lock = threading.Lock()
        self.store = ChatStore(os.path.join(os.getcwd(), CHAT_DB_NAME))

        # Create the async event loop for the engine
        self._loop = asyncio.new_event_loop()
        self._loop_thread = threading.Thread(target=self._loop.run_forever, daemon=True)
        self._loop_thread.start()

        # LangGraph pipeline engine
        llm = LLMProvider(
            base_url=getattr(args, "url", DEFAULT_URL),
            model=getattr(args, "model", DEFAULT_MODEL),
            timeout=getattr(args, "request_timeout", DEFAULT_REQUEST_TIMEOUT),
        )
        self.engine = PipelineEngine(
            llm=llm,
            max_fix_iterations=getattr(args, "max_attempts", DEFAULT_MAX_ATTEMPTS),
        )

        # Chat state (simple dict now)
        self.state_dict: dict = _empty_state_dict()
        chats = self.store.list_chats()
        if chats:
            self.current_chat_id = int(chats[0]["id"])
            self.state_dict = self.store.load_state_dict(self.current_chat_id)
        else:
            self.current_chat_id = self.store.create_chat(self.state_dict, "Новый чат")

    def _run_async(self, coro) -> any:
        """Run an async coroutine from sync context via the background event loop."""
        future = asyncio.run_coroutine_threadsafe(coro, self._loop)
        return future.result(timeout=660)  # generous timeout

    def _save_current_chat(self, title: str | None = None) -> None:
        effective_title = title or _derive_title(self.state_dict.get("base_prompt", ""))
        self.store.save_chat_state(self.current_chat_id, self.state_dict, effective_title)

    def build_messages_payload(self) -> list[dict]:
        return self.store.load_messages(self.current_chat_id)

    def build_state_payload(self) -> dict:
        sd = self.state_dict
        return {
            "has_project": bool(sd.get("current_code", "").strip() or sd.get("base_prompt", "").strip()),
            "base_prompt": sd.get("base_prompt", ""),
            "change_requests": sd.get("change_requests", []),
            "current_code": sd.get("current_code", ""),
            "output_path": sd.get("output_path", DEFAULT_OUTPUT),
            "last_intent": sd.get("last_intent", ""),
            "change_requests_count": len(sd.get("change_requests", [])),
        }

    def build_full_payload(self) -> dict:
        return {
            "active_chat_id": self.current_chat_id,
            "state": self.build_state_payload(),
            "messages": self.build_messages_payload(),
            "chats": self.store.list_chats(),
        }

    def create_new_chat(self) -> dict:
        with self.lock:
            self.state_dict = _empty_state_dict()
            self.current_chat_id = self.store.create_chat(self.state_dict, "Новый чат")
            return self.build_full_payload()

    def switch_chat(self, chat_id: int) -> dict:
        with self.lock:
            self.current_chat_id = chat_id
            self.state_dict = self.store.load_state_dict(chat_id)
            return self.build_full_payload()

    def delete_chat(self, chat_id: int) -> dict:
        with self.lock:
            self.store.delete_chat(chat_id)
            chats = self.store.list_chats()
            if not chats:
                self.state_dict = _empty_state_dict()
                self.current_chat_id = self.store.create_chat(self.state_dict, "Новый чат")
                return self.build_full_payload()
            self.current_chat_id = int(chats[0]["id"])
            self.state_dict = self.store.load_state_dict(self.current_chat_id)
            return self.build_full_payload()

    def handle_message(self, message: str) -> dict:
        text = message.strip()
        if not text:
            return self.build_full_payload()

        with self.lock:
            # Save user message
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
            self._save_current_chat()
            return self.build_full_payload()

    def _process_via_pipeline(self, text: str) -> str:
        """Run the LangGraph pipeline for user text and return response."""
        sd = self.state_dict

        # Build message history for context
        raw_messages = self.store.load_messages(self.current_chat_id)
        messages = [
            {"role": m["role"], "content": m["content"]}
            for m in raw_messages[-10:]  # last 10 messages for context
        ]

        try:
            result = self._run_async(
                self.engine.process_message(
                    chat_id=self.current_chat_id,
                    user_input=text,
                    current_code=sd.get("current_code", ""),
                    base_prompt=sd.get("base_prompt", ""),
                    change_requests=sd.get("change_requests", []),
                    messages=messages,
                    output_path=sd.get("output_path", DEFAULT_OUTPUT),
                )
            )
        except Exception as exc:
            logger.error("pipeline_error", error=str(exc))
            return f"Ошибка: {exc}"

        # Update state from pipeline result
        sd["current_code"] = result.get("current_code", sd.get("current_code", ""))
        sd["base_prompt"] = result.get("base_prompt", sd.get("base_prompt", ""))
        sd["change_requests"] = result.get("change_requests", sd.get("change_requests", []))
        sd["last_intent"] = result.get("intent", "")

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
                f"Последний intent: {sd.get('last_intent', '(не определён)')}",
            ]
            return "\n".join(lines)

        if command == "/prompt":
            sd = self.state_dict
            lines = [f"Задача: {sd.get('base_prompt', '(не задана)')}"]
            for i, cr in enumerate(sd.get("change_requests", []), 1):
                lines.append(f"Правка {i}: {cr}")
            return "\n".join(lines)

        if command == "/new":
            if not argument:
                return "Использование: /new <задача>"
            # Force intent to "create" by clearing current code
            self.state_dict["current_code"] = ""
            self.state_dict["base_prompt"] = ""
            self.state_dict["change_requests"] = []
            return self._process_via_pipeline(argument)

        if command == "/edit":
            if not argument:
                return "Использование: /edit <изменение>"
            return self._process_via_pipeline(argument)

        if command == "/help":
            return (
                "Команды:\n"
                "  /new <задача> — новый проект\n"
                "  /edit <изменение> — изменить текущий код\n"
                "  /code — показать текущий код\n"
                "  /status — статус\n"
                "  /prompt — текущее задание\n"
                "  /help — эта справка\n\n"
                "Или просто напиши задачу / правки обычным текстом."
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

    runtime = AppRuntime(args)
    server = ThreadingHTTPServer((args.host, args.port), make_handler(runtime))
    url = f"http://{args.host}:{args.port}/"
    print(f"Lua Console Builder (LangGraph) запущен: {url}")
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
