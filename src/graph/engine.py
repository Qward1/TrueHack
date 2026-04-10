"""PipelineEngine — canonical API for the Lua generation pipeline."""

from __future__ import annotations

import os
from typing import Any

import structlog

from src.core.llm import LLMProvider
from src.core.state import PipelineState
from src.graph.builder import build_graph

logger = structlog.get_logger(__name__)

DEFAULT_MAX_FIX = 5


class PipelineEngine:
    """Wraps the compiled LangGraph pipeline with a simple message API."""

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
        workspace_root: str = "",
        target_path: str = "",
    ) -> dict[str, Any]:
        """Run the full pipeline for one user turn."""
        initial_workspace = os.path.abspath(workspace_root or os.getcwd())
        initial_target = os.path.abspath(target_path) if target_path else ""
        initial_state: PipelineState = {
            "chat_id": chat_id,
            "user_input": user_input,
            "workspace_root": initial_workspace,
            "target_path": initial_target,
            "target_directory": os.path.dirname(initial_target) if initial_target else initial_workspace,
            "target_explicit": False,
            "intent": "",
            "base_prompt": base_prompt,
            "change_requests": change_requests or [],
            "current_code": current_code,
            "generated_code": "",
            "diagnostics": {},
            "validation_passed": False,
            "fix_iterations": 0,
            "max_fix_iterations": self._max_fix,
            "verification": {},
            "verification_passed": False,
            "save_success": False,
            "save_error": "",
            "saved_to": "",
            "response": "",
            "response_type": "text",
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
            "workspace_root": result.get("workspace_root", initial_workspace),
            "target_path": result.get("target_path", initial_target),
            "save_success": result.get("save_success", False),
            "save_error": result.get("save_error", ""),
            "saved_to": result.get("saved_to", ""),
        }

        logger.info(
            "pipeline_done",
            chat_id=chat_id,
            intent=output["intent"],
            response_type=output["response_type"],
            validation_passed=output["validation_passed"],
            target_path=output["target_path"],
            save_success=output["save_success"],
        )
        return output
