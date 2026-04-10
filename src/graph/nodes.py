"""LangGraph node functions for the canonical Lua generation pipeline."""

from __future__ import annotations

import os
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
from src.tools.target_tools import (
    load_target_code,
    resolve_lua_target,
    save_final_output,
)

logger = structlog.get_logger(__name__)

_ROUTE_SYSTEM = (
    "You are an intent classifier for a Lua code assistant. "
    "Classify the user message into exactly one category. "
    'Return JSON only: {"intent": "<category>", "confidence": <float>}.'
)

_ROUTE_USER = """Categories:
- create: user wants NEW Lua code
- change: user wants to MODIFY / IMPROVE / FIX existing code
- inspect: user asks to explain or review existing code
- question: user asks about Lua / programming without requesting code changes
- general: greeting or casual text

Decision rules:
1. If previous code exists AND the message contains change signals -> change
2. If the message includes error text or asks to fix/apply improvements -> change
3. If the message asks for new code unrelated to previous code -> create
4. Otherwise -> question or general

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
    "Return the COMPLETE updated file, not just the changed parts. "
    "Preserve existing functions unless explicitly asked to remove them. "
    "Return only Lua code."
)

_REFINE_USER = """Primary target file: {target_path}
Existing functions you must preserve unless the user explicitly removes them:
{function_list}

Original code:
{code}

User request:
{user_input}

Return the complete updated Lua file. No fences. No prose."""

_FIX_SYSTEM = (
    "You fix broken Lua code using the user's goal and diagnostics. "
    "Return only corrected Lua code without markdown fences, explanations, or extra text. "
    "Do not remove legitimate interactivity just to pass checks."
)

_FIX_USER = """Primary target file: {target_path}
Original task: {base_prompt}
Current failure stage: {failure_stage}

Validation diagnostics:
- Failure kind: {failure_kind}
- Runtime error: {run_error}
- Runtime output: {run_output}

Requirement verification:
- Summary: {verification_summary}
- Missing requirements: {missing_requirements}

Current code:
{code}

Fix the code so it satisfies the original task and passes validation + requirement checks.
Return only the full corrected Lua file."""

_ANSWER_SYSTEM = (
    "You are a helpful Lua programming assistant. "
    "Answer in the same language as the user's message."
)

_EXPLAIN_SYSTEM = (
    "You explain generated Lua code for the user. "
    "Return strict JSON only with keys: summary, what_is_in_code, how_it_works, "
    "suggested_changes, clarifying_questions. "
    "Keep suggested_changes and clarifying_questions short lists (1-3 items each)."
)


def _target_context(state: PipelineState) -> str:
    target_path = state.get("target_path", "")
    if not target_path:
        return ""
    try:
        relative = os.path.relpath(target_path, state.get("workspace_root", "") or os.getcwd())
    except ValueError:
        relative = target_path
    return f"Primary target Lua file: {relative}"


def _normalize_string_list(value: object, limit: int = 3) -> list[str]:
    if not isinstance(value, list):
        return []
    normalized = [str(item).strip() for item in value if str(item).strip()]
    return normalized[:limit]


def create_nodes(llm: LLMProvider) -> dict[str, Callable]:
    """Build node callables from a pre-constructed LLM provider."""

    async def resolve_target(state: PipelineState) -> dict:
        previous_target = os.path.abspath(state.get("target_path", "")) if state.get("target_path") else ""
        resolved = resolve_lua_target(
            state["user_input"],
            workspace_root=state.get("workspace_root", ""),
            current_target_path=previous_target,
            allow_fallback=False,
        )
        target_path = resolved["target_path"]
        current_code = state.get("current_code", "")

        if target_path:
            same_target = bool(previous_target and previous_target == os.path.abspath(target_path))
            if not same_target or not current_code.strip():
                current_code = load_target_code(target_path)
        elif not previous_target:
            current_code = ""

        logger.info(
            "resolve_target",
            target_path=target_path,
            explicit=resolved["target_explicit"],
        )
        return {
            "workspace_root": resolved["workspace_root"],
            "target_path": target_path,
            "target_directory": resolved["target_directory"],
            "target_explicit": resolved["target_explicit"],
            "current_code": current_code,
        }

    async def route_intent(state: PipelineState) -> dict:
        has_code = bool(state.get("current_code", "").strip())
        prompt = _ROUTE_USER.format(
            has_code=str(has_code).lower(),
            user_input=state["user_input"],
        )
        result = await llm.generate_json(prompt, system=_ROUTE_SYSTEM)
        intent = result.get("intent", "create")
        confidence = float(result.get("confidence", 0.5))

        if has_code and confidence < 0.5:
            intent = "change"

        valid_intents = {"create", "change", "inspect", "question", "general", "retry"}
        if intent not in valid_intents:
            intent = "change" if has_code else "create"

        logger.info("route_intent", intent=intent, confidence=confidence)
        return {"intent": intent}

    async def generate_code(state: PipelineState) -> dict:
        user_input = state["user_input"]
        base_prompt = state.get("base_prompt", "") or user_input
        target_path = state.get("target_path", "")
        target_directory = state.get("target_directory", state.get("workspace_root", ""))
        target_explicit = state.get("target_explicit", False)

        if not target_path:
            fallback = resolve_lua_target(
                user_input,
                workspace_root=state.get("workspace_root", ""),
                allow_fallback=True,
            )
            target_path = fallback["target_path"]
            target_directory = fallback["target_directory"]
            target_explicit = fallback["target_explicit"]

        target_context = _target_context(
            {
                **state,
                "target_path": target_path,
                "target_directory": target_directory,
                "target_explicit": target_explicit,
            }
        )
        prompt = user_input
        if target_context:
            prompt = f"{user_input}\n\n{target_context}\nGenerate only the code for this Lua file."

        raw = await llm.generate(prompt=prompt, system=_GENERATE_SYSTEM)
        analysis = validate_lua_response(raw)
        code = analysis["normalized"]

        if not analysis["valid"]:
            logger.warning("generate_not_lua_retrying", reason=analysis["reason"])
            strict_prompt = (
                f"{prompt}\n\n"
                f"Previous response issue: {analysis['reason']}\n"
                "Return ONLY the full Lua file."
            )
            strict_system = (
                f"{_GENERATE_SYSTEM} "
                "The first non-whitespace character must be valid Lua code."
            )
            raw_retry = await llm.generate(
                prompt=strict_prompt,
                system=strict_system,
                temperature=0.05,
            )
            code = smart_normalize(raw_retry)

        if not code:
            code = smart_normalize(raw)

        logger.info("generate_code_done", code_len=len(code), target_path=target_path)
        return {
            "generated_code": code,
            "base_prompt": base_prompt,
            "fix_iterations": 0,
            "target_path": target_path,
            "target_directory": target_directory,
            "target_explicit": target_explicit,
            "failure_stage": "",
            "verification": {},
            "verification_passed": False,
            "e2e_suite": {},
            "e2e_results": {"summary": "E2E-проверка временно отключена."},
            "e2e_passed": False,
            "save_success": False,
            "save_error": "",
            "saved_to": "",
            "explanation": {},
            "suggested_changes": [],
            "clarifying_questions": [],
        }

    async def refine_code(state: PipelineState) -> dict:
        existing = state.get("current_code", "")
        user_input = state["user_input"]

        if not existing.strip():
            logger.warning("refine_no_existing_code_fallback_generate")
            return await generate_code(state)

        func_names = extract_function_names(existing)
        func_list = "\n".join(f"  - {name}" for name in func_names) or "  (none)"
        target_path = state.get("target_path", "(not set)")
        prompt = _REFINE_USER.format(
            target_path=target_path,
            function_list=func_list,
            code=existing,
            user_input=user_input,
        )
        raw = await llm.generate(prompt=prompt, system=_REFINE_SYSTEM)
        code = smart_normalize(raw)
        if not code:
            code = existing

        code, restored = restore_lost_functions(existing, code, user_input)
        if restored:
            logger.info("refine_restored_functions", restored=restored)

        changes = list(state.get("change_requests") or [])
        changes.append(user_input)

        logger.info("refine_code_done", code_len=len(code), target_path=target_path)
        return {
            "generated_code": code,
            "change_requests": changes,
            "failure_stage": "",
            "verification": {},
            "verification_passed": False,
            "e2e_suite": {},
            "e2e_results": {"summary": "E2E-проверка временно отключена."},
            "e2e_passed": False,
            "save_success": False,
            "save_error": "",
            "saved_to": "",
            "explanation": {},
            "suggested_changes": [],
            "clarifying_questions": [],
        }

    async def validate_code(state: PipelineState) -> dict:
        code = state.get("generated_code", "")
        if not code.strip():
            diagnostics = {
                "success": False,
                "failure_kind": "empty",
                "run_error": "Empty code",
                "run_output": "",
            }
            return {
                "validation_passed": False,
                "failure_stage": "validation",
                "diagnostics": diagnostics,
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
            "failure_stage": "" if passed else "validation",
            "diagnostics": diagnostics,
        }

    async def fix_code(state: PipelineState) -> dict:
        code = state.get("generated_code", "")
        diagnostics = state.get("diagnostics", {})
        verification = state.get("verification", {})
        base_prompt = state.get("base_prompt", "") or state.get("user_input", "")
        fix_iter = state.get("fix_iterations", 0)
        prompt = _FIX_USER.format(
            target_path=state.get("target_path", "(not set)"),
            base_prompt=base_prompt,
            failure_stage=state.get("failure_stage", "unknown"),
            failure_kind=diagnostics.get("failure_kind", "unknown"),
            run_error=diagnostics.get("run_error", "none"),
            run_output=diagnostics.get("run_output", "none"),
            verification_summary=verification.get("summary", "none"),
            missing_requirements=", ".join(verification.get("missing_requirements", [])) or "none",
            code=code,
        )

        messages = [
            {"role": "system", "content": _FIX_SYSTEM},
            {"role": "user", "content": f"Original task:\n{base_prompt}"},
            {"role": "assistant", "content": code},
            {"role": "user", "content": prompt},
        ]
        raw = await llm.chat(messages, temperature=0.05)
        fixed = smart_normalize(raw)
        if not fixed:
            fixed = code

        logger.info("fix_code_done", iteration=fix_iter + 1, code_len=len(fixed))
        return {
            "generated_code": fixed,
            "fix_iterations": fix_iter + 1,
            "failure_stage": "",
            "validation_passed": False,
            "verification_passed": False,
            "e2e_passed": False,
            "e2e_results": {"summary": "E2E-проверка временно отключена."},
            "save_success": False,
            "save_error": "",
            "saved_to": "",
            "explanation": {},
            "suggested_changes": [],
            "clarifying_questions": [],
        }

    async def verify_requirements(state: PipelineState) -> dict:
        code = state.get("generated_code", "")
        base_prompt = state.get("base_prompt", "") or state.get("user_input", "")
        diagnostics = state.get("diagnostics", {})

        try:
            verification = await async_verify_requirements(
                llm,
                prompt=base_prompt,
                code=code,
                run_output=diagnostics.get("run_output", ""),
            )
        except Exception as exc:
            logger.warning("verify_failed", error=str(exc))
            verification = {
                "passed": False,
                "score": 0,
                "summary": f"LLM verification unavailable: {exc}",
                "missing_requirements": [],
                "warnings": ["verification_unavailable"],
                "error": True,
            }

        passed = bool(verification.get("passed")) or int(verification.get("score", 0) or 0) >= 70
        diagnostics_updated = dict(diagnostics)
        diagnostics_updated["verification_summary"] = verification.get("summary", "")
        diagnostics_updated["verification_checked"] = not verification.get("error", False)
        diagnostics_updated["verification_passed"] = passed

        logger.info("verify_done", passed=passed, score=verification.get("score", 0))
        return {
            "verification": verification,
            "verification_passed": passed,
            "failure_stage": "" if (passed or verification.get("error")) else "requirements",
            "diagnostics": diagnostics_updated,
        }

    async def save_code(state: PipelineState) -> dict:
        code = state.get("generated_code", "")
        target_path = state.get("target_path", "")
        if not code.strip():
            return {"save_success": False, "save_error": "Empty code cannot be saved.", "saved_to": ""}
        if not target_path:
            return {"save_success": False, "save_error": "Target path is not set.", "saved_to": ""}

        try:
            save_final_output(target_path, code)
        except OSError as exc:
            logger.error("save_code_failed", target_path=target_path, error=str(exc))
            return {
                "current_code": code,
                "save_success": False,
                "save_error": str(exc),
                "saved_to": "",
            }

        logger.info("save_code_done", target_path=target_path)
        return {
            "current_code": code,
            "save_success": True,
            "save_error": "",
            "saved_to": target_path,
        }

    async def explain_solution(state: PipelineState) -> dict:
        code = state.get("generated_code", "").strip()
        if not code:
            return {"explanation": {}, "suggested_changes": [], "clarifying_questions": []}

        diagnostics = state.get("diagnostics", {})
        verification = state.get("verification", {})
        base_prompt = state.get("base_prompt", "") or state.get("user_input", "")

        explain_prompt = (
            f"User request:\n{base_prompt}\n\n"
            f"Runtime validation summary:\n"
            f"- run_error: {diagnostics.get('run_error', 'none')}\n"
            f"- verification_summary: {verification.get('summary', 'none')}\n\n"
            "Code:\n"
            f"{code}\n\n"
            "Respond with JSON only."
        )

        try:
            raw = await llm.generate_json(explain_prompt, system=_EXPLAIN_SYSTEM, temperature=0.1)
        except Exception as exc:
            logger.warning("explain_failed", error=str(exc))
            raw = {}

        explanation = {
            "summary": str(raw.get("summary", "")).strip(),
            "what_is_in_code": _normalize_string_list(raw.get("what_is_in_code"), limit=5),
            "how_it_works": _normalize_string_list(raw.get("how_it_works"), limit=5),
        }
        suggested_changes = _normalize_string_list(raw.get("suggested_changes"), limit=3)
        clarifying_questions = _normalize_string_list(raw.get("clarifying_questions"), limit=3)

        if not explanation["summary"]:
            explanation["summary"] = "Код сохранен и готов к следующей итерации."
        if not explanation["what_is_in_code"]:
            explanation["what_is_in_code"] = ["Основная логика задачи реализована в одном Lua-файле."]
        if not explanation["how_it_works"]:
            explanation["how_it_works"] = ["Логика выполняется последовательно через функции и условия Lua."]

        return {
            "explanation": explanation,
            "suggested_changes": suggested_changes,
            "clarifying_questions": clarifying_questions,
        }

    async def answer_question(state: PipelineState) -> dict:
        user_input = state["user_input"]
        existing = state.get("current_code", "")
        prompt = user_input
        if existing.strip():
            context = _target_context(state)
            if context:
                prompt = f"{context}\n\nCurrent code:\n```lua\n{existing}\n```\n\n{user_input}"
            else:
                prompt = f"Current code:\n```lua\n{existing}\n```\n\n{user_input}"

        answer = await llm.generate(prompt=prompt, system=_ANSWER_SYSTEM)
        return {
            "response": answer,
            "response_type": "text",
        }

    async def prepare_response(state: PipelineState) -> dict:
        code = state.get("generated_code", "")
        diagnostics = state.get("diagnostics", {})
        verification = state.get("verification", {})
        saved_to = state.get("saved_to", "")
        save_error = state.get("save_error", "")
        explanation = state.get("explanation", {})
        suggested_changes = state.get("suggested_changes", [])
        clarifying_questions = state.get("clarifying_questions", [])
        failure_stage = state.get("failure_stage", "")

        if not code.strip():
            return {
                "response": state.get("response", "Не удалось сгенерировать код."),
                "response_type": state.get("response_type", "error"),
            }

        lines: list[str] = []
        if state.get("save_success", False):
            lines.append("Код сгенерирован, прошел проверки и сохранен.\n")
        else:
            lines.append("Код подготовлен, но финальные условия сохранения не выполнены.\n")
            if failure_stage:
                lines.append(f"Этап с проблемой: {failure_stage}\n")

        if diagnostics.get("run_error"):
            lines.append(f"Runtime error: {diagnostics.get('run_error')}\n")

        verification_summary = str(verification.get("summary", "")).strip()
        if verification_summary:
            lines.append(f"Проверка требований: {verification_summary}\n")

        if saved_to:
            lines.append(f"Сохранено в: `{saved_to}`\n")
        elif save_error:
            lines.append(f"Сохранение не удалось: {save_error}\n")

        lines.append(f"```lua\n{code}\n```")

        run_output = diagnostics.get("run_output", "").strip()
        if run_output:
            lines.append(f"\nВывод программы:\n```\n{run_output}\n```")

        if isinstance(explanation, dict):
            summary = str(explanation.get("summary", "")).strip()
            what_is_in_code = explanation.get("what_is_in_code", [])
            how_it_works = explanation.get("how_it_works", [])
            if summary:
                lines.append(f"\nЧто сделано: {summary}")
            if isinstance(what_is_in_code, list) and what_is_in_code:
                lines.append("\nЧто есть в коде:")
                for item in what_is_in_code[:5]:
                    item_text = str(item).strip()
                    if item_text:
                        lines.append(f"- {item_text}")
            if isinstance(how_it_works, list) and how_it_works:
                lines.append("\nКак это работает:")
                for item in how_it_works[:5]:
                    item_text = str(item).strip()
                    if item_text:
                        lines.append(f"- {item_text}")

        if isinstance(suggested_changes, list) and suggested_changes:
            lines.append("\nЧто можно улучшить:")
            for index, suggestion in enumerate(suggested_changes[:3], 1):
                suggestion_text = str(suggestion).strip()
                if suggestion_text:
                    lines.append(f"{index}. {suggestion_text}")

        if isinstance(clarifying_questions, list) and clarifying_questions:
            lines.append("\nУточняющие вопросы:")
            for index, question in enumerate(clarifying_questions[:3], 1):
                question_text = str(question).strip()
                if question_text:
                    lines.append(f"{index}. {question_text}")

        return {
            "response": "\n".join(lines),
            "response_type": "code",
            "current_code": code,
        }

    return {
        "resolve_target": resolve_target,
        "route_intent": route_intent,
        "generate_code": generate_code,
        "refine_code": refine_code,
        "validate_code": validate_code,
        "fix_code": fix_code,
        "verify_requirements": verify_requirements,
        "save_code": save_code,
        "explain_solution": explain_solution,
        "answer_question": answer_question,
        "prepare_response": prepare_response,
    }
