"""Planner agent — decomposes requests, asks clarifications, assembles code."""

from __future__ import annotations

import time

import structlog

from src.agents.base import BaseAgent
from src.core.config import Settings
from src.core.llm import LLMProvider
from src.core.state import AgentState

logger = structlog.get_logger(__name__)

_PLAN_SCHEMA = {
    "tasks": [
        {
            "id": "string",
            "description": "string",
            "function_name": "string",
            "signature": "string",
            "dependencies": ["string"],
        }
    ]
}

_PLAN_FALLBACK = {
    "tasks": [
        {
            "id": "task_1",
            "description": "user request",
            "function_name": "solution",
            "signature": "script",
            "dependencies": [],
        }
    ]
}

_CLARIFY_SCHEMA = {"questions": ["string"]}
_CLARIFY_FALLBACK = {"questions": ["Уточните что именно должен делать код?"]}

_MODULE_SEPARATOR = "\n\n"

# Planners that over-decompose tend to append a "main"/"demo"/"usage" task which
# the coder then fills with garbage calls to undefined helpers. When the plan
# has more than one task we drop any task matching these names.
_GARBAGE_TASK_NAMES = {
    "main",
    "entry",
    "entry_point",
    "entrypoint",
    "run",
    "runner",
    "demo",
    "example",
    "examples",
    "usage",
    "show_usage",
    "test",
    "tests",
    "driver",
    "exec",
    "execute",
}


class PlannerAgent(BaseAgent):
    """Three modes: plan / clarify / assemble (no LLM for assemble)."""

    def __init__(self, llm: LLMProvider, settings: Settings) -> None:
        super().__init__(llm, settings)
        self._plan_template = self._load_prompt("planner_plan")
        self._clarify_template = self._load_prompt("planner_clarify")

    # ── plan ─────────────────────────────────────────────────────────
    async def plan(self, state: AgentState) -> AgentState:
        """Call LLM to decompose the request into structured tasks."""
        start = time.perf_counter()

        prompt = self._render_prompt(
            self._plan_template,
            user_message=state["user_input"],
        )
        result = await self._llm.generate_structured(
            prompt=prompt,
            system="You are a Lua code architect. Respond with JSON only.",
            schema=_PLAN_SCHEMA,
            fallback=_PLAN_FALLBACK,
        )

        tasks: list[dict] = result.get("tasks", _PLAN_FALLBACK["tasks"])
        # Guard: ensure every task has required keys
        for i, task in enumerate(tasks):
            task.setdefault("id", f"task_{i + 1}")
            task.setdefault("description", state["user_input"])
            task.setdefault("function_name", "solution")
            task.setdefault("signature", "script")
            task.setdefault("dependencies", [])

        # Filter garbage "main"/"demo"/"usage" tasks that the planner tends to
        # append. Only strip them when there are multiple tasks — a standalone
        # "main" task means the user genuinely asked for a main script.
        if len(tasks) > 1:
            filtered = [
                t for t in tasks
                if str(t.get("function_name", "")).strip().lower() not in _GARBAGE_TASK_NAMES
                and str(t.get("id", "")).strip().lower() not in _GARBAGE_TASK_NAMES
            ]
            dropped = len(tasks) - len(filtered)
            if filtered:
                tasks = filtered
                if dropped:
                    logger.info("planner_dropped_garbage_tasks", dropped=dropped)

        elapsed = time.perf_counter() - start
        logger.info("planner_plan_done", n_tasks=len(tasks), elapsed_s=round(elapsed, 3))

        return {
            **state,
            "plan": tasks,
            "current_task_index": 0,
            "generated_codes": {},
        }

    # ── clarify ───────────────────────────────────────────────────────
    async def clarify(self, state: AgentState) -> AgentState:
        """Ask the user clarifying questions when the request is too vague."""
        start = time.perf_counter()

        prompt = self._render_prompt(
            self._clarify_template,
            user_message=state["user_input"],
        )
        result = await self._llm.generate_structured(
            prompt=prompt,
            system="You are a helpful assistant. Respond with JSON only.",
            schema=_CLARIFY_SCHEMA,
            fallback=_CLARIFY_FALLBACK,
        )

        questions: list[str] = result.get("questions", _CLARIFY_FALLBACK["questions"])
        response_lines = ["Чтобы написать код, мне нужно уточнить несколько моментов:\n"]
        for i, q in enumerate(questions, 1):
            response_lines.append(f"{i}. {q}")
        response = "\n".join(response_lines)

        elapsed = time.perf_counter() - start
        logger.info(
            "planner_clarify_done",
            n_questions=len(questions),
            elapsed_s=round(elapsed, 3),
        )

        return {
            **state,
            "clarification_questions": questions,
            "response": response,
            "response_type": "clarification",
        }

    # ── assemble ──────────────────────────────────────────────────────
    def assemble(self, state: AgentState) -> AgentState:
        """Concatenate all per-task codes into a single Lua module (no LLM)."""
        plan: list[dict] = state.get("plan") or []
        codes: dict[str, str] = state.get("generated_codes") or {}

        # Collect codes in plan order, fall back to generated_code for single tasks
        ordered: list[str] = []
        for task in plan:
            code = codes.get(task["id"], "")
            if code.strip():
                ordered.append(code.strip())

        if not ordered:
            # Nothing in generated_codes — use the latest generated_code
            assembled = state.get("generated_code", "")
        else:
            # Deduplicate individual functions across blocks.
            # Strategy: scan blocks in order; for each function definition keep
            # the LAST block that contains it (later tasks override earlier ones).
            # We rebuild by stripping duplicate function bodies from earlier blocks.
            import re
            _FUNC_RE = re.compile(
                r"(local\s+function\s+\w+\s*\([^)]*\)[\s\S]*?end"
                r"|function\s+\w+\s*\([^)]*\)[\s\S]*?end)",
                re.MULTILINE,
            )
            _NAME_RE = re.compile(r"(?:local\s+)?function\s+(\w+)")

            # Find which block last defines each function name
            last_block_for: dict[str, int] = {}
            for idx, block in enumerate(ordered):
                for name in _NAME_RE.findall(block):
                    last_block_for[name] = idx

            # Rebuild: remove function defs from earlier blocks when a later
            # block redefines the same name
            cleaned: list[str] = []
            for idx, block in enumerate(ordered):
                def remove_if_redefined(m: re.Match) -> str:
                    fname = _NAME_RE.search(m.group(0))
                    if fname and last_block_for.get(fname.group(1), idx) != idx:
                        return ""
                    return m.group(0)

                new_block = _FUNC_RE.sub(remove_if_redefined, block).strip()
                if new_block:
                    cleaned.append(new_block)

            assembled = _MODULE_SEPARATOR.join(cleaned)

        logger.info("planner_assemble_done", n_parts=len(ordered))

        return {
            **state,
            "assembled_code": assembled,
            "generated_code": assembled,
        }

    # ── run (dispatcher) ──────────────────────────────────────────────
    async def run(self, state: AgentState) -> AgentState:
        """Dispatch to plan / clarify / assemble based on state."""
        intent = state.get("intent", "")
        plan = state.get("plan") or []
        generated_codes = state.get("generated_codes") or {}

        if intent == "generate_unclear":
            return await self.clarify(state)

        # If all tasks are coded → assemble
        if plan and len(generated_codes) >= len(plan):
            return self.assemble(state)

        # Default: plan the work
        return await self.plan(state)
