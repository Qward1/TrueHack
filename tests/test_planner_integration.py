"""Integration tests for the TaskPlanner node wired into the canonical pipeline."""

from __future__ import annotations

import asyncio
import json
import os
import unittest
from pathlib import Path
from unittest.mock import patch

from src.graph.engine import PipelineEngine
from src.graph.nodes import _build_generation_prompt, _format_planner_section


# System prompt prefixes used by various agents in the pipeline.
PLANNER_SYSTEM_PREFIX = "You are a task analyst for a LowCode Lua 5.5 workflow script generator."
ROUTE_SYSTEM_PREFIX = "You are an intent classifier"
EXPLAIN_SYSTEM_PREFIX = "You explain generated Lua code"
VERIFY_SYSTEM_PREFIX = "You are a strict verifier for LowCode Lua 5.5 workflow solutions."
FIX_VALIDATION_SYSTEM_PREFIX = "You fix Lua 5.5 workflow scripts that fail during execution."
FIX_VERIFICATION_SYSTEM_PREFIX = "You fix Lua 5.5 workflow scripts that fail requirement verification."


def _success_diagnostics() -> dict:
    return {
        "success": True,
        "started_ok": True,
        "timed_out": False,
        "program_mode": "workflow",
        "validation_context": "test",
        "mocked_init_variables": [],
        "mocked_var_paths": ["emails"],
        "contract_blockers": [],
        "contract_warnings": [],
        "run_output": "",
        "run_error": "",
        "run_warning": "",
        "runtime_fix_hints": [],
        "luacheck_output": "",
        "luacheck_error": "",
        "luacheck_warning": "",
        "failure_kind": "",
    }


class IntegrationStubLLM:
    """Configurable LLM stub that handles every system the pipeline calls."""

    def __init__(
        self,
        *,
        planner_response: dict | None = None,
        generate_response: str = "lua{ return wf.vars.value }lua",
        route_intent: str = "create",
    ) -> None:
        self.planner_response = planner_response if planner_response is not None else {
            "reformulated_task": "echo task",
            "identified_workflow_paths": [],
            "key_entities": [],
            "expected_result_action": "return",
            "needs_clarification": False,
            "clarification_questions": [],
            "confidence": 0.9,
        }
        self.generate_response = generate_response
        self.route_intent = route_intent

        self.planner_calls = 0
        self.generate_calls = 0
        self.last_generate_prompt = ""
        self.last_planner_prompt = ""

    async def generate(
        self,
        prompt: str,
        system: str = "",
        temperature: float = 0.2,
        max_tokens: int | None = None,
        *,
        agent_name: str = "",
    ) -> str:
        self.generate_calls += 1
        self.last_generate_prompt = prompt
        return self.generate_response

    async def generate_json(
        self,
        prompt: str,
        system: str = "",
        temperature: float = 0.0,
        *,
        agent_name: str = "",
    ) -> dict:
        if system.startswith(PLANNER_SYSTEM_PREFIX):
            self.planner_calls += 1
            self.last_planner_prompt = prompt
            return self.planner_response
        if system.startswith(ROUTE_SYSTEM_PREFIX):
            return {"intent": self.route_intent, "confidence": 1.0}
        if system.startswith(EXPLAIN_SYSTEM_PREFIX):
            return {
                "summary": "ok",
                "what_is_in_code": [],
                "how_it_works": [],
                "suggested_changes": [],
                "clarifying_questions": [],
            }
        raise AssertionError(f"unexpected generate_json system: {system[:80]}")

    async def chat(
        self,
        messages: list[dict],
        temperature: float = 0.2,
        max_tokens: int | None = None,
        *,
        agent_name: str = "",
    ) -> str:
        system = str(messages[0].get("content", "")) if messages else ""
        if system.startswith(FIX_VALIDATION_SYSTEM_PREFIX) or system.startswith(FIX_VERIFICATION_SYSTEM_PREFIX):
            return self.generate_response
        if system.startswith(VERIFY_SYSTEM_PREFIX):
            return json.dumps({
                "passed": True,
                "summary": "ok",
                "missing_requirements": [],
                "warnings": [],
            })
        raise AssertionError(f"unexpected chat system: {system[:80]}")


class PlannerEnabledPipelineTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp_path = Path("tests_tmp_planner_int")
        self.tmp_path.mkdir(exist_ok=True)
        self._env_patch = patch.dict(os.environ, {"PLANNER_ENABLED": "true"})
        self._env_patch.start()

    def tearDown(self) -> None:
        self._env_patch.stop()
        if self.tmp_path.exists():
            for child in self.tmp_path.iterdir():
                child.unlink()
            self.tmp_path.rmdir()

    def _run_engine(self, llm: IntegrationStubLLM, **kwargs):
        async def fake_diag(code: str, lua_bin: str = "lua55", startup_timeout: float = 3.0) -> dict:
            return _success_diagnostics()

        def fake_save(target_path: str, code: str, jsonstring_code: str = "") -> dict:
            return {
                "success": True,
                "saved_to": target_path,
                "saved_jsonstring_to": target_path + ".jsonstring.txt",
            }

        with patch("src.graph.nodes.async_run_diagnostics", new=fake_diag), \
             patch("src.graph.nodes.save_final_output", new=fake_save):
            engine = PipelineEngine(llm=llm)
            params = {
                "chat_id": 1,
                "user_input": "Get last email",
                "workspace_root": str(self.tmp_path),
                "target_path": str(self.tmp_path / "out.lua"),
            }
            params.update(kwargs)
            return asyncio.run(engine.process_message(**params))

    def test_planner_runs_when_enabled(self) -> None:
        llm = IntegrationStubLLM()
        result = self._run_engine(llm)
        self.assertGreaterEqual(llm.planner_calls, 1)
        self.assertIn("planner_result", result)
        self.assertEqual(result["planner_result"].get("reformulated_task"), "echo task")
        self.assertFalse(result.get("awaiting_planner_clarification", True))

    def test_planner_clarification_sets_awaiting_flag(self) -> None:
        llm = IntegrationStubLLM(planner_response={
            "reformulated_task": "ambiguous",
            "needs_clarification": True,
            "clarification_questions": ["Which workflow path?"],
            "confidence": 0.2,
        })
        result = self._run_engine(llm, user_input="создай скрипт неясной задачи")
        self.assertTrue(result.get("awaiting_planner_clarification"))
        self.assertEqual(result.get("planner_pending_questions"), ["Which workflow path?"])
        self.assertEqual(result.get("planner_original_input"), "создай скрипт неясной задачи")
        self.assertEqual(result.get("response_type"), "text")
        self.assertIn("Which workflow path?", result.get("response", ""))
        # Code generation should not have been called yet.
        self.assertEqual(llm.generate_calls, 0)

    def test_followup_answer_continues_to_generation(self) -> None:
        # First turn: planner asks clarification.
        llm = IntegrationStubLLM(planner_response={
            "reformulated_task": "still vague",
            "needs_clarification": True,
            "clarification_questions": ["What exactly?"],
            "confidence": 0.2,
        })
        first = self._run_engine(llm, user_input="создай скрипт неясной задачи")
        self.assertTrue(first["awaiting_planner_clarification"])

        # Second turn: planner is now satisfied; follow-up should bypass
        # resolve_target/route_intent and go straight into plan_request.
        llm.planner_response = {
            "reformulated_task": "Return wf.vars.last",
            "identified_workflow_paths": ["wf.vars.last"],
            "needs_clarification": False,
            "confidence": 0.9,
        }
        # Reset call counters so we can verify the planner is called again.
        llm.planner_calls = 0
        second = self._run_engine(
            llm,
            user_input="последний из wf.vars.last",
            awaiting_planner_clarification=True,
            planner_pending_questions=first["planner_pending_questions"],
            planner_original_input=first["planner_original_input"],
            planner_clarification_attempts=first["planner_clarification_attempts"],
        )
        self.assertGreaterEqual(llm.planner_calls, 1)
        self.assertFalse(second["awaiting_planner_clarification"])
        # Pipeline should have reached generation.
        self.assertGreaterEqual(llm.generate_calls, 1)
        # The planner saw merged input combining original + answer.
        self.assertIn("Исходная задача", llm.last_planner_prompt)
        self.assertIn("Ответ пользователя", llm.last_planner_prompt)

    def test_max_clarification_attempts_forces_continue(self) -> None:
        llm = IntegrationStubLLM(planner_response={
            "reformulated_task": "still ambiguous",
            "needs_clarification": True,
            "clarification_questions": ["Which?"],
            "confidence": 0.2,
        })
        # Pretend we already hit the max attempts; planner must force continue.
        result = self._run_engine(
            llm,
            user_input="answer",
            awaiting_planner_clarification=True,
            planner_pending_questions=["Which?"],
            planner_original_input="vague",
            planner_clarification_attempts=2,
        )
        self.assertFalse(result["awaiting_planner_clarification"])
        # Generation should run despite planner wanting more clarification.
        self.assertGreaterEqual(llm.generate_calls, 1)

    def test_active_code_clarification_answer_refines_existing_code(self) -> None:
        llm = IntegrationStubLLM(planner_response={
            "reformulated_task": "Modify the current Lua script so it returns only VIP contacts from wf.vars.contacts.",
            "identified_workflow_paths": ["wf.vars.contacts"],
            "target_operation": "filter",
            "expected_result_action": "return",
            "followup_action": "refine_existing_code",
            "needs_clarification": False,
            "clarification_questions": [],
            "confidence": 0.92,
        })
        result = self._run_engine(
            llm,
            user_input="только VIP-контакты",
            current_code="return wf.vars.contacts",
            base_prompt="Верни контакты из wf.vars.contacts",
            active_clarifying_questions=["Нужно вернуть все контакты или только VIP?"],
        )
        self.assertEqual(result["intent"], "change")
        self.assertEqual(result["change_requests"], ["только VIP-контакты"])
        self.assertIn("Current code:", llm.last_generate_prompt)
        self.assertIn("Change request:", llm.last_generate_prompt)

    def test_active_code_clarification_new_task_starts_fresh_generation(self) -> None:
        llm = IntegrationStubLLM(planner_response={
            "reformulated_task": "Return the last order from wf.vars.orders.",
            "identified_workflow_paths": ["wf.vars.orders"],
            "target_operation": "extract",
            "expected_result_action": "return",
            "followup_action": "start_new_generation",
            "needs_clarification": False,
            "clarification_questions": [],
            "confidence": 0.94,
        })
        result = self._run_engine(
            llm,
            user_input="сделай новый скрипт для последнего заказа из wf.vars.orders",
            current_code="return wf.vars.contacts",
            base_prompt="Верни контакты из wf.vars.contacts",
            active_clarifying_questions=["Нужно вернуть все контакты или только VIP?"],
        )
        self.assertEqual(result["intent"], "create")
        self.assertEqual(result["change_requests"], [])
        self.assertNotIn("Current code:", llm.last_generate_prompt)
        self.assertIn("Task:\nReturn the last order from wf.vars.orders.", llm.last_generate_prompt)


class PlannerPromptIntegrationTests(unittest.TestCase):
    def test_format_planner_section_renders_fields(self) -> None:
        compiled_request = {
            "planner_result": {
                "reformulated_task": "Get last item from wf.vars.emails",
                "identified_workflow_paths": ["wf.vars.emails"],
                "expected_result_action": "return",
            },
        }
        section = _format_planner_section(compiled_request)
        self.assertIn("Reformulated task:", section)
        self.assertIn("Get last item from wf.vars.emails", section)
        self.assertIn("Planner-identified workflow paths: wf.vars.emails", section)
        self.assertIn("Expected result action: return", section)

    def test_format_planner_section_skips_when_empty(self) -> None:
        self.assertEqual(_format_planner_section({}), "")
        self.assertEqual(_format_planner_section({"planner_result": {}}), "")

    def test_generation_prompt_includes_planner_section(self) -> None:
        compiled_request = {
            "task_text": "Get last email",
            "raw_context": "",
            "clarification_text": "",
            "planner_result": {
                "reformulated_task": "Return wf.vars.emails[#wf.vars.emails]",
                "identified_workflow_paths": ["wf.vars.emails"],
            },
        }
        prompt = _build_generation_prompt(compiled_request)
        self.assertIn("Planner analysis:", prompt)
        self.assertIn("Reformulated task:", prompt)
        self.assertIn("wf.vars.emails", prompt)


class PlannerDisabledPipelineTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp_path = Path("tests_tmp_planner_disabled")
        self.tmp_path.mkdir(exist_ok=True)
        self._env_patch = patch.dict(os.environ, {"PLANNER_ENABLED": "false"})
        self._env_patch.start()

    def tearDown(self) -> None:
        self._env_patch.stop()
        if self.tmp_path.exists():
            for child in self.tmp_path.iterdir():
                child.unlink()
            self.tmp_path.rmdir()

    def test_planner_skipped_when_disabled(self) -> None:
        async def fake_diag(code: str, lua_bin: str = "lua55", startup_timeout: float = 3.0) -> dict:
            return _success_diagnostics()

        def fake_save(target_path: str, code: str, jsonstring_code: str = "") -> dict:
            return {
                "success": True,
                "saved_to": target_path,
                "saved_jsonstring_to": target_path + ".jsonstring.txt",
            }

        llm = IntegrationStubLLM()
        with patch("src.graph.nodes.async_run_diagnostics", new=fake_diag), \
             patch("src.graph.nodes.save_final_output", new=fake_save):
            engine = PipelineEngine(llm=llm)
            result = asyncio.run(engine.process_message(
                chat_id=1,
                user_input="Get last email",
                workspace_root=str(self.tmp_path),
                target_path=str(self.tmp_path / "out.lua"),
            ))

        self.assertEqual(llm.planner_calls, 0)
        self.assertTrue(result.get("planner_skipped", False))
        # Pipeline still produced code generation.
        self.assertGreaterEqual(llm.generate_calls, 1)


if __name__ == "__main__":
    unittest.main()
