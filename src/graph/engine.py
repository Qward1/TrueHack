"""PipelineEngine — top-level API that replaces main.py orchestration.

Exposes a single ``process_message(chat_id, user_input, ...)`` method
that runs the full LangGraph pipeline and returns a response dict.
"""

from __future__ import annotations

from typing import Any

import structlog

from src.core.llm import LLMProvider
from src.core.state import PipelineState
from src.graph.builder import build_graph

logger = structlog.get_logger(__name__)

DEFAULT_MAX_FIX = 5


class PipelineEngine:
    """Wraps the LangGraph-compiled pipeline with a simple message API."""

    def __init__(
        self,
        llm: LLMProvider | None = None,
        max_fix_iterations: int = DEFAULT_MAX_FIX,
    ) -> None:
        self._llm = llm or LLMProvider()
        self._max_fix = max_fix_iterations
        self._graph = build_graph(self._llm)
        logger.info("pipeline_engine_created")

    async def process_message(
        self,
        *,
        chat_id: int,
        user_input: str,
        current_code: str = "",
        base_prompt: str = "",
        change_requests: list[str] | None = None,
        messages: list[dict] | None = None,
        output_path: str = "generated.lua",
    ) -> dict[str, Any]:
        """Run the full pipeline for one user turn.

        Parameters are loaded from the chat store by the caller (app.py).

        Returns a dict with at least:
            response (str), response_type (str),
            current_code (str — updated if code was generated),
            intent (str), base_prompt (str), change_requests (list).
        """
        initial_state: PipelineState = {
            "chat_id": chat_id,
            "messages": messages or [],
            "user_input": user_input,
            "intent": "",
            "base_prompt": base_prompt,
            "change_requests": change_requests or [],
            "output_path": output_path,
            "artifact_type": "lua",
            "current_code": current_code,
            "generated_code": "",
            "diagnostics": {},
            "validation_passed": False,
            "fix_iterations": 0,
            "max_fix_iterations": self._max_fix,
            "verification": {},
            "verification_passed": False,
            "response": "",
            "response_type": "text",
            "metadata": {},
        }

        logger.info("pipeline_start", chat_id=chat_id, msg_len=len(user_input))

        result: PipelineState = await self._graph.ainvoke(initial_state)

        output = {
            "response": result.get("response", ""),
            "response_type": result.get("response_type", "text"),
            "current_code": result.get("current_code", current_code),
            "generated_code": result.get("generated_code", ""),
            "intent": result.get("intent", ""),
            "base_prompt": result.get("base_prompt", base_prompt),
            "change_requests": result.get("change_requests", change_requests or []),
            "validation_passed": result.get("validation_passed", False),
            "diagnostics": result.get("diagnostics", {}),
            "verification": result.get("verification", {}),
        }

        logger.info(
            "pipeline_done",
            chat_id=chat_id,
            intent=output["intent"],
            response_type=output["response_type"],
            validation_passed=output["validation_passed"],
        )

        return output
