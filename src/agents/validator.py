"""Validator agent — static analysis + LLM review of Lua code."""

from __future__ import annotations

import json
import time

import structlog

from src.agents.base import BaseAgent
from src.core.config import Settings
from src.core.llm import LLMProvider
from src.core.state import AgentState
from src.tools.lua_validator import LuaValidator

logger = structlog.get_logger(__name__)

_REVIEW_SCHEMA = {
    "passed": "boolean",
    "issues": [{"line": "number", "message": "string", "severity": "string"}],
}

_REVIEW_FALLBACK = {
    "passed": False,
    "issues": [{"line": 0, "message": "LLM review unavailable", "severity": "warning"}],
}


class ValidatorAgent(BaseAgent):
    """Two-stage validation: lua54 syntax/lint → LLM logical review."""

    def __init__(
        self,
        llm: LLMProvider,
        settings: Settings,
        lua_validator: LuaValidator,
    ) -> None:
        super().__init__(llm, settings)
        self._lua_validator = lua_validator
        self._template = self._load_prompt("validator_review")

    async def run(self, state: AgentState) -> AgentState:
        """Validate code in state; update ``validation_passed`` and ``validation_errors``."""
        start = time.perf_counter()

        code = state.get("assembled_code") or state.get("generated_code", "")
        task_desc = state.get("task_description") or state.get("user_input", "")

        if not code.strip():
            logger.warning("validator_no_code")
            return {
                **state,
                "validation_passed": False,
                "validation_errors": "No code to validate.",
            }

        # ── Stage 1: static analysis ──────────────────────────────────
        static = await self._lua_validator.validate(code)
        syntax_errors = [i for i in static["issues"] if i["severity"] == "error" and i["type"] == "syntax"]

        if syntax_errors:
            error_msg = "; ".join(
                f"line {e['line']}: {e['message']}" for e in syntax_errors
            )
            elapsed = time.perf_counter() - start
            logger.info(
                "validator_syntax_fail",
                n_errors=len(syntax_errors),
                elapsed_s=round(elapsed, 3),
            )
            return {
                **state,
                "validation_passed": False,
                "validation_errors": error_msg,
            }

        # ── Stage 2: LLM logical review ───────────────────────────────
        static_summary = json.dumps(static["issues"], ensure_ascii=False)

        prompt = self._render_prompt(
            self._template,
            static_analysis=static_summary,
            code=code,
            task_description=task_desc,
        )

        review = await self._llm.generate_structured(
            prompt=prompt,
            system="You are a Lua code reviewer. Respond with JSON only.",
            schema=_REVIEW_SCHEMA,
            fallback=_REVIEW_FALLBACK,
        )

        passed: bool = bool(review.get("passed", False))
        llm_issues: list[dict] = review.get("issues", [])

        # Combine static lint warnings with LLM issues for the error string
        all_issues = static["issues"] + [
            {**i, "type": "llm"} for i in llm_issues if i.get("severity") == "error"
        ]
        error_parts = [
            f"line {i.get('line', 0)}: {i.get('message', '')}"
            for i in all_issues
            if i.get("severity") == "error"
        ]
        validation_errors = "; ".join(error_parts)

        elapsed = time.perf_counter() - start
        logger.info(
            "validator_done",
            passed=passed,
            n_issues=len(all_issues),
            elapsed_s=round(elapsed, 3),
        )

        return {
            **state,
            "validation_passed": passed,
            "validation_errors": validation_errors,
        }
