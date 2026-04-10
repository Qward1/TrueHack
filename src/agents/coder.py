"""Coder agent — generates, refines, and fixes Lua code."""

from __future__ import annotations

import time

import structlog

from src.agents.base import BaseAgent
from src.core.config import Settings
from src.core.llm import LLMProvider
from src.core.state import AgentState
from src.core.utils import extract_lua_code
from src.tools.rag import LuaRAG

logger = structlog.get_logger(__name__)


class CoderAgent(BaseAgent):
    """Three modes: generate / refine / fix."""

    def __init__(self, llm: LLMProvider, settings: Settings, rag: LuaRAG) -> None:
        super().__init__(llm, settings)
        self._rag = rag
        self._gen_template = self._load_prompt("coder_generate")
        self._refine_template = self._load_prompt("coder_refine")
        self._fix_template = self._load_prompt("coder_fix")

    # ── generate ─────────────────────────────────────────────────────
    async def generate(self, state: AgentState) -> AgentState:
        """Generate code for the current task in state.plan."""
        start = time.perf_counter()

        plan: list[dict] = state.get("plan") or []
        idx: int = state.get("current_task_index", 0)
        task = plan[idx] if plan else {
            "id": "task_1",
            "description": state["user_input"],
            "signature": "main()",
        }

        task_id: str = task.get("id", "task_1")
        task_desc: str = task.get("description", state["user_input"])
        signature: str = task.get("signature", "main()")

        # NOTE: RAG is intentionally disabled for code generation. The Lua docs
        # are narrative/pattern text and tend to leak unrelated function names
        # (reverse_string, is_prime, bubble_sort, class patterns, …) into the
        # generated code. RAG is still used by the QA agent for explanations.
        rag_context = ""

        prompt = self._render_prompt(
            self._gen_template,
            rag_context=rag_context,
            task_description=task_desc,
            signature=signature,
        )

        raw = await self._llm.generate(prompt=prompt)
        code = extract_lua_code(raw)

        generated_codes = dict(state.get("generated_codes") or {})
        generated_codes[task_id] = code

        elapsed = time.perf_counter() - start
        logger.info(
            "coder_generate_done",
            task_id=task_id,
            code_len=len(code),
            elapsed_s=round(elapsed, 3),
        )

        return {
            **state,
            "generated_code": code,
            "generated_codes": generated_codes,
            "task_description": task_desc,
            "rag_context": rag_context,
            "current_task_index": idx + 1,
        }

    # ── refine ────────────────────────────────────────────────────────
    async def refine(self, state: AgentState) -> AgentState:
        """Modify existing code according to the user's latest request."""
        start = time.perf_counter()

        existing_code = state.get("assembled_code") or state.get("generated_code", "")

        prompt = self._render_prompt(
            self._refine_template,
            existing_code=existing_code,
            user_message=state["user_input"],
        )

        raw = await self._llm.generate(prompt=prompt)
        code = extract_lua_code(raw)

        elapsed = time.perf_counter() - start
        logger.info("coder_refine_done", code_len=len(code), elapsed_s=round(elapsed, 3))

        return {
            **state,
            "generated_code": code,
            "assembled_code": code,
        }

    # ── fix ───────────────────────────────────────────────────────────
    async def fix(self, state: AgentState) -> AgentState:
        """Fix validation errors in the current code candidate."""
        start = time.perf_counter()

        code = state.get("assembled_code") or state.get("generated_code", "")
        errors = state.get("validation_errors", "")
        task_desc = state.get("task_description") or state.get("user_input", "")
        fix_iterations = state.get("fix_iterations", 0)

        prompt = self._render_prompt(
            self._fix_template,
            validation_errors=errors,
            code=code,
            task_description=task_desc,
        )

        raw = await self._llm.generate(prompt=prompt)
        fixed = extract_lua_code(raw)

        elapsed = time.perf_counter() - start
        logger.info(
            "coder_fix_done",
            iteration=fix_iterations + 1,
            code_len=len(fixed),
            elapsed_s=round(elapsed, 3),
        )

        return {
            **state,
            "generated_code": fixed,
            "assembled_code": fixed,
            "fix_iterations": fix_iterations + 1,
        }

    # ── run (dispatcher) ──────────────────────────────────────────────
    async def run(self, state: AgentState) -> AgentState:
        """Dispatch to generate / refine / fix based on state."""
        intent = state.get("intent", "")
        validation_passed = state.get("validation_passed")
        fix_iterations = state.get("fix_iterations", 0)
        max_fix = self._settings.max_fix_iterations

        if intent == "refine":
            return await self.refine(state)

        if validation_passed is False and fix_iterations < max_fix:
            return await self.fix(state)

        return await self.generate(state)
