"""LangGraph node functions.

All agents are injected via ``create_nodes(agents)`` — no agent is
instantiated inside a node function.
"""

from __future__ import annotations

from typing import Any, Callable

import structlog

from src.core.state import AgentState

logger = structlog.get_logger(__name__)


def create_nodes(agents: dict[str, Any]) -> dict[str, Callable]:
    """Build node callables from a pre-constructed agent dict.

    Expected keys: ``router``, ``planner``, ``coder``, ``validator``, ``qa``.
    Returns a mapping ``node_name -> async callable``.
    """
    router = agents["router"]
    planner = agents["planner"]
    coder = agents["coder"]
    validator = agents["validator"]
    qa = agents["qa"]

    # ── route_intent ──────────────────────────────────────────────────
    async def route_intent(state: AgentState) -> dict:
        result = await router.run(state)
        return {"intent": result["intent"]}

    # ── plan_task ─────────────────────────────────────────────────────
    async def plan_task(state: AgentState) -> dict:
        result = await planner.run(state)
        update: dict = {}
        for key in ("plan", "clarification_questions", "response",
                    "response_type", "current_task_index", "generated_codes"):
            if key in result:
                update[key] = result[key]
        return update

    # ── generate_code ─────────────────────────────────────────────────
    async def generate_code(state: AgentState) -> dict:
        result = await coder.generate(state)
        return {
            "generated_code": result["generated_code"],
            "generated_codes": result["generated_codes"],
            "current_task_index": result["current_task_index"],
            "task_description": result.get("task_description", ""),
            "rag_context": result.get("rag_context", ""),
        }

    # ── validate_code ─────────────────────────────────────────────────
    async def validate_code(state: AgentState) -> dict:
        result = await validator.run(state)
        return {
            "validation_passed": result["validation_passed"],
            "validation_errors": result["validation_errors"],
        }

    # ── fix_code ──────────────────────────────────────────────────────
    async def fix_code(state: AgentState) -> dict:
        result = await coder.fix(state)
        return {
            "generated_code": result["generated_code"],
            "assembled_code": result.get("assembled_code", result["generated_code"]),
            "fix_iterations": result["fix_iterations"],
        }

    # ── assemble_code ─────────────────────────────────────────────────
    async def assemble_code(state: AgentState) -> dict:
        result = planner.assemble(state)  # synchronous
        return {
            "assembled_code": result["assembled_code"],
            "generated_code": result["generated_code"],
        }

    # ── refine_code ───────────────────────────────────────────────────
    async def refine_code(state: AgentState) -> dict:
        result = await coder.refine(state)
        return {
            "generated_code": result["generated_code"],
            "assembled_code": result.get("assembled_code", result["generated_code"]),
            "generated_codes": result.get("generated_codes", state.get("generated_codes", {})),
        }

    # ── answer_question ───────────────────────────────────────────────
    async def answer_question(state: AgentState) -> dict:
        result = await qa.run(state)
        return {
            "response": result["response"],
            "response_type": result.get("response_type", "text"),
        }

    # ── prepare_response ──────────────────────────────────────────────
    async def prepare_response(state: AgentState) -> dict:
        """Build the final user-facing response from assembled code + validation."""
        code = state.get("assembled_code") or state.get("generated_code", "")
        passed = state.get("validation_passed", False)
        errors = state.get("validation_errors", "")

        if code:
            lines: list[str] = []
            if passed:
                lines.append("Код сгенерирован и прошёл валидацию.\n")
            else:
                lines.append("Код сгенерирован, но содержит предупреждения/ошибки.\n")
                if errors:
                    lines.append(f"Проблемы:\n{errors}\n")
            lines.append(f"```lua\n{code}\n```")
            response = "\n".join(lines)
            response_type = "code"
        else:
            response = state.get("response", "Не удалось сгенерировать ответ.")
            response_type = state.get("response_type", "text")

        return {"response": response, "response_type": response_type}

    return {
        "route_intent": route_intent,
        "plan_task": plan_task,
        "generate_code": generate_code,
        "validate_code": validate_code,
        "fix_code": fix_code,
        "assemble_code": assemble_code,
        "refine_code": refine_code,
        "answer_question": answer_question,
        "prepare_response": prepare_response,
    }
