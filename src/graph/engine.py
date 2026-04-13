"""PipelineEngine — canonical API for the Lua generation pipeline."""

from __future__ import annotations

import os
from typing import Any

import structlog

from src.core.llm import LLMProvider
from src.core.logging_runtime import bind_log_context, clear_log_context, new_turn_id, write_runtime_audit
from src.core.state import PipelineState
from src.graph.builder import build_graph

logger = structlog.get_logger(__name__)

DEFAULT_MAX_FIX = 3


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
        logger.info("[PipelineEngine] created", max_fix_iterations=max_fix_iterations)

    async def process_message(
        self,
        *,
        chat_id: int,
        user_input: str,
        turn_id: str = "",
        current_code: str = "",
        base_prompt: str = "",
        change_requests: list[str] | None = None,
        workspace_root: str = "",
        target_path: str = "",
        awaiting_planner_clarification: bool = False,
        planner_pending_questions: list[str] | None = None,
        planner_original_input: str = "",
        planner_clarification_attempts: int = 0,
        active_clarifying_questions: list[str] | None = None,
    ) -> dict[str, Any]:
        """Run the full pipeline for one user turn."""
        resolved_turn_id = turn_id or new_turn_id()
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
            "compiled_request": {},
            "current_code": current_code,
            "generated_code": "",
            "failure_stage": "",
            "diagnostics": {},
            "validation_passed": False,
            "fix_iterations": 0,
            "fix_verification_iterations": 0,
            "max_fix_iterations": self._max_fix,
            "verification": {},
            "verification_passed": False,
            "save_success": False,
            "save_skipped": False,
            "save_skip_reason": "",
            "save_error": "",
            "saved_to": "",
            "saved_jsonstring_to": "",
            "explanation": {},
            "suggested_changes": [],
            "clarifying_questions": [],
            "active_clarifying_questions": list(active_clarifying_questions or []),
            "response": "",
            "response_type": "text",
            "planner_result": {},
            "planner_skipped": False,
            "awaiting_planner_clarification": awaiting_planner_clarification,
            "planner_pending_questions": list(planner_pending_questions or []),
            "planner_original_input": planner_original_input,
            "planner_clarification_attempts": planner_clarification_attempts,
        }

        clear_log_context()
        bind_log_context(chat_id=chat_id, turn_id=resolved_turn_id)
        try:
            logger.info(
                "[PipelineEngine] pipeline_start",
                chat_id=chat_id,
                msg_len=len(user_input),
                has_code=bool(current_code.strip()),
                target_path=initial_target or "(none)",
            )
            write_runtime_audit(
                "pipeline_start",
                chat_id=chat_id,
                turn_id=resolved_turn_id,
                msg_len=len(user_input),
                has_code=bool(current_code.strip()),
                target_path=initial_target or "(none)",
                user_prompt=user_input,
            )

            try:
                graph_config = {"recursion_limit": 80}
                result: PipelineState = await self._graph.ainvoke(initial_state, config=graph_config)
            except Exception:
                write_runtime_audit(
                    "pipeline_failed",
                    chat_id=chat_id,
                    turn_id=resolved_turn_id,
                )
                logger.exception("[PipelineEngine] pipeline_failed")
                raise

            output = {
                "response": result.get("response", ""),
                "response_type": result.get("response_type", "text"),
                "current_code": result.get("current_code", current_code),
                "generated_code": result.get("generated_code", ""),
                "intent": result.get("intent", ""),
                "base_prompt": result.get("base_prompt", base_prompt),
                "change_requests": result.get("change_requests", change_requests or []),
                "compiled_request": result.get("compiled_request", {}),
                "validation_passed": result.get("validation_passed", False),
                "diagnostics": result.get("diagnostics", {}),
                "verification": result.get("verification", {}),
                "workspace_root": result.get("workspace_root", initial_workspace),
                "target_path": result.get("target_path", initial_target),
                "save_success": result.get("save_success", False),
                "save_skipped": result.get("save_skipped", False),
                "save_skip_reason": result.get("save_skip_reason", ""),
                "save_error": result.get("save_error", ""),
                "saved_to": result.get("saved_to", ""),
                "saved_jsonstring_to": result.get("saved_jsonstring_to", ""),
                "explanation": result.get("explanation", {}),
                "suggested_changes": result.get("suggested_changes", []),
                "clarifying_questions": result.get("clarifying_questions", []),
                "planner_result": result.get("planner_result", {}),
                "planner_skipped": result.get("planner_skipped", False),
                "awaiting_planner_clarification": result.get("awaiting_planner_clarification", False),
                "planner_pending_questions": result.get("planner_pending_questions", []),
                "planner_original_input": result.get("planner_original_input", ""),
                "planner_clarification_attempts": result.get("planner_clarification_attempts", 0),
            }

            logger.info(
                "[PipelineEngine] pipeline_done",
                chat_id=chat_id,
                intent=output["intent"],
                response_type=output["response_type"],
                validation_passed=output["validation_passed"],
                target_path=output["target_path"],
                save_success=output["save_success"],
                save_error=output["save_error"] or "none",
                response_len=len(output["response"]),
            )
            write_runtime_audit(
                "pipeline_done",
                chat_id=chat_id,
                turn_id=resolved_turn_id,
                intent=output["intent"],
                response_type=output["response_type"],
                validation_passed=output["validation_passed"],
                target_path=output["target_path"],
                save_success=output["save_success"],
                save_error=output["save_error"] or "none",
                response_len=len(output["response"]),
            )
            return output
        finally:
            clear_log_context()
