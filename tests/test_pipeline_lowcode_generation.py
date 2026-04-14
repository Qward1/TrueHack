import asyncio
import re
import unittest
from pathlib import Path
from unittest.mock import patch

from src.graph.engine import PipelineEngine


ROUTE_SYSTEM_PREFIX = "You are an intent classifier"
EXPLAIN_SYSTEM_PREFIX = "You explain generated Lua code"
FIX_VALIDATION_SYSTEM_PREFIX = "You fix Lua 5.5 workflow scripts that fail during execution."
VERIFIER_AGENT_NAMES = {
    "ContractVerifier",
    "ShapeTypeVerifier",
    "SemanticLogicVerifier",
    "RuntimeStateVerifier",
    "RobustnessVerifier",
}
UNIVERSAL_FIXER_AGENT_NAME = "UniversalVerificationFixer"


def _default_verifier_response(agent_name: str) -> dict:
    return {
        "verifier_name": agent_name,
        "passed": True,
        "error_family": None,
        "error_code": None,
        "severity": "low",
        "summary": f"{agent_name} passed.",
        "field_path": None,
        "evidence": [],
        "expected": {},
        "actual": {},
        "fixer_brief": {
            "goal": "",
            "must_change": [],
            "must_preserve": [],
            "forbidden_fixes": [],
            "suggested_patch": "",
            "patch_scope": "none",
        },
        "confidence": 0.99,
    }


def _extract_wrapped_lua_from_prompt(prompt: str) -> str:
    match = re.search(r"Current broken Lua code:\s*(lua\{.*?\}lua)", prompt, re.DOTALL)
    if match:
        return match.group(1).strip()
    return "lua{return nil}lua"


class StubLLM:
    def __init__(
        self,
        *,
        generate_responses: list[str],
        fix_response: str | list[str],
        route_intent: str = "create",
        json_responses_by_agent: dict[str, object] | None = None,
    ) -> None:
        self._generate_responses = list(generate_responses)
        if isinstance(fix_response, list):
            self._fix_responses = list(fix_response)
        else:
            self._fix_responses = [fix_response]
        self._route_intent = route_intent
        self._json_responses_by_agent: dict[str, list[object]] = {}
        for agent_name, response in (json_responses_by_agent or {}).items():
            if isinstance(response, list):
                self._json_responses_by_agent[agent_name] = list(response)
            else:
                self._json_responses_by_agent[agent_name] = [response]
        self.fix_calls = 0
        self.generate_calls = 0
        self.json_calls_by_agent: dict[str, int] = {}
        self.last_fix_prompt = ""
        self.last_generate_prompt = ""
        self.last_generate_system = ""
        self.last_generate_temperature = None
        self.last_chat_temperature = None
        self.last_fix_system = ""
        self.last_json_prompt_by_agent: dict[str, str] = {}

    def _next_json_response(self, agent_name: str, default: object) -> object:
        count = self.json_calls_by_agent.get(agent_name, 0)
        self.json_calls_by_agent[agent_name] = count + 1
        responses = self._json_responses_by_agent.get(agent_name)
        if responses:
            index = min(count, len(responses) - 1)
            return responses[index]
        return default

    async def generate(
        self,
        prompt: str,
        system: str = "",
        temperature: float = 0.2,
        max_tokens: int | None = None,
        *,
        agent_name: str = "",
    ) -> str:
        if system.startswith(ROUTE_SYSTEM_PREFIX):
            raise AssertionError("route_intent should use generate_json")
        if system.startswith(EXPLAIN_SYSTEM_PREFIX):
            raise AssertionError("explain_solution should use generate_json")
        if agent_name == "CodeValidator":
            return (
                "Error type/message: runtime error\n"
                "Failing line: unknown\n"
                "Root cause: inferred from traceback\n"
                "Why it fails on this context: validation runtime produced this failure\n"
                "Exact repair path: update the failing expression so the runtime error is removed."
            )
        self.generate_calls += 1
        self.last_generate_prompt = prompt
        self.last_generate_system = system
        self.last_generate_temperature = temperature
        if self._generate_responses:
            return self._generate_responses.pop(0)
        raise AssertionError(f"unexpected generate call: {system[:80]}")

    async def generate_json(
        self,
        prompt: str,
        system: str = "",
        temperature: float = 0.0,
        *,
        agent_name: str = "",
    ) -> dict:
        if system.startswith(ROUTE_SYSTEM_PREFIX):
            return {"intent": self._route_intent, "confidence": 1.0}
        if system.startswith(EXPLAIN_SYSTEM_PREFIX):
            return {
                "summary": "Workflow script prepared.",
                "what_is_in_code": ["Uses wf.vars directly."],
                "how_it_works": ["Returns the requested value."],
                "suggested_changes": [],
                "clarifying_questions": [],
            }
        self.last_json_prompt_by_agent[agent_name] = prompt
        if agent_name in VERIFIER_AGENT_NAMES:
            return self._next_json_response(agent_name, _default_verifier_response(agent_name))
        if agent_name == UNIVERSAL_FIXER_AGENT_NAME:
            return self._next_json_response(
                agent_name,
                {
                    "fixed": True,
                    "changed": False,
                    "applied_error_family": "",
                    "applied_error_code": "",
                    "applied_strategy": "noop",
                    "preserved_constraints": [],
                    "remaining_risks": ["No verification failure was supplied."],
                    "fixed_lua_code": _extract_wrapped_lua_from_prompt(prompt),
                },
            )
        raise AssertionError(f"unexpected generate_json call: {system[:80]}")

    async def chat(
        self,
        messages: list[dict],
        temperature: float = 0.2,
        max_tokens: int | None = None,
        *,
        agent_name: str = "",
    ) -> str:
        system = str(messages[0].get("content", "")) if messages else ""
        self.last_chat_temperature = temperature
        if system.startswith(FIX_VALIDATION_SYSTEM_PREFIX):
            self.fix_calls += 1
            self.last_fix_system = system
            self.last_fix_prompt = str(messages[-1].get("content", "")) if messages else ""
            if self._fix_responses:
                return self._fix_responses.pop(0)
            raise AssertionError("unexpected extra fix call")
        raise AssertionError(f"unexpected chat call: {system[:80]}")


class ExplainStringFieldsStubLLM(StubLLM):
    async def generate_json(
        self,
        prompt: str,
        system: str = "",
        temperature: float = 0.0,
        *,
        agent_name: str = "",
    ) -> dict:
        if system.startswith(ROUTE_SYSTEM_PREFIX):
            return {"intent": self._route_intent, "confidence": 1.0}
        if system.startswith(EXPLAIN_SYSTEM_PREFIX):
            return {
                "summary": "Скрипт подготовлен.",
                "what_is_in_code": "Читает массив из wf.vars.cart.items и возвращает количество элементов.",
                "how_it_works": "1. Берет массив cart.items.\n2. Считает количество элементов.\n3. Возвращает число.",
                "suggested_changes": [],
                "clarifying_questions": [],
            }
        return await super().generate_json(
            prompt,
            system=system,
            temperature=temperature,
            agent_name=agent_name,
        )


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
        "runtime_context": {},
        "result_value": None,
        "result_preview": "",
        "workflow_state": None,
        "workflow_state_preview": "",
        "luacheck_output": "",
        "luacheck_error": "",
        "luacheck_warning": "",
        "failure_kind": "",
    }


class PipelineLowcodeGenerationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp_path = Path("tests_tmp_pipeline")
        self.tmp_path.mkdir(exist_ok=True)

    def tearDown(self) -> None:
        if self.tmp_path.exists():
            for child in self.tmp_path.iterdir():
                child.unlink()
            self.tmp_path.rmdir()

    def test_simple_count_prompt_uses_model_generation_and_saves(self) -> None:
        async def fake_run_diagnostics(code: str, lua_bin: str = "lua55", startup_timeout: float = 3.0) -> dict:
            return _success_diagnostics()

        def fake_save_output(target_path: str, code: str, jsonstring_code: str = "") -> dict:
            return {
                "lua_path": target_path,
                "jsonstring_path": f"{target_path}.jsonstring.txt",
            }

        llm = StubLLM(
            generate_responses=["lua{return #wf.vars.cart.items}lua"],
            fix_response="",
        )
        target_path = self.tmp_path / "sample.lua"
        prompt = """Посчитай количество товаров в корзине.

{
  "wf": {
    "vars": {
      "cart": {
        "items": [
          { "sku": "A001" },
          { "sku": "A002" },
          { "sku": "A003" }
        ]
      }
    }
  }
}"""

        with patch("src.graph.nodes.async_run_diagnostics", new=fake_run_diagnostics), patch(
            "src.graph.nodes.save_final_output",
            new=fake_save_output,
        ):
            engine = PipelineEngine(llm=llm)
            result = asyncio.run(
                engine.process_message(
                    chat_id=1,
                    user_input=prompt,
                    workspace_root=str(self.tmp_path),
                    target_path=str(target_path),
                )
            )

        self.assertTrue(result["save_success"])
        self.assertEqual(result["generated_code"].strip(), "return #wf.vars.cart.items")
        self.assertTrue(result["verification_passed"])
        self.assertTrue(result["verification"]["passed"])
        self.assertEqual(result["verification"]["verifier_name"], "RobustnessVerifier")
        self.assertEqual(llm.fix_calls, 0)
        self.assertEqual(llm.generate_calls, 1)
        self.assertNotIn("shortest correct script", llm.last_generate_system)

    def test_explainer_string_fields_do_not_fall_back_to_generic_sections(self) -> None:
        async def fake_run_diagnostics(code: str, lua_bin: str = "lua55", startup_timeout: float = 3.0) -> dict:
            return _success_diagnostics()

        llm = ExplainStringFieldsStubLLM(
            generate_responses=["lua{return #wf.vars.cart.items}lua"],
            fix_response="",
        )
        prompt = """Посчитай количество товаров в корзине.

{
  "wf": {
    "vars": {
      "cart": {
        "items": [
          { "sku": "A001" },
          { "sku": "A002" }
        ]
      }
    }
  }
}"""

        with patch("src.graph.nodes.async_run_diagnostics", new=fake_run_diagnostics):
            engine = PipelineEngine(llm=llm)
            result = asyncio.run(
                engine.process_message(
                    chat_id=1,
                    user_input=prompt,
                    workspace_root=str(self.tmp_path),
                    target_path="",
                )
            )

        self.assertIn("Что есть в коде", result["response"])
        self.assertIn("Читает массив из wf.vars.cart.items", result["response"])
        self.assertIn("Как это работает", result["response"])
        self.assertIn("Берет массив cart.items", result["response"])
        self.assertNotIn("Основная логика задачи реализована", result["response"])

    def test_last_email_prompt_uses_model_generation_and_saves(self) -> None:
        async def fake_run_diagnostics(code: str, lua_bin: str = "lua55", startup_timeout: float = 3.0) -> dict:
            return _success_diagnostics()

        saved_payloads: list[str] = []

        def fake_save_output(target_path: str, code: str, jsonstring_code: str = "") -> dict:
            saved_payloads.append(jsonstring_code)
            return {
                "lua_path": target_path,
                "jsonstring_path": f"{target_path}.jsonstring.txt",
            }

        llm = StubLLM(
            generate_responses=["lua{return wf.vars.emails[#wf.vars.emails]}lua"],
            fix_response="",
        )
        target_path = self.tmp_path / "sample.lua"
        prompt = """Return the last email from the provided workflow context.

{
  "wf": {
    "vars": {
      "emails": ["user1@example.com", "user2@example.com", "user3@example.com"]
    }
  }
}"""

        with patch("src.graph.nodes.async_run_diagnostics", new=fake_run_diagnostics), patch(
            "src.graph.nodes.save_final_output",
            new=fake_save_output,
        ):
            engine = PipelineEngine(llm=llm)
            result = asyncio.run(
                engine.process_message(
                    chat_id=1,
                    user_input=prompt,
                    workspace_root=str(self.tmp_path),
                    target_path=str(target_path),
                )
            )

        self.assertTrue(result["save_success"])
        self.assertEqual(result["generated_code"].strip(), "return wf.vars.emails[#wf.vars.emails]")
        self.assertTrue(result["verification_passed"])
        self.assertTrue(result["verification"]["passed"])
        self.assertEqual(result["verification"]["verifier_name"], "RobustnessVerifier")
        self.assertEqual(llm.generate_calls, 1)
        self.assertIn('"emails"', result["response"])
        self.assertIn('lua{\\r\\nreturn wf.vars.emails[#wf.vars.emails]\\r\\n}lua', result["response"])
        self.assertEqual(len(saved_payloads), 1)
        self.assertIn('"emails"', saved_payloads[0])
        self.assertIn('lua{\\r\\nreturn wf.vars.emails[#wf.vars.emails]\\r\\n}lua', saved_payloads[0])

    def test_active_verification_pipeline_runs_fixer_and_continues_with_patched_code(self) -> None:
        async def fake_run_diagnostics(code: str, lua_bin: str = "lua55", startup_timeout: float = 3.0) -> dict:
            diagnostics = _success_diagnostics()
            diagnostics["workflow_state"] = {
                "wf": {
                    "initVariables": {"recallTime": "2026-04-13T10:20:30"},
                    "vars": {"time": "wrong"},
                }
            }
            return diagnostics

        def fake_save_output(target_path: str, code: str, jsonstring_code: str = "") -> dict:
            return {
                "lua_path": target_path,
                "jsonstring_path": f"{target_path}.jsonstring.txt",
            }

        llm = StubLLM(
            generate_responses=["lua{return wf.vars.time}lua"],
            fix_response="",
            json_responses_by_agent={
                "ContractVerifier": {
                    "passed": False,
                    "error_family": "workflow_path",
                    "error_code": "wrong_read_path",
                    "severity": "high",
                    "summary": "Reads wf.vars.time instead of wf.initVariables.recallTime.",
                    "field_path": "wf.vars.time",
                    "evidence": ["Code reads wf.vars.time."],
                    "expected": {"read_path": "wf.initVariables.recallTime"},
                    "actual": {"read_path": "wf.vars.time"},
                    "fixer_brief": {
                        "goal": "Use the correct workflow path.",
                        "must_change": ["Replace wf.vars.time with wf.initVariables.recallTime."],
                        "must_preserve": ["Keep the direct return behavior."],
                        "forbidden_fixes": ["Do not rewrite the whole script."],
                        "suggested_patch": "Return wf.initVariables.recallTime.",
                        "patch_scope": "local",
                    },
                    "confidence": 0.98,
                },
                "UniversalVerificationFixer": {
                    "fixed": True,
                    "changed": True,
                    "applied_error_family": "workflow_path",
                    "applied_error_code": "wrong_read_path",
                    "applied_strategy": "replace_wrong_path",
                    "preserved_constraints": ["Keep the direct return behavior."],
                    "remaining_risks": [],
                    "fixed_lua_body": "return wf.initVariables.recallTime",
                },
            },
        )
        target_path = self.tmp_path / "recall_time.lua"
        prompt = """Return the recall time from workflow initVariables.

{
  "wf": {
    "initVariables": {
      "recallTime": "2026-04-13T10:20:30"
    },
    "vars": {
      "time": "wrong"
    }
  }
}"""

        with patch("src.graph.nodes.async_run_diagnostics", new=fake_run_diagnostics), patch(
            "src.graph.nodes.save_final_output",
            new=fake_save_output,
        ):
            engine = PipelineEngine(llm=llm)
            result = asyncio.run(
                engine.process_message(
                    chat_id=1,
                    user_input=prompt,
                    workspace_root=str(self.tmp_path),
                    target_path=str(target_path),
                )
            )

        self.assertTrue(result["save_success"])
        self.assertTrue(result["verification_passed"])
        self.assertEqual(result["generated_code"].strip(), "return wf.initVariables.recallTime")
        self.assertEqual(result["verification"]["verifier_name"], "RobustnessVerifier")
        self.assertEqual(result["verification_chain_last_transition"], "verifier_pass")
        self.assertEqual(llm.json_calls_by_agent["ContractVerifier"], 1)
        self.assertEqual(llm.json_calls_by_agent["UniversalVerificationFixer"], 1)
        self.assertTrue(
            result["verification_chain_stage_results"]["ContractVerifier"]["resolved_by_fixer"]
        )
        self.assertIn(
            "return wf.initVariables.recallTime",
            llm.last_json_prompt_by_agent["SemanticLogicVerifier"],
        )
        history_entry_types = [entry.get("entry_type") for entry in result["verification_chain_history"]]
        self.assertIn("fixer", history_entry_types)

    def test_active_verification_pipeline_keeps_blocking_failure_when_fixer_cannot_patch(self) -> None:
        async def fake_run_diagnostics(code: str, lua_bin: str = "lua55", startup_timeout: float = 3.0) -> dict:
            diagnostics = _success_diagnostics()
            diagnostics["workflow_state"] = {
                "wf": {
                    "initVariables": {"recallTime": "2026-04-13T10:20:30"},
                    "vars": {"time": "wrong"},
                }
            }
            return diagnostics

        save_calls: list[str] = []

        def fake_save_output(target_path: str, code: str, jsonstring_code: str = "") -> dict:
            save_calls.append(code)
            return {
                "lua_path": target_path,
                "jsonstring_path": f"{target_path}.jsonstring.txt",
            }

        llm = StubLLM(
            generate_responses=["lua{return wf.vars.time}lua"],
            fix_response="",
            json_responses_by_agent={
                "ContractVerifier": {
                    "passed": False,
                    "error_family": "workflow_path",
                    "error_code": "wrong_read_path",
                    "severity": "high",
                    "summary": "Reads wf.vars.time instead of wf.initVariables.recallTime.",
                    "field_path": "wf.vars.time",
                    "evidence": ["Code reads wf.vars.time."],
                    "expected": {"read_path": "wf.initVariables.recallTime"},
                    "actual": {"read_path": "wf.vars.time"},
                    "fixer_brief": {
                        "goal": "Use the correct workflow path.",
                        "must_change": ["Replace wf.vars.time with wf.initVariables.recallTime."],
                        "must_preserve": ["Keep the direct return behavior."],
                        "forbidden_fixes": ["Do not rewrite the whole script."],
                        "suggested_patch": "Return wf.initVariables.recallTime.",
                        "patch_scope": "local",
                    },
                    "confidence": 0.98,
                },
                "UniversalVerificationFixer": {
                    "fixed": False,
                    "changed": False,
                    "applied_error_family": "workflow_path",
                    "applied_error_code": "wrong_read_path",
                    "applied_strategy": "no_valid_patch",
                    "preserved_constraints": ["Keep the direct return behavior."],
                    "remaining_risks": ["Could not derive a valid patch."],
                    "fixed_lua_code": "lua{return wf.vars.time}lua",
                },
            },
        )
        target_path = self.tmp_path / "recall_time.lua"
        prompt = """Return the recall time from workflow initVariables.

{
  "wf": {
    "initVariables": {
      "recallTime": "2026-04-13T10:20:30"
    },
    "vars": {
      "time": "wrong"
    }
  }
}"""

        with patch("src.graph.nodes.async_run_diagnostics", new=fake_run_diagnostics), patch(
            "src.graph.nodes.save_final_output",
            new=fake_save_output,
        ):
            engine = PipelineEngine(llm=llm)
            result = asyncio.run(
                engine.process_message(
                    chat_id=1,
                    user_input=prompt,
                    workspace_root=str(self.tmp_path),
                    target_path=str(target_path),
                )
            )

        self.assertFalse(result["save_success"])
        self.assertTrue(result["save_skipped"])
        self.assertEqual(save_calls, [])
        self.assertFalse(result["verification_passed"])
        self.assertEqual(result["failure_stage"], "contract_verification")
        self.assertEqual(result["verification"]["verifier_name"], "ContractVerifier")
        self.assertEqual(result["verification"]["error_code"], "wrong_read_path")
        self.assertEqual(llm.json_calls_by_agent["UniversalVerificationFixer"], 2)
        self.assertFalse(
            result["verification_chain_stage_results"]["ContractVerifier"]["resolved_by_fixer"]
        )

    def test_generation_prompt_allows_multi_step_workflow_script(self) -> None:
        async def fake_run_diagnostics(code: str, lua_bin: str = "lua55", startup_timeout: float = 3.0) -> dict:
            return _success_diagnostics()

        def fake_save_output(target_path: str, code: str, jsonstring_code: str = "") -> dict:
            return {
                "lua_path": target_path,
                "jsonstring_path": f"{target_path}.jsonstring.txt",
            }

        llm = StubLLM(
            generate_responses=[
                """lua{
local contacts = wf.vars.contacts
if type(contacts) ~= "table" then
    local arr = _utils.array.new()
    arr[1] = contacts
    _utils.array.markAsArray(arr)
    return arr
end
local isArray = true
for key in pairs(contacts) do
    if type(key) ~= "number" or math.floor(key) ~= key then
        isArray = false
        break
    end
end
if isArray then
    return contacts
end
local arr = _utils.array.new()
arr[1] = contacts
_utils.array.markAsArray(arr)
return arr
}lua"""
            ],
            fix_response="",
        )
        target_path = self.tmp_path / "contacts.lua"
        prompt = """Приведи wf.vars.contacts к массиву. Если там уже массив — верни как есть, иначе оберни значение в массив.

{
  "wf": {
    "vars": {
      "contacts": {
        "name": "Иван"
      }
    }
  }
}"""

        with patch("src.graph.nodes.async_run_diagnostics", new=fake_run_diagnostics), patch(
            "src.graph.nodes.save_final_output",
            new=fake_save_output,
        ):
            engine = PipelineEngine(llm=llm)
            result = asyncio.run(
                engine.process_message(
                    chat_id=1,
                    user_input=prompt,
                    workspace_root=str(self.tmp_path),
                    target_path=str(target_path),
                )
            )

        self.assertTrue(result["save_success"])
        self.assertEqual(llm.generate_calls, 1)
        self.assertEqual(llm.fix_calls, 0)
        self.assertIn('if type(contacts) ~= "table" then', result["generated_code"])
        self.assertIn("for key in pairs(contacts) do", result["generated_code"])
        self.assertIn("Do not force a non-trivial workflow transformation into a one-line `return`.", llm.last_generate_prompt)
        self.assertNotIn("reason privately", llm.last_generate_prompt)
        self.assertNotIn("ranked candidates", llm.last_generate_prompt)
        self.assertNotIn("confidence:", llm.last_generate_prompt)
        self.assertIn("Use workflow path: wf.vars.contacts", llm.last_generate_prompt)
        self.assertIn("Treat an empty table as an array.", llm.last_generate_prompt)
        self.assertIn("create it with `_utils.array.new()`, assign items explicitly", llm.last_generate_prompt)
        self.assertIn(
            "The response MUST start with the literal three characters `lua{` and end with the literal four characters `}lua`.",
            llm.last_generate_system,
        )
        self.assertNotIn("JsonString", llm.last_generate_system)
        self.assertNotIn("return wf.vars.emails[#wf.vars.emails]", llm.last_generate_prompt)
        self.assertEqual(llm.last_generate_temperature, 0.0)

    def test_generation_prompt_guides_numeric_aggregation_and_uses_low_temperature(self) -> None:
        async def fake_run_diagnostics(code: str, lua_bin: str = "lua55", startup_timeout: float = 3.0) -> dict:
            return _success_diagnostics()

        def fake_save_output(target_path: str, code: str, jsonstring_code: str = "") -> dict:
            return {
                "lua_path": target_path,
                "jsonstring_path": f"{target_path}.jsonstring.txt",
            }

        llm = StubLLM(
            generate_responses=[
                """lua{
local total = 0
for _, item in ipairs(wf.vars.items) do
 total = total + (tonumber(item.quantity) or 0)
end
return total
}lua"""
            ],
            fix_response="",
        )
        target_path = self.tmp_path / "items.lua"
        prompt = """Посчитай сумму всех значений quantity в массиве items.

{
  "wf": {
    "vars": {
      "items": [
        { "sku": "A1", "quantity": "2" },
        { "sku": "A2", "quantity": "5" },
        { "sku": "A3", "quantity": "3" }
      ]
    }
  }
}"""

        with patch("src.graph.nodes.async_run_diagnostics", new=fake_run_diagnostics), patch(
            "src.graph.nodes.save_final_output",
            new=fake_save_output,
        ):
            engine = PipelineEngine(llm=llm)
            result = asyncio.run(
                engine.process_message(
                    chat_id=1,
                    user_input=prompt,
                    workspace_root=str(self.tmp_path),
                    target_path=str(target_path),
                )
            )

        self.assertTrue(result["save_success"])
        self.assertIn("numeric aggregation over workflow arrays", llm.last_generate_prompt)
        self.assertEqual(llm.last_generate_temperature, 0.0)
        self.assertIn("(tonumber(item.quantity) or 0)", result["generated_code"])

    def test_generation_normalizes_json_envelope_with_lua_field(self) -> None:
        async def fake_run_diagnostics(code: str, lua_bin: str = "lua55", startup_timeout: float = 3.0) -> dict:
            self.assertEqual(
                code.strip(),
                "return _utils.array.find(wf.vars.orders, function(order) return order.status == 'NEW' end).id",
            )
            return _success_diagnostics()

        def fake_save_output(target_path: str, code: str, jsonstring_code: str = "") -> dict:
            return {
                "lua_path": target_path,
                "jsonstring_path": f"{target_path}.jsonstring.txt",
            }

        llm = StubLLM(
            generate_responses=[
                """lua{
json
{
  "lua": "return _utils.array.find(wf.vars.orders, function(order) return order.status == 'NEW' end).id"
}
}lua"""
            ],
            fix_response="",
        )
        target_path = self.tmp_path / "orders.lua"
        prompt = """Найди id первого заказа со статусом NEW.

{
  "wf": {
    "vars": {
      "orders": [
        { "id": 1, "status": "OLD" },
        { "id": 2, "status": "NEW" }
      ]
    }
  }
}"""

        with patch("src.graph.nodes.async_run_diagnostics", new=fake_run_diagnostics), patch(
            "src.graph.nodes.save_final_output",
            new=fake_save_output,
        ):
            engine = PipelineEngine(llm=llm)
            result = asyncio.run(
                engine.process_message(
                    chat_id=1,
                    user_input=prompt,
                    workspace_root=str(self.tmp_path),
                    target_path=str(target_path),
                )
            )

        self.assertTrue(result["save_success"])
        self.assertEqual(
            result["generated_code"].strip(),
            "return _utils.array.find(wf.vars.orders, function(order) return order.status == 'NEW' end).id",
        )
        self.assertEqual(llm.fix_calls, 0)

    def test_failed_validation_still_returns_code_payload(self) -> None:
        async def failing_run_diagnostics(code: str, lua_bin: str = "lua55", startup_timeout: float = 3.0) -> dict:
            diagnostics = _success_diagnostics()
            diagnostics["success"] = False
            diagnostics["run_error"] = "syntax error near '}'"
            diagnostics["runtime_fix_hints"] = ["Remove wrapper noise and return valid standalone Lua."]
            diagnostics["failure_kind"] = "syntax"
            return diagnostics

        def fail_if_save_called(target_path: str, code: str, jsonstring_code: str = "") -> dict:
            raise AssertionError("save_final_output must not be called when validation never passes")

        llm = StubLLM(
            generate_responses=["lua{return wf.vars.emails[#wf.vars.emails]}lua"],
            fix_response=["lua{return wf.vars.emails[#wf.vars.emails]}lua"] * 10,
        )
        target_path = self.tmp_path / "sample.lua"
        previous_code = "return wf.vars.previous"
        prompt = """Верни последний email.

{
  "wf": {
    "vars": {
      "emails": ["user1@example.com", "user2@example.com"]
    }
  }
}"""

        with patch("src.graph.nodes.async_run_diagnostics", new=failing_run_diagnostics), patch(
            "src.graph.nodes.save_final_output",
            new=fail_if_save_called,
        ):
            engine = PipelineEngine(llm=llm)
            result = asyncio.run(
                engine.process_message(
                    chat_id=1,
                    user_input=prompt,
                    workspace_root=str(self.tmp_path),
                    target_path=str(target_path),
                    current_code=previous_code,
                )
            )

        self.assertFalse(result["validation_passed"])
        self.assertFalse(result["save_success"])
        self.assertEqual(result["response_type"], "code")
        self.assertIn("syntax error near", result["response"])
        self.assertIn("```json", result["response"])
        self.assertEqual(result["current_code"], "return wf.vars.emails[#wf.vars.emails]")

    def test_without_explicit_path_returns_code_but_skips_save(self) -> None:
        async def fake_run_diagnostics(code: str, lua_bin: str = "lua55", startup_timeout: float = 3.0) -> dict:
            return _success_diagnostics()

        def fail_if_save_called(target_path: str, code: str, jsonstring_code: str = "") -> dict:
            raise AssertionError("save_final_output must not be called when no explicit path is set")

        llm = StubLLM(
            generate_responses=["lua{return wf.vars.emails[#wf.vars.emails]}lua"],
            fix_response="",
        )
        prompt = """Return the last email from the provided workflow context.

{
  "wf": {
    "vars": {
      "emails": ["user1@example.com", "user2@example.com", "user3@example.com"]
    }
  }
}"""

        with patch("src.graph.nodes.async_run_diagnostics", new=fake_run_diagnostics), patch(
            "src.graph.nodes.save_final_output",
            new=fail_if_save_called,
        ):
            engine = PipelineEngine(llm=llm)
            result = asyncio.run(
                engine.process_message(
                    chat_id=1,
                    user_input=prompt,
                    workspace_root=str(self.tmp_path),
                    target_path="",
                )
            )

        self.assertFalse(result["save_success"])
        self.assertTrue(result["save_skipped"])
        self.assertEqual(result["target_path"], "")
        self.assertEqual(result["saved_to"], "")
        self.assertEqual(result["saved_jsonstring_to"], "")
        self.assertEqual(result["generated_code"].strip(), "return wf.vars.emails[#wf.vars.emails]")
        self.assertIn("не сохранен в файл", result["response"])
        self.assertEqual(llm.generate_calls, 1)

    def test_change_like_prompt_without_existing_code_is_reclassified_to_create(self) -> None:
        async def fake_run_diagnostics(code: str, lua_bin: str = "lua55", startup_timeout: float = 3.0) -> dict:
            return _success_diagnostics()

        def fake_save_output(target_path: str, code: str, jsonstring_code: str = "") -> dict:
            return {
                "lua_path": target_path,
                "jsonstring_path": f"{target_path}.jsonstring.txt",
            }

        llm = StubLLM(
            generate_responses=["lua{return wf.vars.emails[#wf.vars.emails]}lua"],
            fix_response="",
            route_intent="change",
        )
        target_path = self.tmp_path / "sample.lua"
        prompt = """Улучши обработку email и верни последний email из workflow context.

{
  "wf": {
    "vars": {
      "emails": ["user1@example.com", "user2@example.com", "user3@example.com"]
    }
  }
}"""

        with patch("src.graph.nodes.async_run_diagnostics", new=fake_run_diagnostics), patch(
            "src.graph.nodes.save_final_output",
            new=fake_save_output,
        ):
            engine = PipelineEngine(llm=llm)
            result = asyncio.run(
                engine.process_message(
                    chat_id=1,
                    user_input=prompt,
                    workspace_root=str(self.tmp_path),
                    target_path=str(target_path),
                    current_code="",
                    base_prompt="",
                    change_requests=[],
                )
            )

        self.assertEqual(result["intent"], "create")
        self.assertTrue(result["save_success"])
        self.assertEqual(result["change_requests"], [])
        self.assertEqual(result["generated_code"].strip(), "return wf.vars.emails[#wf.vars.emails]")
        self.assertEqual(llm.generate_calls, 1)

    def test_pasted_message_code_allows_change_without_chat_code(self) -> None:
        async def fake_run_diagnostics(code: str, lua_bin: str = "lua55", startup_timeout: float = 3.0) -> dict:
            return _success_diagnostics()

        def fake_save_output(target_path: str, code: str, jsonstring_code: str = "") -> dict:
            return {
                "lua_path": target_path,
                "jsonstring_path": f"{target_path}.jsonstring.txt",
            }

        llm = StubLLM(
            generate_responses=[
                """lua{
local emails = wf.vars.emails
return emails[#emails]
}lua"""
            ],
            fix_response="",
            route_intent="change",
        )
        target_path = self.tmp_path / "sample.lua"
        prompt = """Исправь этот код так, чтобы он работал с workflow path.

lua{
local emails = {"user1@example.com", "user2@example.com"}
return emails[#emails]
}lua"""

        with patch("src.graph.nodes.async_run_diagnostics", new=fake_run_diagnostics), patch(
            "src.graph.nodes.save_final_output",
            new=fake_save_output,
        ):
            engine = PipelineEngine(llm=llm)
            result = asyncio.run(
                engine.process_message(
                    chat_id=1,
                    user_input=prompt,
                    workspace_root=str(self.tmp_path),
                    target_path=str(target_path),
                    current_code="",
                    base_prompt="",
                    change_requests=[],
                )
        )

        self.assertEqual(result["intent"], "change")
        self.assertEqual(result["base_prompt"].strip(), "Исправь этот код так, чтобы он работал с workflow path.")
        self.assertEqual(result["change_requests"], [prompt])
        self.assertTrue(result["save_success"])
        self.assertEqual(result["generated_code"].strip(), "local emails = wf.vars.emails\nreturn emails[#emails]")
        self.assertEqual(llm.generate_calls, 1)
        self.assertTrue(llm.last_generate_system.startswith("You modify existing Lua workflow scripts"))

    def test_lua_question_without_code_stays_question(self) -> None:
        llm = StubLLM(
            generate_responses=["В Lua цикл `for` используется для повторения действий."],
            fix_response="",
            route_intent="create",
        )

        engine = PipelineEngine(llm=llm)
        result = asyncio.run(
            engine.process_message(
                chat_id=1,
                user_input="Как в Lua работает цикл for?",
                workspace_root=str(self.tmp_path),
                target_path="",
                current_code="",
                base_prompt="",
                change_requests=[],
            )
        )

        self.assertEqual(result["intent"], "question")
        self.assertEqual(result["response_type"], "text")
        self.assertFalse(result["save_success"])
        self.assertEqual(result["generated_code"], "")
        self.assertEqual(result["response"], "В Lua цикл `for` используется для повторения действий.")
        self.assertEqual(llm.generate_calls, 1)

    def test_runtime_bad_argument_error_adds_general_fix_hints(self) -> None:
        async def fake_run_diagnostics(code: str, lua_bin: str = "lua55", startup_timeout: float = 3.0) -> dict:
            if "os.time(recallTime)" in code:
                return {
                    "success": False,
                    "started_ok": True,
                    "timed_out": False,
                    "program_mode": "workflow",
                    "validation_context": "test",
                    "mocked_init_variables": ["recallTime"],
                    "mocked_var_paths": [],
                    "contract_blockers": [],
                    "contract_warnings": [],
                    "run_output": "",
                    "run_error": "bad argument #1 to 'time' (table expected, got string)",
                    "run_warning": "",
                    "runtime_fix_hints": [
                        "Function `time` expects argument #1 of type `table`, but the code passes `string`.",
                        "Before calling `time`, validate or convert the workflow value to the expected `table` type instead of passing it through unchanged.",
                    ],
                    "luacheck_output": "",
                    "luacheck_error": "",
                    "luacheck_warning": "",
                    "failure_kind": "runtime",
                }
            return _success_diagnostics()

        def fake_save_output(target_path: str, code: str, jsonstring_code: str = "") -> dict:
            return {
                "lua_path": target_path,
                "jsonstring_path": f"{target_path}.jsonstring.txt",
            }

        llm = StubLLM(
            generate_responses=["lua{local recallTime = wf.initVariables.recallTime\nlocal unixTime = os.time(os.date(\"*t\", os.time(recallTime)))\nreturn unixTime}lua"],
            fix_response="""lua{
local iso_time = wf.initVariables.recallTime
if not iso_time then
    return nil
end
local year, month, day, hour, minute, second, sign, offset_hour, offset_minute =
    iso_time:match("(%d+)%-(%d+)%-(%d+)T(%d+):(%d+):(%d+)([+-])(%d+):(%d+)")
if not year then
    return nil
end
local epoch = os.time({
    year = tonumber(year),
    month = tonumber(month),
    day = tonumber(day),
    hour = tonumber(hour),
    min = tonumber(minute),
    sec = tonumber(second),
})
local offset = (tonumber(offset_hour) * 3600) + (tonumber(offset_minute) * 60)
if sign == "+" then
    epoch = epoch - offset
else
    epoch = epoch + offset
end
return epoch
}lua""",
        )
        target_path = self.tmp_path / "sample.lua"
        prompt = """Конвертируй время в переменной recallTime в unix-формат.

{
  "wf": {
    "initVariables": {
      "recallTime": "2023-10-15T15:30:00+00:00"
    }
  }
}"""

        with patch("src.graph.nodes.async_run_diagnostics", new=fake_run_diagnostics), patch(
            "src.graph.nodes.save_final_output",
            new=fake_save_output,
        ):
            engine = PipelineEngine(llm=llm)
            result = asyncio.run(
                engine.process_message(
                    chat_id=1,
                    user_input=prompt,
                    workspace_root=str(self.tmp_path),
                    target_path=str(target_path),
                )
            )

        self.assertEqual(llm.fix_calls, 1)
        self.assertTrue(result["save_success"])
        self.assertIn("Runtime error:", llm.last_fix_prompt)
        self.assertIn("Current code with line numbers:", llm.last_fix_prompt)
        self.assertNotIn("Task:\n", llm.last_fix_prompt)
        self.assertNotIn("Original workflow context:\n", llm.last_fix_prompt)
        self.assertNotIn("Workflow anchor:\n", llm.last_fix_prompt)
        self.assertNotIn("Planner analysis:\n", llm.last_fix_prompt)
        self.assertIn("wf.initVariables.recallTime", result["generated_code"])

    def test_runtime_arithmetic_nil_error_retries_with_numbered_code_prompt(self) -> None:
        seen_workflow_contexts: list[dict | None] = []

        async def fake_run_diagnostics(
            code: str,
            lua_bin: str = "lua55",
            startup_timeout: float = 3.0,
            workflow_context: dict | None = None,
        ) -> dict:
            seen_workflow_contexts.append(workflow_context)
            if "timezone_text" in code:
                return _success_diagnostics()
            return {
                "success": False,
                "started_ok": True,
                "timed_out": False,
                "program_mode": "workflow",
                "validation_context": "test",
                "mocked_init_variables": ["recallTime"],
                "mocked_var_paths": [],
                "contract_blockers": [],
                "contract_warnings": [],
                "run_output": "",
                "run_error": "C:\\Users\\Admin\\AppData\\Local\\Temp\\tmp.lua:15: attempt to perform arithmetic on a nil value",
                "run_warning": "",
                "runtime_fix_hints": [
                    "Arithmetic is applied to `nil`. Convert inputs with `tonumber(...)` or guard nil/non-numeric values before the operation.",
                ],
                "runtime_context": {
                    "line": 15,
                    "locals": [
                        {"name": "offsetSign", "type": "string", "value": "-"},
                        {"name": "offsetHours", "type": "nil", "value": "nil"},
                    ],
                },
                "luacheck_output": "",
                "luacheck_error": "",
                "luacheck_warning": "",
                "failure_kind": "runtime",
            }

        def fake_save_output(target_path: str, code: str, jsonstring_code: str = "") -> dict:
            return {
                "lua_path": target_path,
                "jsonstring_path": f"{target_path}.jsonstring.txt",
            }

        repeated_broken = """lua{
local isoDate = wf.initVariables.recallTime
local year, month, day, hour, minute, second, offsetHours, offsetMinutes
year, month, day, hour, minute, second, offsetSign, offsetHours, offsetMinutes = string.match(isoDate, "(%d+)-(%d+)-(%d+)T(%d+):(%d+):(%d+)([+-])(%d+):(%d+)")
local epoch = os.time({year=tonumber(year), month=tonumber(month), day=tonumber(day), hour=tonumber(hour), min=tonumber(minute), sec=tonumber(second)})
epoch = epoch + (offsetHours * 3600) + (offsetMinutes * 60)
return epoch
}lua"""
        fixed_code = """lua{
local isoDate = wf.initVariables.recallTime
local baseText, timezone_text = isoDate:match("^(%d+%-%d+%-%d+T%d+:%d+:%d+)(Z|[+-]%d+:%d+)$")
if not baseText or not timezone_text then
    return nil
end
local year, month, day, hour, minute, second = baseText:match("^(%d+)%-(%d+)%-(%d+)T(%d+):(%d+):(%d+)$")
local epoch = os.time({year=tonumber(year), month=tonumber(month), day=tonumber(day), hour=tonumber(hour), min=tonumber(minute), sec=tonumber(second)})
if not epoch then
    return nil
end
if timezone_text ~= "Z" then
    local sign, offsetHour, offsetMinute = timezone_text:match("^([+-])(%d+):(%d+)$")
    local offsetSeconds = (tonumber(offsetHour) * 3600) + (tonumber(offsetMinute) * 60)
    if sign == "+" then
        epoch = epoch - offsetSeconds
    else
        epoch = epoch + offsetSeconds
    end
end
wf.vars.recallTimeEpoch = epoch
return epoch
}lua"""

        llm = StubLLM(
            generate_responses=[repeated_broken],
            fix_response=[repeated_broken, fixed_code],
        )
        target_path = self.tmp_path / "sample.lua"
        prompt = """Конвертируй время в переменной recallTime в unix-формат.

{
  "wf": {
    "initVariables": {
      "recallTime": "2023-10-15T15:30:00+00:00"
    }
  }
}"""

        with patch("src.graph.nodes.async_run_diagnostics", new=fake_run_diagnostics), patch(
            "src.graph.nodes.save_final_output",
            new=fake_save_output,
        ):
            engine = PipelineEngine(llm=llm)
            result = asyncio.run(
                engine.process_message(
                    chat_id=1,
                    user_input=prompt,
                    workspace_root=str(self.tmp_path),
                    target_path=str(target_path),
                )
            )

        self.assertEqual(llm.fix_calls, 2)
        self.assertTrue(result["save_success"])
        self.assertTrue(seen_workflow_contexts)
        self.assertEqual(
            seen_workflow_contexts[0]["wf"]["initVariables"]["recallTime"],
            "2023-10-15T15:30:00+00:00",
        )
        self.assertIn("Current code with line numbers:", llm.last_fix_prompt)
        self.assertIn("  1 | local isoDate = wf.initVariables.recallTime", llm.last_fix_prompt)
        self.assertIn("Likely failing Lua line: 15", llm.last_fix_prompt)
        self.assertIn("Failing code context:", llm.last_fix_prompt)
        self.assertIn("Error analysis:", llm.last_fix_prompt)
        self.assertIn("the previous fix attempt returned unchanged code", llm.last_fix_prompt.lower())
        self.assertIn("timezone_text", result["generated_code"])

    def test_ambiguity_returns_clarification_without_generation(self) -> None:
        async def fake_run_diagnostics(code: str, lua_bin: str = "lua55", startup_timeout: float = 3.0) -> dict:
            return _success_diagnostics()

        def fake_save_output(target_path: str, code: str, jsonstring_code: str = "") -> dict:
            return {
                "lua_path": target_path,
                "jsonstring_path": f"{target_path}.jsonstring.txt",
            }

        llm = StubLLM(generate_responses=["lua{return #wf.vars.cart.items}lua"], fix_response="")
        target_path = self.tmp_path / "sample.lua"
        prompt = """Посчитай количество товаров.

{
  "wf": {
    "vars": {
      "cart": {
        "items": [
          { "sku": "A001" }
        ]
      },
      "wishlist": {
        "items": [
          { "sku": "W001" }
        ]
      }
    }
  }
}"""

        with patch("src.graph.nodes.async_run_diagnostics", new=fake_run_diagnostics), patch(
            "src.graph.nodes.save_final_output",
            new=fake_save_output,
        ):
            engine = PipelineEngine(llm=llm)
            result = asyncio.run(
                engine.process_message(
                    chat_id=1,
                    user_input=prompt,
                    workspace_root=str(self.tmp_path),
                    target_path=str(target_path),
                )
            )

        self.assertEqual(result["response_type"], "text")
        self.assertFalse(result["save_success"])
        self.assertEqual(result["generated_code"], "")
        self.assertEqual(llm.generate_calls, 0)
        self.assertIn("wf.vars.cart.items", result["response"])

    def test_clarification_followup_reuses_original_base_prompt(self) -> None:
        async def fake_run_diagnostics(code: str, lua_bin: str = "lua55", startup_timeout: float = 3.0) -> dict:
            return _success_diagnostics()

        def fake_save_output(target_path: str, code: str, jsonstring_code: str = "") -> dict:
            return {
                "lua_path": target_path,
                "jsonstring_path": f"{target_path}.jsonstring.txt",
            }

        llm = StubLLM(generate_responses=["lua{return #wf.vars.cart.items}lua"], fix_response="")
        target_path = self.tmp_path / "sample.lua"
        original_prompt = """Посчитай количество товаров.

{
  "wf": {
    "vars": {
      "cart": {
        "items": [
          { "sku": "A001" }
        ]
      },
      "wishlist": {
        "items": [
          { "sku": "W001" }
        ]
      }
    }
  }
}"""

        with patch("src.graph.nodes.async_run_diagnostics", new=fake_run_diagnostics), patch(
            "src.graph.nodes.save_final_output",
            new=fake_save_output,
        ):
            engine = PipelineEngine(llm=llm)
            first = asyncio.run(
                engine.process_message(
                    chat_id=1,
                    user_input=original_prompt,
                    workspace_root=str(self.tmp_path),
                    target_path=str(target_path),
                )
            )
            second = asyncio.run(
                engine.process_message(
                    chat_id=1,
                    user_input="используй wf.vars.cart.items",
                    current_code="",
                    base_prompt=first["base_prompt"],
                    change_requests=[],
                    workspace_root=str(self.tmp_path),
                    target_path=str(target_path),
                )
            )

        self.assertEqual(first["response_type"], "text")
        self.assertEqual(first["base_prompt"].strip(), original_prompt.strip())
        self.assertTrue(second["save_success"])
        self.assertEqual(second["generated_code"].strip(), "return #wf.vars.cart.items")
        self.assertEqual(llm.generate_calls, 1)


if __name__ == "__main__":
    unittest.main()
