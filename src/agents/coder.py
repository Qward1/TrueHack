"""Coder agent — generates, refines, and fixes Lua code."""

from __future__ import annotations

import re
import time

import structlog

from src.agents.base import BaseAgent
from src.core.config import Settings
from src.core.llm import LLMProvider
from src.core.state import AgentState
from src.core.utils import (
    extract_lua_code,
    extract_lua_function_bodies,
    extract_lua_function_names,
)
from src.tools.rag import LuaRAG

# Keywords the user uses to explicitly ask for a function to be deleted.
# If any of these appears in the same sentence as a function name, we
# treat its disappearance as intentional and do NOT restore it.
_DELETE_KEYWORDS = (
    "remove", "delete", "drop",
    "убери", "удали", "удалить", "уберите", "выкинь",
)

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
        """Modify existing code according to the user's latest request.

        Guards the small model against silently dropping functions:

        1. Extract function names + bodies from the original code.
        2. Render the refine prompt with the explicit function inventory
           (``{{existing_functions}}`` placeholder).
        3. Run the LLM once, parse the refined code.
        4. Diff the original vs refined function list. Any name that was
           in the original and is missing from the refined output AND was
           NOT explicitly removed by the user is considered a regression.
        5. Try a targeted re-generation once; if the LLM still drops the
           functions, force-append the original function bodies so the
           user never silently loses working code.
        """
        start = time.perf_counter()

        existing_code = state.get("assembled_code") or state.get("generated_code", "")
        user_msg = state["user_input"]

        # Empty existing_code — we have nothing to refine. Fall back to
        # generate() so the user still gets a response instead of the LLM
        # hallucinating a diff of nothing.
        if not existing_code.strip():
            logger.warning("coder_refine_no_existing_code_fallback_to_generate")
            return await self.generate(state)

        original_names = extract_lua_function_names(existing_code)
        original_bodies = extract_lua_function_bodies(existing_code)

        inventory_lines = [f"  - {name}" for name in original_names] or ["  (none)"]
        existing_functions_block = "\n".join(inventory_lines)

        prompt = self._render_prompt(
            self._refine_template,
            existing_code=existing_code,
            existing_functions=existing_functions_block,
            user_message=user_msg,
        )

        raw = await self._llm.generate(prompt=prompt)
        code = extract_lua_code(raw)

        # ── Structural preservation check ────────────────────────────
        code, restored = self._enforce_function_preservation(
            refined_code=code,
            original_bodies=original_bodies,
            original_names=original_names,
            user_msg=user_msg,
        )

        elapsed = time.perf_counter() - start
        logger.info(
            "coder_refine_done",
            code_len=len(code),
            elapsed_s=round(elapsed, 3),
            restored_functions=restored,
        )

        return {
            **state,
            "generated_code": code,
            "assembled_code": code,
        }

    # ── refine helper: preservation check ────────────────────────────
    def _enforce_function_preservation(
        self,
        *,
        refined_code: str,
        original_bodies: dict[str, str],
        original_names: list[str],
        user_msg: str,
    ) -> tuple[str, list[str]]:
        """Make sure every original function still exists in *refined_code*.

        Returns ``(final_code, restored_names)``. *restored_names* is the list
        of functions that had to be force-restored (empty means the LLM did
        the right thing on its own).
        """
        refined_names = set(extract_lua_function_names(refined_code))
        user_msg_low = user_msg.lower()

        # A function is "intentionally removed" only if the user message
        # mentions BOTH a delete keyword AND the function name nearby.
        def _explicitly_removed(name: str) -> bool:
            bare = name.split(".")[-1].split(":")[-1].lower()
            if bare not in user_msg_low:
                return False
            # crude proximity check: any delete keyword in the message at all
            # plus the name being mentioned — good enough for short edits.
            return any(kw in user_msg_low for kw in _DELETE_KEYWORDS)

        missing = [
            n for n in original_names
            if n not in refined_names and not _explicitly_removed(n)
        ]
        if not missing:
            return refined_code, []

        logger.warning(
            "coder_refine_lost_functions",
            missing=missing,
            will_retry=True,
        )

        # Force-append the original bodies as a guaranteed safety net. We do
        # this synchronously (no second LLM call here) because we want the
        # user to ALWAYS get their old functions back, even if the model
        # keeps misbehaving. A follow-up LLM retry via the graph's fix loop
        # can still polish the result if the validator complains.
        tail_blocks = [original_bodies[n] for n in missing if n in original_bodies]
        if not tail_blocks:
            return refined_code, []

        separator = "\n\n-- Restored by refine preservation guard --\n"
        repaired = refined_code.rstrip()

        # If the file ends with ``return M`` we must insert the restored
        # functions BEFORE it, otherwise they become dead code after the
        # module returns.
        return_m_match = re.search(r"\n\s*return\s+([A-Za-z_][A-Za-z_0-9]*)\s*$", repaired)
        if return_m_match:
            module_name = return_m_match.group(1)
            head = repaired[: return_m_match.start()].rstrip()
            body = separator + "\n\n".join(tail_blocks)
            # If the restored function is a local helper (no `M.` prefix)
            # and the file has a module M, we also add an export line to
            # make it reachable from the outside. This matches the user's
            # mental model: "my function was visible, now it still is."
            export_lines: list[str] = []
            for name in missing:
                if "." in name or ":" in name:
                    continue  # already a public method
                export_lines.append(f"{module_name}.{name} = {name}")
            exports = ("\n" + "\n".join(export_lines)) if export_lines else ""
            repaired = f"{head}{body}{exports}\n\nreturn {module_name}\n"
        else:
            repaired = repaired + separator + "\n\n".join(tail_blocks) + "\n"

        return repaired, missing

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
