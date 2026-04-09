"""QA agent — answers questions, explains code."""

from __future__ import annotations

import time

import structlog

from src.agents.base import BaseAgent
from src.core.config import Settings
from src.core.llm import LLMProvider
from src.core.state import AgentState
from src.tools.rag import LuaRAG

logger = structlog.get_logger(__name__)


class QAAgent(BaseAgent):
    """Handles question / explain / general intents."""

    def __init__(self, llm: LLMProvider, settings: Settings, rag: LuaRAG) -> None:
        super().__init__(llm, settings)
        self._rag = rag
        self._answer_template = self._load_prompt("qa_answer")
        self._explain_template = self._load_prompt("qa_explain")

    async def run(self, state: AgentState) -> AgentState:
        """Build a response based on intent."""
        start = time.perf_counter()
        intent = state.get("intent", "question")

        if intent == "explain":
            response = await self._explain(state)
        elif intent == "question":
            response = await self._answer(state, use_rag=True)
        else:  # "general" or anything else
            response = await self._answer(state, use_rag=False)

        elapsed = time.perf_counter() - start
        logger.info(
            "qa_done",
            intent=intent,
            response_len=len(response),
            elapsed_s=round(elapsed, 3),
        )

        return {**state, "response": response, "response_type": "text"}

    # ── private ───────────────────────────────────────────────────────

    async def _explain(self, state: AgentState) -> str:
        """Explain the most recent code from the conversation."""
        # Find the latest code: assembled > generated > last assistant message containing ```
        code = (
            state.get("assembled_code")
            or state.get("generated_code")
            or self._extract_code_from_history(state)
        )

        prompt = self._render_prompt(
            self._explain_template,
            code=code,
            user_message=state["user_input"],
        )
        return await self._llm.generate(prompt=prompt)

    async def _answer(self, state: AgentState, *, use_rag: bool) -> str:
        """Answer a general or Lua question, optionally enriched with RAG."""
        rag_context = ""
        if use_rag:
            results = await self._rag.search(state["user_input"], top_k=3)
            rag_context = "\n\n".join(r["text"] for r in results)

        # Include recent code as context if it exists
        code = state.get("assembled_code") or state.get("generated_code", "")
        code_context = (
            f"Code from this conversation:\n```lua\n{code}\n```" if code else ""
        )

        prompt = self._render_prompt(
            self._answer_template,
            rag_context=rag_context,
            code_context=code_context,
            user_message=state["user_input"],
        )
        return await self._llm.generate(prompt=prompt)

    @staticmethod
    def _extract_code_from_history(state: AgentState) -> str:
        """Scan message history for the last ```lua block."""
        for msg in reversed(state.get("messages", [])):
            if msg["role"] != "assistant":
                continue
            content = msg["content"]
            start = content.find("```lua")
            if start != -1:
                end = content.find("```", start + 6)
                if end != -1:
                    return content[start + 6 : end].strip()
        return ""
