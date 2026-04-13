"""Tests for the standalone TaskPlanner agent module."""

import asyncio
import os
import unittest
from unittest.mock import patch

from src.agents.planner import (
    PlannerAgent,
    PlannerOutput,
    _build_clarification_response,
    _extract_workflow_paths_from_text,
    _is_planner_enabled,
    _normalize_planner_result,
    create_planner_node,
)


class StubLLM:
    """Minimal LLM stub that returns canned JSON responses."""

    def __init__(self, response: dict | None = None):
        self._response = response or {}
        self.call_count = 0
        self.last_prompt = ""
        self.last_system = ""

    async def generate(
        self,
        prompt: str,
        system: str = "",
        temperature: float = 0.2,
        max_tokens: int | None = None,
        *,
        agent_name: str = "",
    ) -> str:
        self.call_count += 1
        self.last_prompt = prompt
        self.last_system = system
        import json
        return json.dumps(self._response)

    async def generate_json(
        self,
        prompt: str,
        system: str = "",
        temperature: float = 0.0,
        *,
        agent_name: str = "",
    ) -> dict:
        self.call_count += 1
        self.last_prompt = prompt
        self.last_system = system
        return self._response

    async def chat(
        self,
        messages: list[dict],
        temperature: float = 0.2,
        max_tokens: int | None = None,
        *,
        agent_name: str = "",
    ) -> str:
        self.call_count += 1
        return ""


class TestExtractWorkflowPaths(unittest.TestCase):
    def test_extracts_vars_paths(self) -> None:
        text = "Получи последний элемент из wf.vars.emails и wf.vars.cart.items"
        paths = _extract_workflow_paths_from_text(text)
        self.assertEqual(paths, ["wf.vars.cart.items", "wf.vars.emails"])

    def test_extracts_init_variables_paths(self) -> None:
        text = "Конвертируй wf.initVariables.recallTime в unix"
        paths = _extract_workflow_paths_from_text(text)
        self.assertEqual(paths, ["wf.initVariables.recallTime"])

    def test_empty_text_returns_empty(self) -> None:
        self.assertEqual(_extract_workflow_paths_from_text(""), [])
        self.assertEqual(_extract_workflow_paths_from_text("просто текст без путей"), [])

    def test_deduplicates_paths(self) -> None:
        text = "wf.vars.emails и wf.vars.emails повторяются"
        paths = _extract_workflow_paths_from_text(text)
        self.assertEqual(paths, ["wf.vars.emails"])


class TestNormalizePlannerResult(unittest.TestCase):
    def test_valid_result_passes_through(self) -> None:
        raw = {
            "reformulated_task": "Get last email from wf.vars.emails",
            "identified_workflow_paths": ["wf.vars.emails"],
            "target_operation": "extract",
            "key_entities": ["emails", "last"],
            "data_types": {"wf.vars.emails": "array_string"},
            "expected_result_action": "return",
            "followup_action": "none",
            "needs_clarification": False,
            "clarification_questions": [],
            "confidence": 0.9,
        }
        result = _normalize_planner_result(raw, "original input")
        self.assertEqual(result["reformulated_task"], "Get last email from wf.vars.emails")
        self.assertEqual(result["target_operation"], "extract")
        self.assertEqual(result["followup_action"], "none")
        self.assertAlmostEqual(result["confidence"], 0.9)
        self.assertFalse(result["needs_clarification"])

    def test_empty_dict_fallback(self) -> None:
        result = _normalize_planner_result({}, "original input")
        self.assertEqual(result["reformulated_task"], "original input")
        self.assertEqual(result["target_operation"], "custom")
        self.assertEqual(result["confidence"], 0.0)
        self.assertFalse(result["needs_clarification"])
        self.assertEqual(result["clarification_questions"], [])

    def test_non_dict_fallback(self) -> None:
        result = _normalize_planner_result("garbage", "original input")  # type: ignore
        self.assertEqual(result["reformulated_task"], "original input")

    def test_invalid_operation_defaults_to_custom(self) -> None:
        raw = {"target_operation": "invalid_op"}
        result = _normalize_planner_result(raw, "task")
        self.assertEqual(result["target_operation"], "custom")

    def test_invalid_result_action_defaults_to_return(self) -> None:
        raw = {"expected_result_action": "something_weird"}
        result = _normalize_planner_result(raw, "task")
        self.assertEqual(result["expected_result_action"], "return")

    def test_invalid_followup_action_defaults_to_none(self) -> None:
        raw = {"followup_action": "unexpected_mode"}
        result = _normalize_planner_result(raw, "task")
        self.assertEqual(result["followup_action"], "none")

    def test_confidence_clamped(self) -> None:
        raw = {"confidence": 5.0}
        result = _normalize_planner_result(raw, "task")
        self.assertAlmostEqual(result["confidence"], 1.0)

        raw = {"confidence": -1.0}
        result = _normalize_planner_result(raw, "task")
        self.assertAlmostEqual(result["confidence"], 0.0)

    def test_confidence_non_numeric(self) -> None:
        raw = {"confidence": "not a number"}
        result = _normalize_planner_result(raw, "task")
        self.assertAlmostEqual(result["confidence"], 0.0)

    def test_questions_capped_at_3(self) -> None:
        raw = {"clarification_questions": ["q1", "q2", "q3", "q4", "q5"]}
        result = _normalize_planner_result(raw, "task")
        self.assertEqual(len(result["clarification_questions"]), 3)

    def test_data_types_filters_non_string(self) -> None:
        raw = {"data_types": {"wf.vars.x": "number", 123: "bad", "ok": 456}}
        result = _normalize_planner_result(raw, "task")
        self.assertIn("wf.vars.x", result["data_types"])
        self.assertNotIn(123, result["data_types"])


class TestBuildClarificationResponse(unittest.TestCase):
    def test_with_questions(self) -> None:
        result = {"clarification_questions": ["Что делать?", "С какими данными?"]}
        response = _build_clarification_response(result)
        self.assertIn("1. Что делать?", response)
        self.assertIn("2. С какими данными?", response)

    def test_without_questions(self) -> None:
        response = _build_clarification_response({})
        self.assertIn("Уточните", response)


class TestPlannerAgentDisabled(unittest.TestCase):
    def test_disabled_returns_skipped(self) -> None:
        llm = StubLLM()
        agent = PlannerAgent(llm, enabled=False)
        result = asyncio.run(agent.plan(user_input="test"))
        self.assertTrue(result["planner_skipped"])
        self.assertEqual(result["planner_result"], {})
        self.assertEqual(llm.call_count, 0)

    def test_enabled_property(self) -> None:
        llm = StubLLM()
        self.assertFalse(PlannerAgent(llm, enabled=False).enabled)
        self.assertTrue(PlannerAgent(llm, enabled=True).enabled)


class TestPlannerAgentPlan(unittest.TestCase):
    def test_reformulates_task_with_context(self) -> None:
        llm = StubLLM(response={
            "reformulated_task": "Получить последний элемент массива wf.vars.emails",
            "identified_workflow_paths": ["wf.vars.emails"],
            "target_operation": "extract",
            "key_entities": ["emails", "последний"],
            "data_types": {"wf.vars.emails": "array_string"},
            "expected_result_action": "return",
            "needs_clarification": False,
            "clarification_questions": [],
            "confidence": 0.95,
        })
        agent = PlannerAgent(llm, enabled=True)
        result = asyncio.run(agent.plan(
            user_input="Из полученного списка email получи последний",
            has_context=True,
            workflow_paths=["wf.vars.emails"],
        ))
        self.assertFalse(result["planner_skipped"])
        pr = result["planner_result"]
        self.assertIn("wf.vars.emails", pr["reformulated_task"])
        self.assertEqual(pr["target_operation"], "extract")
        self.assertGreater(pr["confidence"], 0.7)

    def test_identifies_workflow_paths(self) -> None:
        llm = StubLLM(response={
            "reformulated_task": "Transform DATUM and TIME",
            "identified_workflow_paths": [
                "wf.vars.json.IDOC.ZCDF_HEAD.DATUM",
                "wf.vars.json.IDOC.ZCDF_HEAD.TIME",
            ],
            "target_operation": "convert",
            "key_entities": ["DATUM", "TIME", "ISO 8601"],
            "data_types": {
                "wf.vars.json.IDOC.ZCDF_HEAD.DATUM": "string",
                "wf.vars.json.IDOC.ZCDF_HEAD.TIME": "string",
            },
            "expected_result_action": "return",
            "needs_clarification": False,
            "clarification_questions": [],
            "confidence": 0.85,
        })
        agent = PlannerAgent(llm, enabled=True)
        result = asyncio.run(agent.plan(
            user_input="Преобразуй DATUM и TIME из wf.vars.json.IDOC.ZCDF_HEAD в ISO дату",
            has_context=True,
        ))
        pr = result["planner_result"]
        self.assertIn("wf.vars.json.IDOC.ZCDF_HEAD.DATUM", pr["identified_workflow_paths"])
        self.assertIn("wf.vars.json.IDOC.ZCDF_HEAD.TIME", pr["identified_workflow_paths"])

    def test_detects_ambiguous_request(self) -> None:
        llm = StubLLM(response={
            "reformulated_task": "обработай данные",
            "identified_workflow_paths": [],
            "target_operation": "custom",
            "key_entities": ["данные"],
            "data_types": {},
            "expected_result_action": "return",
            "needs_clarification": True,
            "clarification_questions": [
                "Какие именно данные нужно обработать?",
                "Какую операцию выполнить?",
            ],
            "confidence": 0.2,
        })
        agent = PlannerAgent(llm, enabled=True)
        result = asyncio.run(agent.plan(user_input="обработай данные"))
        pr = result["planner_result"]
        self.assertTrue(pr["needs_clarification"])
        self.assertGreaterEqual(len(pr["clarification_questions"]), 1)
        self.assertLessEqual(len(pr["clarification_questions"]), 3)

    def test_provides_clarification_questions(self) -> None:
        llm = StubLLM(response={
            "reformulated_task": "Добавь переменную с квадратом числа",
            "identified_workflow_paths": [],
            "target_operation": "custom",
            "key_entities": ["квадрат", "переменная"],
            "data_types": {},
            "expected_result_action": "return",
            "needs_clarification": True,
            "clarification_questions": [
                "Какое число возвести в квадрат?",
                "Куда сохранить результат?",
            ],
            "confidence": 0.3,
        })
        agent = PlannerAgent(llm, enabled=True)
        result = asyncio.run(agent.plan(user_input="Добавь переменную с квадратом числа"))
        pr = result["planner_result"]
        self.assertTrue(pr["needs_clarification"])
        self.assertEqual(len(pr["clarification_questions"]), 2)
        self.assertIn("число", pr["clarification_questions"][0].lower())

    def test_handles_clear_request(self) -> None:
        llm = StubLLM(response={
            "reformulated_task": "Увеличить значение wf.vars.try_count_n на 1 и вернуть результат",
            "identified_workflow_paths": ["wf.vars.try_count_n"],
            "target_operation": "increment",
            "key_entities": ["try_count_n", "увеличить"],
            "data_types": {"wf.vars.try_count_n": "number"},
            "expected_result_action": "return",
            "needs_clarification": False,
            "clarification_questions": [],
            "confidence": 0.95,
        })
        agent = PlannerAgent(llm, enabled=True)
        result = asyncio.run(agent.plan(
            user_input="Увеличивай значение переменной try_count_n на каждой итерации",
            has_context=True,
            workflow_paths=["wf.vars.try_count_n"],
        ))
        pr = result["planner_result"]
        self.assertFalse(pr["needs_clarification"])
        self.assertGreater(pr["confidence"], 0.7)
        self.assertEqual(pr["target_operation"], "increment")

    def test_detects_operation_type(self) -> None:
        cases = [
            ("filter", "Отфильтруй элементы по Discount"),
            ("remove_keys", "Очисти значения переменных ID, ENTITY_ID"),
            ("convert", "Конвертируй время recallTime в unix"),
        ]
        for expected_op, user_input in cases:
            llm = StubLLM(response={
                "reformulated_task": user_input,
                "target_operation": expected_op,
                "needs_clarification": False,
                "confidence": 0.8,
            })
            agent = PlannerAgent(llm, enabled=True)
            result = asyncio.run(agent.plan(user_input=user_input))
            pr = result["planner_result"]
            self.assertEqual(pr["target_operation"], expected_op, f"Failed for: {user_input}")

    def test_extracts_key_entities(self) -> None:
        llm = StubLLM(response={
            "reformulated_task": "Convert DATUM and TIME to ISO",
            "key_entities": ["DATUM", "TIME", "ZCDF_HEAD", "ISO 8601"],
            "target_operation": "convert",
            "needs_clarification": False,
            "confidence": 0.85,
        })
        agent = PlannerAgent(llm, enabled=True)
        result = asyncio.run(agent.plan(
            user_input="Преобразуй DATUM и TIME из wf.vars.json.IDOC.ZCDF_HEAD"
        ))
        pr = result["planner_result"]
        self.assertIn("DATUM", pr["key_entities"])
        self.assertIn("TIME", pr["key_entities"])
        self.assertIn("ZCDF_HEAD", pr["key_entities"])

    def test_handles_malformed_json(self) -> None:
        llm = StubLLM(response={})  # Empty dict = malformed
        agent = PlannerAgent(llm, enabled=True)
        original_input = "my task here"
        result = asyncio.run(agent.plan(user_input=original_input))
        pr = result["planner_result"]
        # Should fallback gracefully
        self.assertEqual(pr["reformulated_task"], original_input)
        self.assertEqual(pr["target_operation"], "custom")
        self.assertAlmostEqual(pr["confidence"], 0.0)
        self.assertFalse(pr["needs_clarification"])

    def test_always_runs_for_code_generation(self) -> None:
        """Planner should always make an LLM call when enabled, regardless of intent."""
        for intent in ("create", "change", "retry"):
            llm = StubLLM(response={
                "reformulated_task": "test",
                "target_operation": "extract",
                "needs_clarification": False,
                "confidence": 0.8,
            })
            agent = PlannerAgent(llm, enabled=True)
            result = asyncio.run(agent.plan(user_input="test", intent=intent))
            self.assertFalse(result["planner_skipped"], f"Should not skip for intent={intent}")
            self.assertEqual(llm.call_count, 1, f"Should call LLM for intent={intent}")

    def test_llm_receives_correct_prompt(self) -> None:
        llm = StubLLM(response={"reformulated_task": "x", "confidence": 0.5})
        agent = PlannerAgent(llm, enabled=True)
        asyncio.run(agent.plan(
            user_input="Get last email from wf.vars.emails",
            has_context=True,
            workflow_paths=["wf.vars.emails"],
        ))
        self.assertIn("Get last email", llm.last_prompt)
        self.assertIn("wf.vars.emails", llm.last_prompt)
        self.assertIn("true", llm.last_prompt)  # has_context
        self.assertIn("task analyst", llm.last_system)


class TestEnvToggle(unittest.TestCase):
    def test_env_true(self) -> None:
        with patch.dict(os.environ, {"PLANNER_ENABLED": "true"}):
            self.assertTrue(_is_planner_enabled())

    def test_env_false(self) -> None:
        with patch.dict(os.environ, {"PLANNER_ENABLED": "false"}):
            self.assertFalse(_is_planner_enabled())

    def test_env_missing(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            # Remove PLANNER_ENABLED if present
            os.environ.pop("PLANNER_ENABLED", None)
            self.assertFalse(_is_planner_enabled())

    def test_env_1(self) -> None:
        with patch.dict(os.environ, {"PLANNER_ENABLED": "1"}):
            self.assertTrue(_is_planner_enabled())

    def test_env_yes(self) -> None:
        with patch.dict(os.environ, {"PLANNER_ENABLED": "yes"}):
            self.assertTrue(_is_planner_enabled())


class TestCreatePlannerNode(unittest.TestCase):
    def test_returns_callable(self) -> None:
        llm = StubLLM()
        node = create_planner_node(llm)
        self.assertTrue(callable(node))

    def test_node_accepts_pipeline_state(self) -> None:
        llm = StubLLM(response={
            "reformulated_task": "test task",
            "target_operation": "extract",
            "needs_clarification": False,
            "confidence": 0.8,
        })
        with patch.dict(os.environ, {"PLANNER_ENABLED": "true"}):
            node = create_planner_node(llm)
            # Minimal PipelineState-like dict
            state = {
                "user_input": "Get last email",
                "intent": "create",
                "current_code": "",
                "compiled_request": {},
            }
            result = asyncio.run(node(state))
            self.assertIn("planner_result", result)
            self.assertIn("planner_skipped", result)
            self.assertFalse(result["planner_skipped"])

    def test_node_clarification_sets_response(self) -> None:
        llm = StubLLM(response={
            "reformulated_task": "unclear task",
            "target_operation": "custom",
            "needs_clarification": True,
            "clarification_questions": ["What exactly do you need?"],
            "confidence": 0.2,
        })
        with patch.dict(os.environ, {"PLANNER_ENABLED": "true"}):
            node = create_planner_node(llm)
            state = {
                "user_input": "do something",
                "intent": "create",
                "current_code": "",
                "compiled_request": {},
            }
            result = asyncio.run(node(state))
            self.assertIn("response", result)
            self.assertEqual(result["response_type"], "text")
            self.assertEqual(result["failure_stage"], "clarification")
            self.assertIn("What exactly do you need?", result["response"])
            self.assertEqual(result["clarifying_questions"], ["What exactly do you need?"])

    def test_node_disabled_skips(self) -> None:
        llm = StubLLM()
        with patch.dict(os.environ, {"PLANNER_ENABLED": "false"}):
            node = create_planner_node(llm)
            state = {
                "user_input": "test",
                "intent": "create",
                "current_code": "",
                "compiled_request": {},
            }
            result = asyncio.run(node(state))
            self.assertTrue(result["planner_skipped"])
            self.assertEqual(llm.call_count, 0)

    def test_node_reads_has_context_from_compiled_request(self) -> None:
        """When compiled_request has parseable context, planner should get has_context=True."""
        llm = StubLLM(response={
            "reformulated_task": "task with context",
            "target_operation": "extract",
            "needs_clarification": False,
            "confidence": 0.9,
        })
        with patch.dict(os.environ, {"PLANNER_ENABLED": "true"}):
            node = create_planner_node(llm)
            state = {
                "user_input": "get emails",
                "intent": "create",
                "current_code": "",
                "compiled_request": {"has_parseable_context": True},
            }
            asyncio.run(node(state))
            # Verify prompt contains "true" for has_context
            self.assertIn("true", llm.last_prompt)


if __name__ == "__main__":
    unittest.main()
