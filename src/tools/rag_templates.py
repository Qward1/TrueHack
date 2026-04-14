"""Lightweight RAG for Lua generation templates.

Searches the local JSONL knowledge base by template description and returns
only compact Lua template snippets for prompt injection into the code
generator. Uses Ollama embeddings when available and falls back to lexical
ranking when embeddings are unavailable.
"""

from __future__ import annotations

import asyncio
import json
import math
import os
import re
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

import structlog
from openai import AsyncOpenAI

logger = structlog.get_logger(__name__)

DEFAULT_TEMPLATE_KB_PATH = "lua_rag_templates_kb.jsonl"
DEFAULT_TEMPLATE_TOP_K = 5
DEFAULT_TEMPLATE_EMBED_MODEL = "qwen3-embedding:0.6b"
DEFAULT_OLLAMA_BASE_URL = "http://127.0.0.1:11434/v1"

_TOKEN_RE = re.compile(r"[A-Za-zА-Яа-я0-9_]+", re.UNICODE)
_EMBEDDING_CACHE: dict[tuple[str, int, str], list[list[float]]] = {}
_EMBEDDING_LOCK = asyncio.Lock()


@dataclass(frozen=True)
class TemplateMatch:
    id: str
    title: str
    task_type: str
    input_shape: str
    output_shape: str
    retrieval_text: str
    llm_context: str
    score: float


def rag_templates_enabled() -> bool:
    raw = str(os.getenv("RAG_TEMPLATES_ENABLED", "true") or "").strip().lower()
    return raw not in {"0", "false", "off", "no"}


def _resolve_kb_path() -> Path:
    raw = str(os.getenv("RAG_TEMPLATES_KB_PATH", DEFAULT_TEMPLATE_KB_PATH) or "").strip()
    return Path(raw).expanduser()


def _resolve_top_k() -> int:
    raw = str(os.getenv("RAG_TEMPLATES_TOP_K", str(DEFAULT_TEMPLATE_TOP_K)) or "").strip()
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return DEFAULT_TEMPLATE_TOP_K
    return max(1, min(value, 5))


def _resolve_embed_model() -> str:
    for key in ("RAG_TEMPLATES_EMBED_MODEL", "OLLAMA_EMBED_MODEL"):
        value = str(os.getenv(key, "") or "").strip()
        if value:
            return value
    return DEFAULT_TEMPLATE_EMBED_MODEL


def _require_planner_result() -> bool:
    raw = str(os.getenv("RAG_TEMPLATES_REQUIRE_PLANNER", "true") or "").strip().lower()
    return raw not in {"0", "false", "off", "no"}


def _resolve_base_url() -> str:
    return str(os.getenv("OLLAMA_BASE_URL", DEFAULT_OLLAMA_BASE_URL) or "").strip() or DEFAULT_OLLAMA_BASE_URL


def _coerce_str_list(value: Any) -> list[str]:
    if isinstance(value, list):
        result: list[str] = []
        for item in value:
            text = str(item or "").strip()
            if text:
                result.append(text)
        return result
    return []


@lru_cache(maxsize=8)
def _load_kb_cached(path_str: str, mtime_ns: int) -> tuple[dict[str, Any], ...]:
    path = Path(path_str)
    entries: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as handle:
        for line_no, raw_line in enumerate(handle, start=1):
            line = raw_line.strip()
            if not line:
                continue
            try:
                payload = json.loads(line)
            except json.JSONDecodeError:
                logger.warning("rag_templates.invalid_jsonl_line", path=str(path), line_no=line_no)
                continue
            if not isinstance(payload, dict):
                continue
            llm_context = str(payload.get("llm_context", "") or "").strip()
            if not llm_context:
                continue
            payload = dict(payload)
            payload["_search_text"] = _build_entry_search_text(payload)
            entries.append(payload)
    return tuple(entries)


def load_template_kb(path: str | Path | None = None) -> list[dict[str, Any]]:
    resolved = Path(path) if path is not None else _resolve_kb_path()
    if not resolved.exists() or not resolved.is_file():
        return []
    stat = resolved.stat()
    return list(_load_kb_cached(str(resolved.resolve()), stat.st_mtime_ns))


def _build_entry_search_text(entry: dict[str, Any]) -> str:
    parts = [
        str(entry.get("title", "") or "").strip(),
        str(entry.get("task_type", "") or "").strip(),
        str(entry.get("retrieval_text", "") or "").strip(),
        str(entry.get("input_shape", "") or "").strip(),
        str(entry.get("output_shape", "") or "").strip(),
        " ".join(_coerce_str_list(entry.get("use_cases"))),
        " ".join(_coerce_str_list(entry.get("tags"))),
    ]
    return "\n".join(part for part in parts if part)


def _tokenize(text: str) -> list[str]:
    return [token.lower() for token in _TOKEN_RE.findall(str(text or ""))]


def build_template_query(compiled_request: dict[str, Any]) -> str:
    planner = compiled_request.get("planner_result") or {}
    planner = planner if isinstance(planner, dict) else {}

    semantic_expectations = [
        str(item or "").strip()
        for item in compiled_request.get("semantic_expectations", []) or []
        if str(item or "").strip()
    ]
    requested_item_keys = [
        str(item or "").strip()
        for item in compiled_request.get("requested_item_keys", []) or []
        if str(item or "").strip()
    ]
    query_parts = [
        str(compiled_request.get("task_text", "") or "").strip(),
        str(planner.get("reformulated_task", "") or "").strip(),
        str(compiled_request.get("selected_operation", "") or "").strip(),
        str(planner.get("target_operation", "") or "").strip(),
        str(compiled_request.get("selected_primary_type", "") or "").strip(),
        ", ".join(semantic_expectations),
        ", ".join(requested_item_keys),
    ]
    return "\n".join(part for part in query_parts if part)


def _lexical_rank(
    compiled_request: dict[str, Any],
    entries: list[dict[str, Any]],
    *,
    top_k: int,
) -> list[TemplateMatch]:
    query = build_template_query(compiled_request)
    query_tokens = set(_tokenize(query))
    if not query_tokens:
        return []

    selected_operation = str(compiled_request.get("selected_operation", "") or "").strip().lower()
    selected_type = str(compiled_request.get("selected_primary_type", "") or "").strip().lower()
    planner = compiled_request.get("planner_result") or {}
    planner = planner if isinstance(planner, dict) else {}
    planner_operation = str(planner.get("target_operation", "") or "").strip().lower()
    requested_item_keys = {
        item.lower()
        for item in compiled_request.get("requested_item_keys", []) or []
        if str(item or "").strip()
    }

    ranked: list[TemplateMatch] = []
    for entry in entries:
        search_text = str(entry.get("_search_text", "") or "")
        entry_tokens = set(_tokenize(search_text))
        if not entry_tokens:
            continue
        overlap = len(query_tokens & entry_tokens)
        if overlap == 0:
            continue

        score = overlap / math.sqrt(len(query_tokens) * len(entry_tokens))
        task_type = str(entry.get("task_type", "") or "").strip().lower()
        if task_type and task_type in {selected_operation, planner_operation}:
            score += 0.35
        input_shape = str(entry.get("input_shape", "") or "").strip().lower()
        if input_shape and input_shape == selected_type:
            score += 0.2

        tags = {tag.lower() for tag in _coerce_str_list(entry.get("tags"))}
        score += 0.05 * len(requested_item_keys & tags)
        ranked.append(_to_template_match(entry, score))

    ranked.sort(key=lambda item: item.score, reverse=True)
    return ranked[:top_k]


def _cosine_similarity(left: list[float], right: list[float]) -> float:
    if not left or not right or len(left) != len(right):
        return -1.0
    dot = 0.0
    left_norm = 0.0
    right_norm = 0.0
    for left_value, right_value in zip(left, right):
        dot += left_value * right_value
        left_norm += left_value * left_value
        right_norm += right_value * right_value
    if left_norm <= 0.0 or right_norm <= 0.0:
        return -1.0
    return dot / math.sqrt(left_norm * right_norm)


def _to_template_match(entry: dict[str, Any], score: float) -> TemplateMatch:
    return TemplateMatch(
        id=str(entry.get("id", "") or "").strip(),
        title=str(entry.get("title", "") or "").strip(),
        task_type=str(entry.get("task_type", "") or "").strip(),
        input_shape=str(entry.get("input_shape", "") or "").strip(),
        output_shape=str(entry.get("output_shape", "") or "").strip(),
        retrieval_text=str(entry.get("retrieval_text", "") or "").strip(),
        llm_context=str(entry.get("llm_context", "") or "").strip(),
        score=score,
    )


async def _embed_texts(client: AsyncOpenAI, model: str, texts: list[str]) -> list[list[float]]:
    response = await client.embeddings.create(model=model, input=texts)
    vectors: list[list[float]] = []
    for item in response.data:
        vectors.append(list(item.embedding))
    return vectors


async def _get_cached_kb_embeddings(
    *,
    client: AsyncOpenAI,
    path: Path,
    entries: list[dict[str, Any]],
    model: str,
) -> list[list[float]]:
    key = (str(path.resolve()), path.stat().st_mtime_ns, model)
    cached = _EMBEDDING_CACHE.get(key)
    if cached is not None:
        return cached

    async with _EMBEDDING_LOCK:
        cached = _EMBEDDING_CACHE.get(key)
        if cached is not None:
            return cached

        search_texts = [str(entry.get("_search_text", "") or "") for entry in entries]
        embeddings: list[list[float]] = []
        batch_size = 16
        for index in range(0, len(search_texts), batch_size):
            batch = search_texts[index:index + batch_size]
            embeddings.extend(await _embed_texts(client, model, batch))
        _EMBEDDING_CACHE[key] = embeddings
        return embeddings


async def _embedding_rank(
    compiled_request: dict[str, Any],
    entries: list[dict[str, Any]],
    *,
    top_k: int,
    path: Path,
) -> list[TemplateMatch]:
    if not entries:
        return []

    query = build_template_query(compiled_request)
    if not query.strip():
        return []

    model = _resolve_embed_model()
    client = AsyncOpenAI(
        base_url=_resolve_base_url(),
        api_key="local-runtime",
        timeout=60.0,
    )

    query_embedding = (await _embed_texts(client, model, [query]))[0]
    kb_embeddings = await _get_cached_kb_embeddings(client=client, path=path, entries=entries, model=model)

    ranked: list[TemplateMatch] = []
    for entry, vector in zip(entries, kb_embeddings):
        score = _cosine_similarity(query_embedding, vector)
        if score <= 0.0:
            continue
        ranked.append(_to_template_match(entry, score))
    ranked.sort(key=lambda item: item.score, reverse=True)
    return ranked[:top_k]


async def retrieve_template_matches(compiled_request: dict[str, Any]) -> list[TemplateMatch]:
    if not rag_templates_enabled() or not isinstance(compiled_request, dict):
        return []

    planner = compiled_request.get("planner_result") or {}
    if _require_planner_result() and (not isinstance(planner, dict) or not planner):
        return []

    path = _resolve_kb_path()
    if not path.exists() or not path.is_file():
        logger.info("rag_templates.kb_missing", path=str(path))
        return []

    entries = load_template_kb(path)
    if not entries:
        return []

    top_k = _resolve_top_k()
    try:
        matches = await _embedding_rank(compiled_request, entries, top_k=top_k, path=path)
    except Exception as exc:
        logger.warning(
            "rag_templates.embedding_failed",
            path=str(path),
            error=str(exc),
            error_type=type(exc).__name__,
        )
        matches = []

    if matches:
        return matches
    return _lexical_rank(compiled_request, entries, top_k=top_k)


def render_template_prompt_block(matches: list[TemplateMatch]) -> str:
    usable = [match for match in matches if match.llm_context]
    if not usable:
        return ""

    parts = [
        "Relevant Lua template patterns:",
        "Use these only as small structural references. Replace placeholder paths and fields with the exact paths from this task.",
    ]
    for index, match in enumerate(usable, start=1):
        parts.append(f"Template {index}:\n{match.llm_context}")
    return "\n\n".join(parts)


def render_template_selection_prompt(
    compiled_request: dict[str, Any],
    matches: list[TemplateMatch],
) -> str:
    usable = [match for match in matches if match.llm_context]
    if not usable:
        return ""

    planner = compiled_request.get("planner_result") or {}
    planner = planner if isinstance(planner, dict) else {}
    requested_item_keys = [
        str(item or "").strip()
        for item in compiled_request.get("requested_item_keys", []) or []
        if str(item or "").strip()
    ]

    task_parts = [
        str(compiled_request.get("task_text", "") or "").strip(),
        str(planner.get("reformulated_task", "") or "").strip(),
        f"Operation: {str(compiled_request.get('selected_operation', '') or '').strip()}",
        f"Primary type: {str(compiled_request.get('selected_primary_type', '') or '').strip()}",
        (
            "Requested item keys: " + ", ".join(requested_item_keys)
            if requested_item_keys else ""
        ),
    ]
    parts = [
        "Current task:",
        "\n".join(part for part in task_parts if part),
        "",
        "Top retrieved candidates:",
    ]
    for index, match in enumerate(usable, start=1):
        candidate_parts = [
            f"Candidate {index}:",
            f"id: {match.id}",
            f"title: {match.title}",
            f"task_type: {match.task_type}",
            f"input_shape: {match.input_shape}",
            f"output_shape: {match.output_shape}",
            f"retrieval_text: {match.retrieval_text}" if match.retrieval_text else "",
            f"llm_context:\n{match.llm_context}",
        ]
        parts.append("\n".join(part for part in candidate_parts if part))
    return "\n\n".join(part for part in parts if part.strip())
