"""Unit tests for the standalone ShapeTypeVerifier agent module."""

import asyncio
import unittest

from src.agents.shape_type_verifier import (
    ShapeTypeVerifierAgent,
    _SYSTEM_PROMPT,
    _build_shape_type_verifier_prompt,
    _normalize_shape_type_verifier_result,
    build_shape_type_verifier_input_from_state,
    create_shape_type_verifier_node,
    to_aggregate_verification_result,
)
from src.agents.verification_chain import create_verification_chain_nodes, get_verification_chain_specs


class StubLLM:
    """Minimal LLM stub that returns canned JSON responses."""

    def __init__(self, response: object = None):
        self._response = response if response is not None else {}
        self.call_count = 0
        self.last_prompt = ""
        self.last_system = ""
        self.last_agent_name = ""

    async def generate_json(
        self,
        prompt: str,
        system: str = "",
        temperature: float = 0.0,
        *,
        agent_name: str = "",
    ) -> object:
        self.call_count += 1
        self.last_prompt = prompt
        self.last_system = system
        self.last_agent_name = agent_name
        return self._response


class TestNormalizeShapeTypeVerifierResult(unittest.TestCase):
    def test_passed_result_gets_safe_defaults(self) -> None:
        result = _normalize_shape_type_verifier_result({"passed": True, "summary": "OK"})
        self.assertTrue(result["passed"])
        self.assertEqual(result["verifier_name"], "ShapeTypeVerifier")
        self.assertIsNone(result["error_family"])
        self.assertIsNone(result["error_code"])
        self.assertEqual(result["severity"], "low")
        self.assertEqual(result["fixer_brief"]["patch_scope"], "none")

    def test_failed_result_defaults_to_high_severity(self) -> None:
        result = _normalize_shape_type_verifier_result(
            {
                "passed": False,
                "summary": "Wrong shape.",
                "expected": {"shape": "array"},
                "actual": {"shape": "object"},
                "fixer_brief": {"must_change": ["Normalize wf.vars.contacts into an array-like table."]},
            }
        )
        self.assertFalse(result["passed"])
        self.assertEqual(result["severity"], "high")
        self.assertEqual(result["expected"]["shape"], "array-like table")
        self.assertEqual(result["actual"]["shape"], "object-like table")


class TestShapeTypeVerifierPrompt(unittest.TestCase):
    def test_prompt_contains_target_shape_and_after_state(self) -> None:
        prompt = _build_shape_type_verifier_prompt(
            {
                "task": "If contacts is not an array, wrap it into an array.",
                "code": "return wf.vars.contacts",
                "target_field_path": "wf.vars.contacts",
                "selected_primary_type": "object",
                "semantic_expectations": ["array_normalization"],
                "runtime_result": {"name": "Ivan"},
                "after_state": {"wf": {"vars": {"contacts": {"name": "Ivan"}}}},
            }
        )
        self.assertIn("target field path", prompt)
        self.assertIn("expected shape", prompt)
        self.assertIn("after_state value at wf.vars.contacts", prompt)
        self.assertIn('"verifier_name": "ShapeTypeVerifier"', prompt)


class TestShapeTypeVerifierAgent(unittest.TestCase):
    def test_after_state_failure_has_field_path_and_skips_llm(self) -> None:
        llm = StubLLM(response={"passed": True, "summary": "Should not be used."})
        agent = ShapeTypeVerifierAgent(llm)
        result = asyncio.run(
            agent.verify(
                {
                    "task": "If contacts is not an array, wrap it into an array.",
                    "target_field_path": "wf.vars.contacts",
                    "selected_primary_type": "object",
                    "semantic_expectations": ["array_normalization"],
                    "before_state": {"wf": {"vars": {"contacts": {"name": "Ivan"}}}},
                    "after_state": {"wf": {"vars": {"contacts": {"name": "Ivan"}}}},
                    "code": "return wf.vars.contacts",
                }
            )
        )
        self.assertFalse(result["passed"])
        self.assertEqual(result["error_code"], "object_left_object")
        self.assertEqual(result["field_path"], "wf.vars.contacts")
        self.assertEqual(llm.call_count, 0)
        self.assertEqual(result["expected"]["shape"], "array-like table")
        self.assertEqual(result["actual"]["shape"], "object-like table")

    def test_after_state_success_skips_llm(self) -> None:
        llm = StubLLM(response={"passed": False, "summary": "Should not be used."})
        agent = ShapeTypeVerifierAgent(llm)
        result = asyncio.run(
            agent.verify(
                {
                    "task": "If contacts is not an array, wrap it into an array.",
                    "target_field_path": "wf.vars.contacts",
                    "selected_primary_type": "object",
                    "semantic_expectations": ["array_normalization"],
                    "after_state": {"wf": {"vars": {"contacts": [{"name": "Ivan"}]}}},
                    "code": "return wf.vars.contacts",
                }
            )
        )
        self.assertTrue(result["passed"])
        self.assertEqual(llm.call_count, 0)
        self.assertIn("expected `array-like table`", result["summary"])

    def test_table_only_shape_logic_short_circuits_without_llm(self) -> None:
        llm = StubLLM(response={"passed": True, "summary": "Should not be used."})
        agent = ShapeTypeVerifierAgent(llm)
        result = asyncio.run(
            agent.verify(
                {
                    "task": "If contacts is not an array, wrap it into an array.",
                    "target_field_path": "wf.vars.contacts",
                    "selected_primary_type": "object",
                    "semantic_expectations": ["array_normalization"],
                    "code": (
                        "local contacts = wf.vars.contacts\n"
                        "if type(contacts) ~= \"table\" then\n"
                        "    return contacts\n"
                        "end\n"
                        "return contacts\n"
                    ),
                }
            )
        )
        self.assertFalse(result["passed"])
        self.assertEqual(result["error_code"], "table_only_shape_check")
        self.assertEqual(result["field_path"], "wf.vars.contacts")
        self.assertEqual(llm.call_count, 0)

    def test_mark_as_array_wrong_level_short_circuits_without_llm(self) -> None:
        llm = StubLLM(response={"passed": True, "summary": "Should not be used."})
        agent = ShapeTypeVerifierAgent(llm)
        result = asyncio.run(
            agent.verify(
                {
                    "task": "Normalize wf.vars.payload.contacts.items into an array.",
                    "target_field_path": "wf.vars.payload.contacts.items",
                    "selected_primary_type": "object",
                    "semantic_expectations": ["array_normalization"],
                    "code": "_utils.array.markAsArray(wf.vars.payload.contacts)\nreturn wf.vars.payload.contacts.items",
                }
            )
        )
        self.assertFalse(result["passed"])
        self.assertEqual(result["error_code"], "mark_as_array_wrong_level")
        self.assertEqual(result["field_path"], "wf.vars.payload.contacts.items")
        self.assertEqual(llm.call_count, 0)

    def test_verify_calls_llm_when_no_evidence_or_local_blocker(self) -> None:
        llm = StubLLM(
            response={
                "passed": False,
                "error_family": "shape_type",
                "error_code": "nested_field_wrong_shape",
                "severity": "high",
                "summary": "Nested field remains object-like.",
                "field_path": "wf.vars.payload.contacts.items",
                "evidence": ["items stayed object-like"],
                "expected": {"shape": "array-like table"},
                "actual": {"shape": "object-like table"},
                "fixer_brief": {
                    "goal": "Normalize only the nested field shape.",
                    "must_change": ["Normalize wf.vars.payload.contacts.items into an array-like table."],
                    "must_preserve": ["Keep parent objects unchanged."],
                    "forbidden_fixes": ["Do not normalize wf.vars.payload.contacts instead."],
                    "suggested_patch": "Apply the normalization to wf.vars.payload.contacts.items.",
                    "patch_scope": "local",
                },
                "confidence": 0.87,
            }
        )
        agent = ShapeTypeVerifierAgent(llm)
        result = asyncio.run(
            agent.verify(
                {
                    "task": "Normalize wf.vars.payload.contacts.items into an array.",
                    "target_field_path": "wf.vars.payload.contacts.items",
                    "selected_primary_type": "object",
                    "semantic_expectations": [],
                    "expected_shape": "array-like table",
                    "code": "return wf.vars.payload.contacts.items",
                }
            )
        )
        self.assertFalse(result["passed"])
        self.assertEqual(result["error_code"], "nested_field_wrong_shape")
        self.assertEqual(result["field_path"], "wf.vars.payload.contacts.items")
        self.assertEqual(llm.call_count, 1)
        self.assertEqual(llm.last_system, _SYSTEM_PROMPT)
        self.assertEqual(llm.last_agent_name, "ShapeTypeVerifier")


class TestAggregateAdapter(unittest.TestCase):
    def test_aggregate_adapter_preserves_compatibility_fields(self) -> None:
        aggregate = to_aggregate_verification_result(
            {
                "verifier_name": "ShapeTypeVerifier",
                "passed": False,
                "error_family": "shape_type",
                "error_code": "object_left_object",
                "severity": "high",
                "summary": "wf.vars.contacts stayed object-like.",
                "field_path": "wf.vars.contacts",
                "evidence": ["after_state shows an object"],
                "expected": {"shape": "array-like table"},
                "actual": {"shape": "object-like table"},
                "fixer_brief": {
                    "goal": "Normalize the target field.",
                    "must_change": ["Normalize wf.vars.contacts into an array-like table."],
                    "must_preserve": [],
                    "forbidden_fixes": [],
                    "suggested_patch": "Wrap the object into a new array.",
                    "patch_scope": "local",
                },
                "confidence": 0.93,
            }
        )
        self.assertFalse(aggregate["passed"])
        self.assertEqual(
            aggregate["missing_requirements"],
            ["Normalize wf.vars.contacts into an array-like table."],
        )
        self.assertEqual(aggregate["verifier_name"], "ShapeTypeVerifier")


class TestStateBridgeAndNode(unittest.TestCase):
    def test_build_input_from_state_uses_current_pipeline_fields(self) -> None:
        payload = build_shape_type_verifier_input_from_state(
            {
                "user_input": "fallback text",
                "generated_code": "return wf.vars.contacts",
                "compiled_request": {
                    "verification_prompt": "If contacts is not an array, wrap it into an array.",
                    "selected_primary_path": "wf.vars.contacts",
                    "selected_primary_type": "object",
                    "expected_workflow_paths": ["wf.vars.contacts"],
                    "semantic_expectations": ["array_normalization"],
                    "has_parseable_context": True,
                    "parsed_context": {"wf": {"vars": {"contacts": {"name": "Ivan"}}}},
                },
                "diagnostics": {
                    "workflow_state": {"wf": {"vars": {"contacts": [{"name": "Ivan"}]}}},
                    "result_preview": "",
                },
            }
        )
        self.assertEqual(payload["task"], "If contacts is not an array, wrap it into an array.")
        self.assertEqual(payload["target_field_path"], "wf.vars.contacts")
        self.assertEqual(payload["selected_primary_type"], "object")
        self.assertEqual(payload["semantic_expectations"], ["array_normalization"])
        self.assertIsNotNone(payload["before_state"])
        self.assertIsNotNone(payload["after_state"])

    def test_node_bridges_result_to_aggregate_verification(self) -> None:
        llm = StubLLM(
            response={
                "passed": True,
                "summary": "Shape/type check passed.",
                "severity": "low",
                "fixer_brief": {"patch_scope": "none"},
                "confidence": 0.91,
            }
        )
        node = create_shape_type_verifier_node(llm)
        result = asyncio.run(
            node(
                {
                    "generated_code": "return wf.vars.contacts",
                    "compiled_request": {
                        "verification_prompt": "If contacts is not an array, wrap it into an array.",
                        "selected_primary_path": "wf.vars.contacts",
                        "selected_primary_type": "object",
                        "expected_workflow_paths": ["wf.vars.contacts"],
                        "semantic_expectations": ["array_normalization"],
                    },
                    "diagnostics": {
                        "workflow_state": {"wf": {"vars": {"contacts": [{"name": "Ivan"}]}}},
                    },
                }
            )
        )
        self.assertTrue(result["verification_passed"])
        self.assertIn("shape_type_verifier_result", result)
        self.assertEqual(result["verification"]["verifier_name"], "ShapeTypeVerifier")


class TestVerificationChainRegistry(unittest.TestCase):
    def test_registry_contains_first_two_verifiers_in_order(self) -> None:
        specs = get_verification_chain_specs()
        self.assertEqual(specs[0]["verifier_name"], "ContractVerifier")
        self.assertEqual(specs[1]["verifier_name"], "ShapeTypeVerifier")
        self.assertEqual(specs[0]["default_max_fix_iterations"], 1)
        self.assertEqual(specs[1]["default_max_fix_iterations"], 1)

    def test_registry_builds_nodes(self) -> None:
        nodes = create_verification_chain_nodes(StubLLM())
        self.assertIn("verify_contract", nodes)
        self.assertIn("verify_shape_type", nodes)
        self.assertTrue(callable(nodes["verify_contract"]))
        self.assertTrue(callable(nodes["verify_shape_type"]))


if __name__ == "__main__":
    unittest.main()
