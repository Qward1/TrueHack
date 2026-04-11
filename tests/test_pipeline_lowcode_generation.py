import asyncio
import unittest
from pathlib import Path
from unittest.mock import patch

from src.graph.engine import PipelineEngine


ROUTE_SYSTEM_PREFIX = "You are an intent classifier"
EXPLAIN_SYSTEM_PREFIX = "You explain generated Lua code"
VERIFY_SYSTEM_PREFIX = "You review whether a Lua solution fully satisfies the user's request."
FIX_SYSTEM_PREFIX = "You fix broken Lua workflow scripts."


class StubLLM:
    def __init__(self, *, generate_responses: list[str], fix_response: str, route_intent: str = "create") -> None:
        self._generate_responses = list(generate_responses)
        self._fix_response = fix_response
        self._route_intent = route_intent
        self.fix_calls = 0
        self.generate_calls = 0
        self.last_fix_prompt = ""

    async def generate(self, prompt: str, system: str = "", temperature: float = 0.2, max_tokens: int | None = None) -> str:
        if system.startswith(ROUTE_SYSTEM_PREFIX):
            raise AssertionError("route_intent should use generate_json")
        if system.startswith(EXPLAIN_SYSTEM_PREFIX):
            raise AssertionError("explain_solution should use generate_json")
        self.generate_calls += 1
        if self._generate_responses:
            return self._generate_responses.pop(0)
        raise AssertionError(f"unexpected generate call: {system[:80]}")

    async def generate_json(self, prompt: str, system: str = "", temperature: float = 0.0) -> dict:
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
        raise AssertionError(f"unexpected generate_json call: {system[:80]}")

    async def chat(self, messages: list[dict], temperature: float = 0.2, max_tokens: int | None = None) -> str:
        system = str(messages[0].get("content", "")) if messages else ""
        if system.startswith(VERIFY_SYSTEM_PREFIX):
            return (
                '{"passed": true, "score": 100, "summary": "LLM verification passed.", '
                '"missing_requirements": [], "warnings": []}'
            )
        if system.startswith(FIX_SYSTEM_PREFIX):
            self.fix_calls += 1
            self.last_fix_prompt = str(messages[-1].get("content", "")) if messages else ""
            return self._fix_response
        raise AssertionError(f"unexpected chat call: {system[:80]}")


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


class PipelineLowcodeGenerationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp_path = Path("tests_tmp_pipeline")
        self.tmp_path.mkdir(exist_ok=True)

    def tearDown(self) -> None:
        if self.tmp_path.exists():
            for child in self.tmp_path.iterdir():
                child.unlink()
            self.tmp_path.rmdir()

    def test_simple_count_prompt_uses_deterministic_fast_path(self) -> None:
        async def fake_run_diagnostics(code: str, lua_bin: str = "lua55", startup_timeout: float = 3.0) -> dict:
            return _success_diagnostics()

        def fake_save_output(target_path: str, code: str, jsonstring_code: str = "") -> dict:
            return {
                "lua_path": target_path,
                "jsonstring_path": f"{target_path}.jsonstring.txt",
            }

        llm = StubLLM(
            generate_responses=[],
            fix_response="lua{return #wf.vars.cart.items}lua",
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
        self.assertTrue(result["verification"]["passed"])
        self.assertEqual(llm.fix_calls, 0)
        self.assertEqual(llm.generate_calls, 0)

    def test_last_email_prompt_uses_deterministic_fast_path(self) -> None:
        async def fake_run_diagnostics(code: str, lua_bin: str = "lua55", startup_timeout: float = 3.0) -> dict:
            return _success_diagnostics()

        def fake_save_output(target_path: str, code: str, jsonstring_code: str = "") -> dict:
            return {
                "lua_path": target_path,
                "jsonstring_path": f"{target_path}.jsonstring.txt",
            }

        llm = StubLLM(
            generate_responses=[],
            fix_response="lua{return wf.vars.emails[#wf.vars.emails]}lua",
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
        self.assertTrue(result["verification"]["passed"])
        self.assertEqual(llm.generate_calls, 0)

    def test_without_explicit_path_returns_code_but_skips_save(self) -> None:
        async def fake_run_diagnostics(code: str, lua_bin: str = "lua55", startup_timeout: float = 3.0) -> dict:
            return _success_diagnostics()

        def fail_if_save_called(target_path: str, code: str, jsonstring_code: str = "") -> dict:
            raise AssertionError("save_final_output must not be called when no explicit path is set")

        llm = StubLLM(
            generate_responses=[],
            fix_response="lua{return wf.vars.emails[#wf.vars.emails]}lua",
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
        self.assertEqual(llm.generate_calls, 0)

    def test_change_intent_without_existing_code_goes_directly_to_generate(self) -> None:
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

        self.assertEqual(result["intent"], "change")
        self.assertTrue(result["save_success"])
        self.assertEqual(result["change_requests"], [])
        self.assertEqual(result["generated_code"].strip(), "return wf.vars.emails[#wf.vars.emails]")
        self.assertEqual(llm.generate_calls, 0)

    def test_sends_complex_app_style_generation_into_fix_loop(self) -> None:
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
local payload = {
  DATUM = "20260410",
  TIME = "123045"
}
print(payload.DATUM .. payload.TIME)
}lua"""
            ],
            fix_response="""lua{
local DATUM = wf.vars.json.IDOC.ZCDF_HEAD.DATUM
local TIME = wf.vars.json.IDOC.ZCDF_HEAD.TIME
return string.format("%s-%s-%sT%s:%s:%s.00000Z", string.sub(DATUM, 1, 4), string.sub(DATUM, 5, 6), string.sub(DATUM, 7, 8), string.sub(TIME, 1, 2), string.sub(TIME, 3, 4), string.sub(TIME, 5, 6))
}lua""",
        )
        target_path = self.tmp_path / "sample.lua"
        prompt = """Преобразуй DATUM/TIME из workflow context в ISO 8601.

{
  "wf": {
    "vars": {
      "json": {
        "IDOC": {
          "ZCDF_HEAD": {
            "DATUM": "20260410",
            "TIME": "123045"
          }
        }
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

        self.assertEqual(llm.fix_calls, 1)
        self.assertTrue(result["save_success"])
        self.assertIn("wf.vars.json.IDOC.ZCDF_HEAD.DATUM", result["generated_code"])
        self.assertEqual(result["verification"]["anti_patterns"], [])

    def test_remove_keys_task_rejects_plain_return_and_enters_fix_loop(self) -> None:
        async def fake_run_diagnostics(code: str, lua_bin: str = "lua55", startup_timeout: float = 3.0) -> dict:
            return _success_diagnostics()

        def fake_save_output(target_path: str, code: str, jsonstring_code: str = "") -> dict:
            return {
                "lua_path": target_path,
                "jsonstring_path": f"{target_path}.jsonstring.txt",
            }

        llm = StubLLM(
            generate_responses=["lua{return wf.vars.RESTbody.result}lua"],
            fix_response="""lua{
local result = wf.vars.RESTbody.result
for _, item in ipairs(result) do
    if type(item) == "table" then
        item.ID = nil
        item.ENTITY_ID = nil
        item.CALL = nil
    end
end
return result
}lua""",
        )
        target_path = self.tmp_path / "sample.lua"
        prompt = """Для полученных данных из предыдущего REST запроса очисти значения переменных ID, ENTITY_ID, CALL.

{
  "wf": {
    "vars": {
      "RESTbody": {
        "result": [
          {
            "ID": 123,
            "ENTITY_ID": 456,
            "CALL": "example_call_1",
            "OTHER_KEY_1": "value1",
            "OTHER_KEY_2": "value2"
          },
          {
            "ID": 789,
            "ENTITY_ID": 101,
            "CALL": "example_call_2",
            "EXTRA_KEY_1": "value3",
            "EXTRA_KEY_2": "value4"
          }
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

        self.assertEqual(llm.fix_calls, 1)
        self.assertTrue(result["save_success"])
        self.assertEqual(result["verification"]["selected_operation"], "remove_keys")
        self.assertIn('item.ID = nil', result["generated_code"])
        self.assertIn('item.ENTITY_ID = nil', result["generated_code"])
        self.assertIn('item.CALL = nil', result["generated_code"])

    def test_bare_field_name_maps_to_unique_workflow_path_and_blocks_wrong_code(self) -> None:
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
local result = wf.vars.RESTbody.result
for _, item in ipairs(result) do
    if type(item) == "table" then
        item.ID = nil
        item.ENTITY_ID = nil
        item.CALL = nil
    end
end
return result
}lua"""
            ],
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
        self.assertEqual(result["verification"]["selected_primary_path"], "wf.initVariables.recallTime")
        self.assertIn("wf.initVariables.recallTime", result["generated_code"])
        self.assertEqual(result["verification"]["expected_workflow_paths"], ["wf.initVariables.recallTime"])

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
        self.assertIn("argument #1 of type `table`", llm.last_fix_prompt)
        self.assertIn("validate or convert the workflow value", llm.last_fix_prompt)
        self.assertIn("wf.initVariables.recallTime", result["generated_code"])

    def test_ambiguity_returns_clarification_without_generation(self) -> None:
        async def fake_run_diagnostics(code: str, lua_bin: str = "lua55", startup_timeout: float = 3.0) -> dict:
            return _success_diagnostics()

        def fake_save_output(target_path: str, code: str, jsonstring_code: str = "") -> dict:
            return {
                "lua_path": target_path,
                "jsonstring_path": f"{target_path}.jsonstring.txt",
            }

        llm = StubLLM(generate_responses=[], fix_response="")
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

        llm = StubLLM(generate_responses=[], fix_response="")
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


if __name__ == "__main__":
    unittest.main()
