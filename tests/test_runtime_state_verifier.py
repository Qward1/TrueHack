"""Unit tests for the standalone RuntimeStateVerifier agent module."""

import asyncio
import unittest

from src.agents.runtime_state_verifier import (
    RuntimeStateVerifierAgent,
    _SYSTEM_PROMPT,
    _build_runtime_state_verifier_prompt,
    _normalize_runtime_state_verifier_result,
    build_runtime_state_verifier_input_from_state,
    create_runtime_state_verifier_node,
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


class TestNormalizeRuntimeStateVerifierResult(unittest.TestCase):
    def test_passed_result_gets_safe_defaults(self) -> None:
        result = _normalize_runtime_state_verifier_result({"passed": True, "summary": "OK"})
        self.assertTrue(result["passed"])
        self.assertEqual(result["verifier_name"], "RuntimeStateVerifier")
        self.assertIsNone(result["error_family"])
        self.assertIsNone(result["error_code"])
        self.assertEqual(result["severity"], "low")
        self.assertEqual(result["fixer_brief"]["patch_scope"], "none")

    def test_failed_result_defaults_to_high_severity(self) -> None:
        result = _normalize_runtime_state_verifier_result(
            {
                "passed": False,
                "summary": "Wrong path updated.",
                "fixer_brief": {"must_change": ["Update the right path."]},
            }
        )
        self.assertFalse(result["passed"])
        self.assertEqual(result["severity"], "high")


class TestRuntimeStateVerifierPrompt(unittest.TestCase):
    def test_prompt_contains_diff_whitelists_and_focused_evidence(self) -> None:
        prompt = _build_runtime_state_verifier_prompt(
            {
                "task": "Save count to wf.vars.cart_count.",
                "code": "local total = 2\nwf.vars.cart_count = total",
                "source_field_path": "wf.vars.cart.items",
                "output_field_path": "wf.vars.cart_count",
                "selected_operation": "count",
                "before_state": {"wf": {"vars": {"cart": {"items": [{"sku": "A"}]}, "cart_count": 0}}},
                "after_state": {"wf": {"vars": {"cart": {"items": [{"sku": "A"}]}, "cart_count": 2}}},
                "runtime_result": {"count": 2},
            }
        )
        self.assertIn("Observed changed paths", prompt)
        self.assertIn("Relevant diff for wf.vars.cart_count", prompt)
        self.assertIn("expected result action", prompt)
        self.assertIn("allowed_workflow_paths", prompt)
        self.assertIn("available_code_variables", prompt)
        self.assertIn("runtime_result", prompt)
        self.assertIn("before_state value at wf.vars.cart_count", prompt)
        self.assertIn("after_state value at wf.vars.cart_count", prompt)
        self.assertNotIn("Parsed workflow context:", prompt)


class TestRuntimeStateVerifierAgent(unittest.TestCase):
    def test_expected_save_action_with_rootless_after_state_passes_without_llm(self) -> None:
        llm = StubLLM(response={"passed": False, "summary": "Should not be used."})
        agent = RuntimeStateVerifierAgent(llm)
        result = asyncio.run(
            agent.verify(
                {
                    "task": "For each item in RESTbody.result, remove ID, ENTITY_ID, and CALL from workflow variables.",
                    "expected_result_action": "save_to_wf_vars",
                    "source_field_path": "wf.vars.RESTbody.result",
                    "before_state": {
                        "wf": {
                            "vars": {
                                "RESTbody": {
                                    "result": [
                                        {"ID": 123, "ENTITY_ID": 456, "CALL": "x", "OTHER_KEY": "value"}
                                    ]
                                }
                            }
                        }
                    },
                    "after_state": {
                        "vars": {
                            "RESTbody": {
                                "result": [
                                    {"OTHER_KEY": "value"}
                                ]
                            }
                        },
                        "initVariables": [],
                    },
                    "runtime_result": [{"OTHER_KEY": "value"}],
                    "code": "local result = wf.vars.RESTbody.result\nreturn result",
                }
            )
        )
        self.assertTrue(result["passed"])
        self.assertEqual(llm.call_count, 0)
        self.assertIn("expected path", result["summary"])

    def test_wrong_path_updated_skips_llm(self) -> None:
        llm = StubLLM(response={"passed": True, "summary": "Should not be used."})
        agent = RuntimeStateVerifierAgent(llm)
        result = asyncio.run(
            agent.verify(
                {
                    "task": "Save count to wf.vars.cart_count.",
                    "output_field_path": "wf.vars.cart_count",
                    "before_state": {"wf": {"vars": {"cart_count": 0, "debug": 0}}},
                    "after_state": {"wf": {"vars": {"cart_count": 0, "debug": 1}}},
                    "code": "wf.vars.debug = 1",
                }
            )
        )
        self.assertFalse(result["passed"])
        self.assertEqual(result["error_code"], "wrong_path_updated")
        self.assertEqual(result["field_path"], "wf.vars.debug")
        self.assertEqual(llm.call_count, 0)

    def test_required_field_unchanged_skips_llm(self) -> None:
        llm = StubLLM(response={"passed": True, "summary": "Should not be used."})
        agent = RuntimeStateVerifierAgent(llm)
        result = asyncio.run(
            agent.verify(
                {
                    "task": "Save count to wf.vars.cart_count.",
                    "output_field_path": "wf.vars.cart_count",
                    "before_state": {"wf": {"vars": {"cart_count": 0}}},
                    "after_state": {"wf": {"vars": {"cart_count": 0}}},
                    "code": "return 0",
                }
            )
        )
        self.assertFalse(result["passed"])
        self.assertEqual(result["error_code"], "required_field_unchanged")
        self.assertEqual(result["field_path"], "wf.vars.cart_count")
        self.assertEqual(llm.call_count, 0)

    def test_extra_unintended_state_change_skips_llm(self) -> None:
        llm = StubLLM(response={"passed": True, "summary": "Should not be used."})
        agent = RuntimeStateVerifierAgent(llm)
        result = asyncio.run(
            agent.verify(
                {
                    "task": "Save count to wf.vars.cart_count.",
                    "output_field_path": "wf.vars.cart_count",
                    "before_state": {"wf": {"vars": {"cart_count": 0, "debug": 0}}},
                    "after_state": {"wf": {"vars": {"cart_count": 2, "debug": 1}}},
                    "code": "wf.vars.cart_count = 2\nwf.vars.debug = 1",
                }
            )
        )
        self.assertFalse(result["passed"])
        self.assertEqual(result["error_code"], "extra_unintended_state_change")
        self.assertEqual(result["field_path"], "wf.vars.debug")
        self.assertEqual(llm.call_count, 0)

    def test_runtime_result_contradiction_skips_llm(self) -> None:
        llm = StubLLM(response={"passed": True, "summary": "Should not be used."})
        agent = RuntimeStateVerifierAgent(llm)
        result = asyncio.run(
            agent.verify(
                {
                    "task": "Count items in wf.vars.cart.items.",
                    "source_field_path": "wf.vars.cart.items",
                    "selected_operation": "count",
                    "before_state": {"wf": {"vars": {"cart": {"items": [{"sku": "A"}, {"sku": "B"}]}}}},
                    "runtime_result": {"count": 3},
                    "code": "return { count = 3 }",
                }
            )
        )
        self.assertFalse(result["passed"])
        self.assertEqual(result["error_code"], "runtime_result_contradicts_request")
        self.assertEqual(llm.call_count, 0)

    def test_wrong_value_after_execution_skips_llm(self) -> None:
        llm = StubLLM(response={"passed": True, "summary": "Should not be used."})
        agent = RuntimeStateVerifierAgent(llm)
        result = asyncio.run(
            agent.verify(
                {
                    "task": "Save count to wf.vars.cart_count.",
                    "source_field_path": "wf.vars.cart.items",
                    "output_field_path": "wf.vars.cart_count",
                    "selected_operation": "count",
                    "before_state": {"wf": {"vars": {"cart": {"items": [{"sku": "A"}, {"sku": "B"}]}, "cart_count": 0}}},
                    "after_state": {"wf": {"vars": {"cart": {"items": [{"sku": "A"}, {"sku": "B"}]}, "cart_count": 5}}},
                    "code": "wf.vars.cart_count = 5",
                }
            )
        )
        self.assertFalse(result["passed"])
        self.assertEqual(result["error_code"], "wrong_value_after_execution")
        self.assertEqual(result["field_path"], "wf.vars.cart_count")
        self.assertEqual(llm.call_count, 0)

    def test_positive_execution_evidence_passes_without_llm(self) -> None:
        llm = StubLLM(response={"passed": False, "summary": "Should not be used."})
        agent = RuntimeStateVerifierAgent(llm)
        result = asyncio.run(
            agent.verify(
                {
                    "task": "Save count to wf.vars.cart_count.",
                    "output_field_path": "wf.vars.cart_count",
                    "before_state": {"wf": {"vars": {"cart_count": 0}}},
                    "after_state": {"wf": {"vars": {"cart_count": 2}}},
                    "code": "wf.vars.cart_count = 2",
                }
            )
        )
        self.assertTrue(result["passed"])
        self.assertEqual(llm.call_count, 0)
        self.assertIn("expected path", result["summary"])

    def test_verify_calls_llm_when_no_concrete_evidence(self) -> None:
        llm = StubLLM(
            response={
                "passed": False,
                "error_family": "runtime_state",
                "error_code": "runtime_result_contradicts_request",
                "severity": "high",
                "summary": "Observed runtime evidence contradicts the task.",
                "field_path": "wf.vars.total",
                "evidence": ["LLM reasoned from partial execution evidence."],
                "expected": {"expected_behavior": "Write the correct total."},
                "actual": {"actual_behavior": "Wrote the wrong total."},
                "fixer_brief": {
                    "goal": "Fix the execution target only.",
                    "must_change": ["Write the correct total."],
                    "must_preserve": ["Keep unrelated paths unchanged."],
                    "forbidden_fixes": ["Do not mutate extra paths."],
                    "suggested_patch": "Fix the write target and value.",
                    "patch_scope": "local",
                },
                "confidence": 0.86,
            }
        )
        agent = RuntimeStateVerifierAgent(llm)
        result = asyncio.run(
            agent.verify(
                {
                    "task": "Save the correct total to wf.vars.total.",
                    "output_field_path": "wf.vars.total",
                    "code": "wf.vars.total = value",
                }
            )
        )
        self.assertFalse(result["passed"])
        self.assertEqual(result["error_code"], "runtime_result_contradicts_request")
        self.assertEqual(llm.call_count, 1)
        self.assertEqual(llm.last_system, _SYSTEM_PROMPT)
        self.assertEqual(llm.last_agent_name, "RuntimeStateVerifier")


class TestAggregateAdapter(unittest.TestCase):
    def test_aggregate_adapter_preserves_compatibility_fields(self) -> None:
        aggregate = to_aggregate_verification_result(
            {
                "verifier_name": "RuntimeStateVerifier",
                "passed": False,
                "error_family": "runtime_state",
                "error_code": "wrong_path_updated",
                "severity": "high",
                "summary": "Updated wrong path.",
                "field_path": "wf.vars.debug",
                "evidence": ["diff shows wrong path update"],
                "expected": {"expected_behavior": "Update wf.vars.count."},
                "actual": {"changed_paths": ["wf.vars.debug"]},
                "fixer_brief": {
                    "goal": "Fix the execution target only.",
                    "must_change": ["Update wf.vars.count instead."],
                    "must_preserve": [],
                    "forbidden_fixes": [],
                    "suggested_patch": "Move the write target.",
                    "patch_scope": "local",
                },
                "confidence": 0.94,
            }
        )
        self.assertFalse(aggregate["passed"])
        self.assertEqual(aggregate["missing_requirements"], ["Update wf.vars.count instead."])
        self.assertEqual(aggregate["verifier_name"], "RuntimeStateVerifier")


class TestStateBridgeAndNode(unittest.TestCase):
    def test_build_input_from_state_uses_current_pipeline_fields(self) -> None:
        payload = build_runtime_state_verifier_input_from_state(
            {
                "generated_code": "local total = 2\nwf.vars.cart_count = total",
                "compiled_request": {
                    "verification_prompt": "Save count to wf.vars.cart_count.",
                    "selected_primary_path": "wf.vars.cart.items",
                    "selected_operation": "count",
                    "selected_save_path": "wf.vars.cart_count",
                    "operation_argument": None,
                    "semantic_expectations": ["numeric_aggregation"],
                    "expected_workflow_paths": ["wf.vars.cart.items"],
                    "workflow_path_inventory": [
                        {"path": "wf.vars.cart.items"},
                        {"path": "wf.vars.cart_count"},
                    ],
                    "has_parseable_context": True,
                    "parsed_context": {"wf": {"vars": {"cart": {"items": [{"sku": "A"}, {"sku": "B"}]}, "cart_count": 0}}},
                },
                "diagnostics": {
                    "result_value": {"count": 2},
                    "workflow_state": {"wf": {"vars": {"cart_count": 2}}},
                },
            }
        )
        self.assertEqual(payload["source_field_path"], "wf.vars.cart.items")
        self.assertEqual(payload["output_field_path"], "wf.vars.cart_count")
        self.assertEqual(payload["selected_operation"], "count")
        self.assertEqual(payload["semantic_expectations"], ["numeric_aggregation"])
        self.assertIn("wf.vars.cart.items", payload["allowed_workflow_paths"])
        self.assertIn("wf.vars.cart_count", payload["allowed_workflow_paths"])
        self.assertIn("total", payload["available_code_variables"])
        self.assertTrue(payload["available_runtime_evidence"]["after_state"])

    def test_build_input_uses_planner_expected_action_and_normalizes_rootless_after_state(self) -> None:
        payload = build_runtime_state_verifier_input_from_state(
            {
                "generated_code": "local result = wf.vars.RESTbody.result\nreturn result",
                "compiled_request": {
                    "verification_prompt": "Remove ID, ENTITY_ID and CALL from wf.vars.RESTbody.result.",
                    "selected_primary_path": "wf.vars.RESTbody.result",
                    "selected_operation": "remove_keys",
                    "selected_save_path": "",
                    "semantic_expectations": ["remove_keys"],
                    "expected_workflow_paths": ["wf.vars.RESTbody.result"],
                    "workflow_path_inventory": [{"path": "wf.vars.RESTbody.result"}],
                    "has_parseable_context": True,
                    "parsed_context": {
                        "wf": {
                            "vars": {
                                "RESTbody": {
                                    "result": [{"ID": 1, "CALL": "x", "OTHER": "ok"}]
                                }
                            }
                        }
                    },
                    "planner_result": {"expected_result_action": "save_to_wf_vars"},
                },
                "diagnostics": {
                    "result_value": [{"OTHER": "ok"}],
                    "workflow_state": {
                        "vars": {"RESTbody": {"result": [{"OTHER": "ok"}]}},
                        "initVariables": [],
                    },
                },
            }
        )
        self.assertEqual(payload["expected_result_action"], "save_to_wf_vars")
        self.assertIn("wf", payload["after_state"])
        self.assertTrue(payload["available_runtime_evidence"]["after_state"])

    def test_node_bridges_result_to_aggregate_verification(self) -> None:
        llm = StubLLM(
            response={
                "passed": True,
                "summary": "Runtime/state check passed.",
                "severity": "low",
                "fixer_brief": {"patch_scope": "none"},
                "confidence": 0.92,
            }
        )
        node = create_runtime_state_verifier_node(llm)
        result = asyncio.run(
            node(
                {
                    "generated_code": "return wf.vars.users",
                    "compiled_request": {
                        "verification_prompt": "Return users.",
                        "selected_primary_path": "wf.vars.users",
                    },
                }
            )
        )
        self.assertTrue(result["verification_passed"])
        self.assertEqual(result["verification"]["verifier_name"], "RuntimeStateVerifier")
        self.assertEqual(result["failure_stage"], "")


class TestVerificationChainRegistry(unittest.TestCase):
    def test_registry_contains_first_four_verifiers_in_order(self) -> None:
        specs = get_verification_chain_specs()
        self.assertEqual(specs[0]["verifier_name"], "ContractVerifier")
        self.assertEqual(specs[1]["verifier_name"], "ShapeTypeVerifier")
        self.assertEqual(specs[2]["verifier_name"], "SemanticLogicVerifier")
        self.assertEqual(specs[3]["verifier_name"], "RuntimeStateVerifier")
        self.assertEqual(specs[3]["default_max_fix_iterations"], 1)

    def test_registry_builds_nodes(self) -> None:
        nodes = create_verification_chain_nodes(StubLLM())
        self.assertIn("verify_contract", nodes)
        self.assertIn("verify_shape_type", nodes)
        self.assertIn("verify_semantic_logic", nodes)
        self.assertIn("verify_runtime_state", nodes)
        self.assertTrue(callable(nodes["verify_runtime_state"]))


if __name__ == "__main__":
    unittest.main()
