"""AgentEngine — top-level orchestrator that wires all components together."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import structlog

from src.core.config import Settings
from src.core.llm import LLMManager
from src.core.state import AgentState
from src.graph.builder import build_graph
from src.storage.chat_repository import ChatRepository
from src.storage.database import Database
from src.tools.lua_executor import LuaExecutor
from src.tools.lua_validator import LuaValidator
from src.tools.rag import LuaRAG

logger = structlog.get_logger(__name__)


class AgentEngine:
    """Wires all components and exposes a simple ``process_message`` API."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings

        # ── Storage ───────────────────────────────────────────────────
        self._db = Database(settings.paths.database)
        self._repo: ChatRepository | None = None  # set after init()

        # ── LLM ───────────────────────────────────────────────────────
        self._llm_manager = LLMManager(settings)

        # ── Tools ─────────────────────────────────────────────────────
        lua_cmd = settings.lua.interpreter
        self._lua_validator = LuaValidator(lua_cmd)
        self._lua_executor = LuaExecutor(lua_cmd)
        self._rag = LuaRAG(
            docs_path=settings.paths.lua_docs,
            index_path=settings.paths.rag_index,
        )

        # ── Agents ────────────────────────────────────────────────────
        # Import here to avoid circular imports at module level
        from src.agents.coder import CoderAgent
        from src.agents.planner import PlannerAgent
        from src.agents.qa import QAAgent
        from src.agents.router import RouterAgent
        from src.agents.validator import ValidatorAgent

        self._agents = {
            "router": RouterAgent(
                llm=self._llm_manager.get_provider("router"),
                settings=settings,
            ),
            "planner": PlannerAgent(
                llm=self._llm_manager.get_provider("planner"),
                settings=settings,
            ),
            "coder": CoderAgent(
                llm=self._llm_manager.get_provider("coder"),
                settings=settings,
                rag=self._rag,
            ),
            "validator": ValidatorAgent(
                llm=self._llm_manager.get_provider("validator"),
                settings=settings,
                lua_validator=self._lua_validator,
            ),
            "qa": QAAgent(
                llm=self._llm_manager.get_provider("qa"),
                settings=settings,
                rag=self._rag,
            ),
        }

        # ── Graph ─────────────────────────────────────────────────────
        self._graph = build_graph(self._agents, settings)
        logger.info("engine_created")

    # ── Lifecycle ─────────────────────────────────────────────────────

    async def initialize(self) -> None:
        """Open the database and ensure the RAG index exists."""
        await self._db.init()
        self._repo = ChatRepository(self._db)
        logger.info("db_ready", path=self._settings.paths.database)

        # Build RAG index if it doesn't exist yet
        index_file = Path(self._settings.paths.rag_index) / "index.npy"
        if not index_file.exists():
            logger.info("rag_index_missing_building")
            self._rag.build_index()
        else:
            logger.info("rag_index_found")

    async def close(self) -> None:
        """Close the database connection."""
        await self._db.close()

    # ── Message processing ────────────────────────────────────────────

    async def process_message(self, chat_id: str, user_message: str) -> dict[str, Any]:
        """Run the full agent pipeline for one user turn.

        Returns a dict with keys:
        ``response``, ``response_type``, ``code`` (optional),
        ``validation_passed`` (optional), ``validation_errors`` (optional).
        """
        if self._repo is None:
            raise RuntimeError("AgentEngine not initialised — call await engine.initialize() first.")

        # Persist user message
        await self._repo.add_message(chat_id, "user", user_message)

        # Load recent context
        recent = await self._repo.get_recent_messages(
            chat_id, limit=self._settings.max_chat_history
        )
        messages = [{"role": m.role, "content": m.content} for m in recent]

        # Load previous code so refine / fix_error have something to operate on.
        # CRITICAL: without this, refine gets an empty existing_code and the
        # model rewrites from scratch, losing functions that were not mentioned
        # in the user's edit request.
        previous_code = await self._repo.get_latest_code(chat_id) or ""
        has_previous_code = bool(previous_code)

        # Build initial state
        initial_state: AgentState = {
            "chat_id": chat_id,
            "messages": messages,
            "user_input": user_message,
            "intent": "",
            "plan": [],
            "current_task_index": 0,
            "task_description": "",
            "generated_code": previous_code,
            "generated_codes": {},
            "assembled_code": previous_code,
            "fix_iterations": 0,
            "validation_passed": False,
            "validation_errors": "",
            "rag_context": "",
            "response": "",
            "response_type": "text",
            "metadata": {"has_previous_code": has_previous_code},
        }

        logger.info("engine_processing", chat_id=chat_id, msg_len=len(user_message))

        # Run the graph
        result: AgentState = await self._graph.ainvoke(initial_state)

        response = result.get("response", "")
        response_type = result.get("response_type", "text")
        assembled = result.get("assembled_code", "")

        # Persist assistant response
        msg = await self._repo.add_message(chat_id, "assistant", response)

        # Persist code artifact if code was generated
        if assembled:
            await self._repo.save_code_artifact(
                chat_id=chat_id,
                message_id=msg.id,
                code=assembled,
                validation_status="passed" if result.get("validation_passed") else "failed",
                test_results=None,
            )

        output: dict[str, Any] = {
            "response": response,
            "response_type": response_type,
        }
        if assembled:
            output["code"] = assembled
            output["validation_passed"] = result.get("validation_passed", False)
            output["validation_errors"] = result.get("validation_errors", "")

        logger.info("engine_done", chat_id=chat_id, response_type=response_type)
        return output

    # ── Code execution ────────────────────────────────────────────────

    async def execute_code(self, code: str) -> dict[str, Any]:
        """Run Lua code through the sandboxed executor."""
        return await self._lua_executor.execute(code)
