"""LangGraph node functions for the Lua code generation pipeline.

Each node receives the full PipelineState, performs one operation, and
returns a partial dict update. The LLM provider is injected once via
``create_nodes(llm)``.
"""

from __future__ import annotations

from typing import Any, Callable

import structlog

from src.core.llm import LLMProvider
from src.core.state import PipelineState
from src.tools.lua_tools import (
    async_run_diagnostics,
    async_verify_requirements,
    extract_function_names,
    restore_lost_functions,
    smart_normalize,
    validate_lua_response,
)

logger = structlog.get_logger(__name__)

# ── Prompts ──────────────────────────────────────────────────────────────

_ROUTE_SYSTEM = (
    "You are an intent classifier for a Lua code assistant. "
    "Classify the user message into exactly one category. "
    "Return JSON only: {\"intent\": \"<category>\", \"confidence\": <float>}"
)

_ROUTE_USER = """Categories:
- create: user wants NEW Lua code (clear task description)
- change: user wants to MODIFY / IMPROVE / FIX existing code. Signals: "add", "change",
  "rename", "fix", "improve", "поправки", "правки", "доработай", "добавь", "измени", "исправь",
  "теперь сделай", "а ещё", a numbered list of fixes/requests.
- inspect: user asks to explain or review existing code
- question: user asks about Lua / programming (no code request)
- general: casual conversation, greetings, thanks

Decision rules:
1. If previous code exists AND the message contains change signals → change
2. If message includes error text / "it crashes with..." → change (as a fix request)
3. If message asks for NEW code unrelated to previous → create
4. Otherwise → question or general

Previous code exists: {has_code}
User message: {user_input}

JSON only:"""

_GENERATE_SYSTEM = (
    "You generate clean, correct Lua 5.4 code from the user's request. "
    "Return ONLY Lua code without markdown fences or explanations. "
    "If the program is a Windows console app, prefer ASCII-only UI text."
)

_REFINE_SYSTEM = (
    "You modify existing Lua code according to the user's request. "
    "Return the COMPLETE updated file — not just the changed parts. "
    "PRESERVE all existing functions unless explicitly asked to remove them. "
    "Return only Lua code, no markdown fences, no explanations."
)

_REFINE_USER = """EXISTING FUNCTIONS you MUST preserve (unless user says remove):
{function_list}

ORIGINAL CODE:
{code}

USER REQUEST:
{user_input}

Return the complete updated Lua file. No fences, no prose."""

_FIX_SYSTEM = (
    "You fix broken Lua code using the user's goal and diagnostics. "
    "Return only corrected Lua code without markdown fences, explanations, or extra text. "
    "Do not remove interactivity just to pass checks."
)

_FIX_USER = """Original task: {base_prompt}

Failure kind: {failure_kind}
Runtime error: {run_error}
Luacheck error: {luacheck_error}
Runtime output: {run_output}
Luacheck output: {luacheck_output}

Current code:
{code}

Fix the code. Return only the full corrected Lua file."""

_ANSWER_SYSTEM = (
    "You are a helpful Lua programming assistant. "
    "Answer in the same language as the user's message."
)


def create_nodes(llm: LLMProvider) -> dict[str, Callable]:
    """Build node callables from a pre-constructed LLM provider."""

    # ── route_intent ─────────────────────────────────────────────────
    async def route_intent(state: PipelineState) -> dict:
        has_code = bool(state.get("current_code", "").strip())
        prompt = _ROUTE_USER.format(
            has_code=str(has_code).lower(),
            user_input=state["user_input"],
        )
        result = await llm.generate_json(prompt, system=_ROUTE_SYSTEM)
        intent = result.get("intent", "create")
        confidence = float(result.get("confidence", 0.5))

        # Safety: if code exists and model is uncertain, prefer "change"
        if has_code and confidence < 0.5:
            intent = "change"

        # Normalize
        valid_intents = {"create", "change", "inspect", "question", "general", "retry"}
        if intent not in valid_intents:
            intent = "change" if has_code else "create"

        logger.info("route_intent", intent=intent, confidence=confidence)
        return {"intent": intent}

    # ── generate_code ────────────────────────────────────────────────
    async def generate_code(state: PipelineState) -> dict:
        user_input = state["user_input"]
        base_prompt = state.get("base_prompt", "") or user_input

        raw = await llm.generate(prompt=user_input, system=_GENERATE_SYSTEM)

        # Smart normalize: strip fences, preamble, zero-width chars
        analysis = validate_lua_response(raw)
        code = analysis["normalized"]

        # If response isn't valid Lua, retry with stricter prompt
        if not analysis["valid"]:
            logger.warning("generate_not_lua_retrying", reason=analysis["reason"])
            strict_prompt = (
                f"{user_input}\n\n"
                f"Previous response issue: {analysis['reason']}\n"
                "Return ONLY the full Lua file."
            )
            strict_system = (
                f"{_GENERATE_SYSTEM} "
                "The first non-whitespace character must be valid Lua code."
            )
            raw2 = await llm.generate(
                prompt=strict_prompt,
                system=strict_system,
                temperature=0.05,
            )
            code = smart_normalize(raw2)

        if not code:
            code = smart_normalize(raw)

        logger.info("generate_code_done", code_len=len(code))
        return {
            "generated_code": code,
            "base_prompt": base_prompt,
            "fix_iterations": 0,
        }

    # ── refine_code ──────────────────────────────────────────────────
    async def refine_code(state: PipelineState) -> dict:
        existing = state.get("current_code", "")
        user_input = state["user_input"]

        if not existing.strip():
            # Nothing to refine → treat as generate
            logger.warning("refine_no_existing_code_fallback_generate")
            return await generate_code(state)

        # Build function inventory for the model
        func_names = extract_function_names(existing)
        func_list = "\n".join(f"  - {n}" for n in func_names) or "  (none)"

        prompt = _REFINE_USER.format(
            function_list=func_list,
            code=existing,
            user_input=user_input,
        )
        raw = await llm.generate(prompt=prompt, system=_REFINE_SYSTEM)
        code = smart_normalize(raw)

        if not code:
            code = existing  # fallback: don't lose working code

        # Preservation guard: restore silently dropped functions
        code, restored = restore_lost_functions(existing, code, user_input)
        if restored:
            logger.info("refine_restored_functions", restored=restored)

        # Update change_requests
        changes = list(state.get("change_requests") or [])
        changes.append(user_input)

        logger.info("refine_code_done", code_len=len(code))
        return {
            "generated_code": code,
            "change_requests": changes,
            "fix_iterations": 0,
        }

    # ── validate_code ────────────────────────────────────────────────
    async def validate_code(state: PipelineState) -> dict:
        code = state.get("generated_code", "")
        if not code.strip():
            return {
                "validation_passed": False,
                "diagnostics": {"success": False, "run_error": "Empty code"},
            }

        diagnostics = await async_run_diagnostics(code)
        passed = diagnostics.get("success", False)

        logger.info(
            "validate_code_done",
            passed=passed,
            failure_kind=diagnostics.get("failure_kind", ""),
        )
        return {
            "validation_passed": passed,
            "diagnostics": diagnostics,
        }

    # ── fix_code ─────────────────────────────────────────────────────
    async def fix_code(state: PipelineState) -> dict:
        code = state.get("generated_code", "")
        diagnostics = state.get("diagnostics", {})
        base_prompt = state.get("base_prompt", "") or state.get("user_input", "")
        fix_iter = state.get("fix_iterations", 0)

        prompt = _FIX_USER.format(
            base_prompt=base_prompt,
            failure_kind=diagnostics.get("failure_kind", "unknown"),
            run_error=diagnostics.get("run_error", "none"),
            luacheck_error=diagnostics.get("luacheck_error", "none"),
            run_output=diagnostics.get("run_output", "none"),
            luacheck_output=diagnostics.get("luacheck_output", "none"),
            code=code,
        )

        # Use chat format: system + original task + broken code + diagnostics
        messages = [
            {"role": "system", "content": _FIX_SYSTEM},
            {"role": "user", "content": f"Original task:\n{base_prompt}"},
            {"role": "assistant", "content": code},
            {"role": "user", "content": prompt},
        ]
        raw = await llm.chat(messages, temperature=0.05)
        fixed = smart_normalize(raw)

        if not fixed:
            fixed = code  # don't lose code on empty response

        logger.info("fix_code_done", iteration=fix_iter + 1, code_len=len(fixed))
        return {
            "generated_code": fixed,
            "fix_iterations": fix_iter + 1,
        }

    # ── verify_requirements ──────────────────────────────────────────
    async def verify_requirements(state: PipelineState) -> dict:
        code = state.get("generated_code", "")
        base_prompt = state.get("base_prompt", "") or state.get("user_input", "")
        diagnostics = state.get("diagnostics", {})

        try:
            verification = await async_verify_requirements(
                prompt=base_prompt,
                code=code,
                run_output=diagnostics.get("run_output", ""),
                luacheck_output=diagnostics.get("luacheck_output", ""),
            )
        except Exception as exc:
            logger.warning("verify_failed", error=str(exc))
            verification = {"passed": True, "score": 0, "summary": f"Verification error: {exc}"}

        passed = verification.get("passed", False)
        score = verification.get("score", 0)

        logger.info("verify_done", passed=passed, score=score)
        return {
            "verification": verification,
            "verification_passed": passed or score >= 70,
        }

    # ── answer_question ──────────────────────────────────────────────
    async def answer_question(state: PipelineState) -> dict:
        user_input = state["user_input"]
        existing = state.get("current_code", "")

        prompt = user_input
        if existing.strip():
            prompt = f"Current code:\n```lua\n{existing}\n```\n\n{user_input}"

        answer = await llm.generate(prompt=prompt, system=_ANSWER_SYSTEM)
        return {
            "response": answer,
            "response_type": "text",
        }

    # ── prepare_response ─────────────────────────────────────────────
    async def prepare_response(state: PipelineState) -> dict:
        code = state.get("generated_code", "")
        passed = state.get("validation_passed", False)
        diagnostics = state.get("diagnostics", {})
        verification = state.get("verification", {})

        if not code.strip():
            return {
                "response": state.get("response", "Не удалось сгенерировать код."),
                "response_type": state.get("response_type", "error"),
            }

        lines: list[str] = []
        if passed:
            v_score = verification.get("score", 0)
            v_passed = verification.get("passed", False)
            if v_passed or v_score >= 70:
                lines.append("Код сгенерирован и прошёл валидацию.\n")
            else:
                lines.append("Код сгенерирован. Валидация пройдена, но проверка требований частичная.\n")
        else:
            lines.append("Код сгенерирован, но содержит предупреждения/ошибки.\n")
            run_err = diagnostics.get("run_error", "")
            lc_err = diagnostics.get("luacheck_error", "")
            if run_err:
                lines.append(f"Ошибка выполнения: {run_err}\n")
            if lc_err:
                lines.append(f"Luacheck: {lc_err}\n")

        lines.append(f"```lua\n{code}\n```")

        # Add run output if interesting
        run_output = diagnostics.get("run_output", "").strip()
        if run_output and passed:
            lines.append(f"\nВывод программы:\n```\n{run_output}\n```")

        return {
            "response": "\n".join(lines),
            "response_type": "code",
            "current_code": code,  # persist for next turn
        }

    return {
        "route_intent": route_intent,
        "generate_code": generate_code,
        "refine_code": refine_code,
        "validate_code": validate_code,
        "fix_code": fix_code,
        "verify_requirements": verify_requirements,
        "answer_question": answer_question,
        "prepare_response": prepare_response,
    }
