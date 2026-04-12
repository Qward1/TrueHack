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
    def __init__(
        self,
        *,
        generate_responses: list[str],
        fix_response: str | list[str],
        route_intent: str = "create",
        verify_response: str | list[str] | None = None,
    ) -> None:
        self._generate_responses = list(generate_responses)
        if isinstance(fix_response, list):
            self._fix_responses = list(fix_response)
        else:
            self._fix_responses = [fix_response]
        self._route_intent = route_intent
        if isinstance(verify_response, list):
            self._verify_responses = list(verify_response)
        elif verify_response is None:
            self._verify_responses = []
        else:
            self._verify_responses = [verify_response]
        self.fix_calls = 0
        self.generate_calls = 0
        self.last_fix_prompt = ""
        self.last_generate_prompt = ""
        self.last_generate_system = ""
        self.last_generate_temperature = None
        self.last_chat_temperature = None
        self.last_verify_messages: list[dict] = []

    async def generate(self, prompt: str, system: str = "", temperature: float = 0.2, max_tokens: int | None = None) -> str:
        if system.startswith(ROUTE_SYSTEM_PREFIX):
            raise AssertionError("route_intent should use generate_json")
        if system.startswith(EXPLAIN_SYSTEM_PREFIX):
            raise AssertionError("explain_solution should use generate_json")
        self.generate_calls += 1
        self.last_generate_prompt = prompt
        self.last_generate_system = system
        self.last_generate_temperature = temperature
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
        self.last_chat_temperature = temperature
        if system.startswith(VERIFY_SYSTEM_PREFIX):
            self.last_verify_messages = list(messages)
            if self._verify_responses:
                return self._verify_responses.pop(0)
            return (
                '{"passed": true, "score": 100, "summary": "LLM verification passed.", '
                '"missing_requirements": [], "warnings": [], '
                '"checks": {'
                '"workflow_path_usage": {"status": "pass", "reason": ""}, '
                '"source_shape_understood": {"status": "pass", "reason": ""}, '
                '"target_shape_satisfied": {"status": "pass", "reason": ""}, '
                '"logic_correctness": {"status": "pass", "reason": ""}, '
                '"helper_api_usage": {"status": "pass", "reason": ""}, '
                '"edge_case_handling": {"status": "pass", "reason": ""}'
                '}}'
            )
        if system.startswith(FIX_SYSTEM_PREFIX):
            self.fix_calls += 1
            self.last_fix_prompt = str(messages[-1].get("content", "")) if messages else ""
            if self._fix_responses:
                return self._fix_responses.pop(0)
            raise AssertionError("unexpected extra fix call")
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
        "runtime_context": {},
        "result_value": None,
        "result_preview": "",
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
        self.assertTrue(result["verification"]["passed"])
        self.assertEqual(llm.fix_calls, 0)
        self.assertEqual(llm.generate_calls, 1)
        self.assertNotIn("shortest correct script", llm.last_generate_system)

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
        self.assertTrue(result["verification"]["passed"])
        self.assertEqual(llm.generate_calls, 1)
        self.assertIn('"emails"', result["response"])
        self.assertIn('lua{\\r\\nreturn wf.vars.emails[#wf.vars.emails]\\r\\n}lua', result["response"])
        self.assertEqual(len(saved_payloads), 1)
        self.assertIn('"emails"', saved_payloads[0])
        self.assertIn('lua{\\r\\nreturn wf.vars.emails[#wf.vars.emails]\\r\\n}lua', saved_payloads[0])

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
        self.assertIn("The response must start with `lua{` and end with `}lua`", llm.last_generate_system)
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

    def test_array_normalization_task_rejects_table_only_shortcut_and_enters_fix_loop(self) -> None:
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
if type(wf.vars.contacts) ~= 'table' then wf.vars.contacts = _utils.array.new({wf.vars.contacts}) end
_utils.array.markAsArray(wf.vars.contacts)
return wf.vars.contacts
}lua"""
            ],
            verify_response=(
                '{"passed": false, "score": 15, "summary": "The code mutates and returns the original object instead of producing a safe array result.", '
                '"missing_requirements": ["Wrap wf.vars.contacts into a new array and return that array instead of rewriting the source value in place."], '
                '"warnings": [], '
                '"checks": {'
                '"workflow_path_usage": {"status": "pass", "reason": ""}, '
                '"source_shape_understood": {"status": "fail", "reason": "The solution does not distinguish object-like tables from array-like tables."}, '
                '"target_shape_satisfied": {"status": "fail", "reason": "The returned value is not guaranteed to be a proper array result."}, '
                '"logic_correctness": {"status": "fail", "reason": "The script marks the source value as an array in place."}, '
                '"helper_api_usage": {"status": "fail", "reason": "_utils.array.new is used with inline arguments."}, '
                '"edge_case_handling": {"status": "fail", "reason": "Object tables are not handled safely."}'
                '}}'
            ),
            fix_response="""lua{
local contacts = wf.vars.contacts
if type(contacts) ~= "table" then
    local arr = _utils.array.new()
    arr[1] = contacts
    _utils.array.markAsArray(arr)
    return arr
end
local is_array = true
for key in pairs(contacts) do
    if type(key) ~= "number" or math.floor(key) ~= key then
        is_array = false
        break
    end
end
if is_array then
    return contacts
end
local arr = _utils.array.new()
arr[1] = contacts
_utils.array.markAsArray(arr)
return arr
}lua""",
        )
        target_path = self.tmp_path / "contacts.lua"
        prompt = """Если поле contacts не массив, оберни его в массив.

{
  "wf": {
    "vars": {
      "contacts": {
        "name": "Ivan",
        "phone": "+79990001122"
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
        self.assertIn("_utils.array.new()", result["generated_code"])
        self.assertIn("for key in pairs(contacts) do", result["generated_code"])
        self.assertIn(
            "Wrap wf.vars.contacts into a new array and return that array instead of rewriting the source value in place.",
            llm.last_fix_prompt,
        )
        self.assertIn("Current broken code with line numbers:", llm.last_fix_prompt)
        self.assertIn("return wf.vars.contacts", llm.last_fix_prompt)
        self.assertNotIn("Rewrite the script from scratch", llm.last_fix_prompt)

    def test_array_normalization_rejects_next_check_and_source_marking_shortcut(self) -> None:
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
if type(wf.vars.contacts) ~= 'table' or next(wf.vars.contacts) == nil then
    wf.vars.contacts = _utils.array.new()
    _utils.array.markAsArray(wf.vars.contacts)
end
return wf.vars.contacts
}lua"""
            ],
            verify_response=(
                '{"passed": false, "score": 20, "summary": "The code still returns the original workflow value without proving array semantics.", '
                '"missing_requirements": ["Wrap object-like wf.vars.contacts into a new array instead of relabeling the source value in place."], '
                '"warnings": [], '
                '"checks": {'
                '"workflow_path_usage": {"status": "pass", "reason": ""}, '
                '"source_shape_understood": {"status": "fail", "reason": "next(...) only distinguishes empty vs non-empty tables."}, '
                '"target_shape_satisfied": {"status": "fail", "reason": "The script may still return an object table."}, '
                '"logic_correctness": {"status": "fail", "reason": "The source workflow value is marked as an array in place."}, '
                '"helper_api_usage": {"status": "pass", "reason": ""}, '
                '"edge_case_handling": {"status": "fail", "reason": "Object-like tables are not wrapped into a new array."}'
                '}}'
            ),
            fix_response="""lua{
local contacts = wf.vars.contacts
if type(contacts) ~= "table" then
    local arr = _utils.array.new()
    arr[1] = contacts
    _utils.array.markAsArray(arr)
    return arr
end
local is_array = true
for key in pairs(contacts) do
    if type(key) ~= "number" or math.floor(key) ~= key then
        is_array = false
        break
    end
end
if is_array then
    return contacts
end
local arr = _utils.array.new()
arr[1] = contacts
_utils.array.markAsArray(arr)
return arr
}lua""",
        )
        target_path = self.tmp_path / "contacts.lua"
        prompt = """Сделай Если поле contacts не массив, оберни его в массив.

{
  "wf": {
    "vars": {
      "contacts": {
        "name": "Ivan",
        "phone": "+79990001122"
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
        self.assertIn(
            "Wrap object-like wf.vars.contacts into a new array instead of relabeling the source value in place.",
            llm.last_fix_prompt,
        )
        self.assertIn("Current broken code with line numbers:", llm.last_fix_prompt)

    def test_fix_code_accepts_single_repair_when_requirement_failure_is_addressed(self) -> None:
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
local filtered = _utils.array.new()
for _, item in ipairs(wf.vars.parsedCsv) do
    if item.Discount ~= nil or item.Markdown ~= nil then
        table.insert(filtered, item)
    end
end
_utils.array.markAsArray(filtered)
return filtered
}lua"""
            ],
            verify_response=(
                '{"passed": false, "score": 20, "summary": "The result does not satisfy the shape requirement.", '
                '"missing_requirements": ["Return an array-shaped result for contacts and handle object-like tables explicitly."], '
                '"warnings": [], '
                '"checks": {'
                '"workflow_path_usage": {"status": "fail", "reason": "The script works on wf.vars.parsedCsv instead of wf.vars.contacts."}, '
                '"source_shape_understood": {"status": "fail", "reason": "The request is about contacts array normalization."}, '
                '"target_shape_satisfied": {"status": "fail", "reason": "The returned value does not match the requested contacts array shape."}, '
                '"logic_correctness": {"status": "fail", "reason": "The script is solving a different task."}, '
                '"helper_api_usage": {"status": "pass", "reason": ""}, '
                '"edge_case_handling": {"status": "fail", "reason": "The contacts object case is not handled."}'
                '}}'
            ),
            fix_response=[
                """```lua
local contacts = wf.vars.contacts
if type(contacts) ~= "table" or next(contacts, nil) == nil then
    contacts = _utils.array.new()
    table.insert(contacts, wf.vars.contacts)
    _utils.array.markAsArray(contacts)
end
return contacts
```""",
                """lua{
local contacts = wf.vars.contacts
if type(contacts) ~= "table" then
    local arr = _utils.array.new()
    arr[1] = contacts
    _utils.array.markAsArray(arr)
    return arr
end
local is_array = true
for key in pairs(contacts) do
    if type(key) ~= "number" or math.floor(key) ~= key then
        is_array = false
        break
    end
end
if is_array then
    return contacts
end
local arr = _utils.array.new()
arr[1] = contacts
_utils.array.markAsArray(arr)
return arr
}lua""",
            ],
        )
        target_path = self.tmp_path / "contacts.lua"
        prompt = """Сделай Если поле contacts не массив, оберни его в массив.

{
  "wf": {
    "vars": {
      "contacts": {
        "name": "Ivan",
        "phone": "+79990001122"
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
        self.assertIn("local contacts = wf.vars.contacts", result["generated_code"])
        self.assertIn("next(contacts, nil)", result["generated_code"])
        self.assertNotIn("assistant", llm.last_fix_prompt.lower())

    def test_verifier_second_pass_can_overrule_false_positive_first_pass(self) -> None:
        async def fake_run_diagnostics(
            code: str,
            lua_bin: str = "lua55",
            startup_timeout: float = 3.0,
            workflow_context: dict | None = None,
        ) -> dict:
            diagnostics = _success_diagnostics()
            diagnostics["result_value"] = {
                "name": "Ivan",
                "phone": "+79990001122",
            }
            diagnostics["result_preview"] = '{"name":"Ivan","phone":"+79990001122"}'
            return diagnostics

        def fake_save_output(target_path: str, code: str, jsonstring_code: str = "") -> dict:
            return {
                "lua_path": target_path,
                "jsonstring_path": f"{target_path}.jsonstring.txt",
            }

        llm = StubLLM(
            generate_responses=[
                """lua{
local contacts = wf.vars.contacts
if type(contacts) ~= "table" or next(contacts, nil) == nil then
    contacts = _utils.array.new()
    table.insert(contacts, wf.vars.contacts)
    _utils.array.markAsArray(contacts)
end
return contacts
}lua"""
            ],
            fix_response="",
            verify_response=[
                (
                    '{"passed": true, "score": 100, "summary": "Looks correct.", '
                    '"missing_requirements": [], "warnings": [], '
                    '"checks": {'
                    '"workflow_path_usage": {"status": "pass", "reason": ""}, '
                    '"source_shape_understood": {"status": "pass", "reason": ""}, '
                    '"target_shape_satisfied": {"status": "pass", "reason": ""}, '
                    '"logic_correctness": {"status": "pass", "reason": ""}, '
                    '"helper_api_usage": {"status": "pass", "reason": ""}, '
                    '"edge_case_handling": {"status": "pass", "reason": ""}'
                    '}}'
                ),
                (
                    '{"passed": false, "score": 20, "summary": "Runtime result is still an object table, not an array wrapper.", '
                    '"missing_requirements": ["Wrap wf.vars.contacts into a new array and return that array instead of returning the original object table."], '
                    '"warnings": [], '
                    '"checks": {'
                    '"workflow_path_usage": {"status": "pass", "reason": ""}, '
                    '"source_shape_understood": {"status": "pass", "reason": ""}, '
                    '"target_shape_satisfied": {"status": "fail", "reason": "The returned result is an object with keys name/phone instead of an array."}, '
                    '"logic_correctness": {"status": "fail", "reason": "The code returns wf.vars.contacts unchanged when it is a non-empty object table."}, '
                    '"helper_api_usage": {"status": "pass", "reason": ""}, '
                    '"edge_case_handling": {"status": "fail", "reason": "Object-like tables are not wrapped into an array."}'
                    '}}'
                ),
            ],
        )
        target_path = self.tmp_path / "contacts.lua"
        prompt = """Сделай Если поле contacts не массив, оберни его в массив.

{
  "wf": {
    "vars": {
      "contacts": {
        "name": "Ivan",
        "phone": "+79990001122"
      }
    }
  }
}"""

        with patch("src.graph.nodes.async_run_diagnostics", new=fake_run_diagnostics), patch(
            "src.graph.nodes.save_final_output",
            new=fake_save_output,
        ):
            engine = PipelineEngine(llm=llm, max_fix_iterations=0)
            result = asyncio.run(
                engine.process_message(
                    chat_id=1,
                    user_input=prompt,
                    workspace_root=str(self.tmp_path),
                    target_path=str(target_path),
                )
            )

        self.assertFalse(result["verification"]["passed"])
        self.assertFalse(result["save_success"])
        self.assertIn("object table", result["verification"]["summary"])
        self.assertIn(
            "Wrap wf.vars.contacts into a new array and return that array instead of returning the original object table.",
            result["verification"]["missing_requirements"],
        )

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

    def test_verifier_receives_runtime_result_context_for_logic_checks(self) -> None:
        async def fake_run_diagnostics(
            code: str,
            lua_bin: str = "lua55",
            startup_timeout: float = 3.0,
            workflow_context: dict | None = None,
        ) -> dict:
            diagnostics = _success_diagnostics()
            diagnostics["result_value"] = [
                {"SKU": "A001", "Discount": "10%", "Markdown": ""},
                {"SKU": "A002", "Discount": "", "Markdown": "5%"},
                {"SKU": "A004", "Discount": "", "Markdown": ""},
            ]
            diagnostics["result_preview"] = (
                '[{"SKU":"A001","Discount":"10%","Markdown":""},'
                '{"SKU":"A002","Discount":"","Markdown":"5%"},'
                '{"SKU":"A004","Discount":"","Markdown":""}]'
            )
            return diagnostics

        llm = StubLLM(
            generate_responses=[
                """lua{
local function filterParsedCsv(parsedCsv)
    local filteredArray = {}
    for _, item in ipairs(parsedCsv) do
        if item.Discount or item.Markdown then
            table.insert(filteredArray, item)
        end
    end
    return filteredArray
end

return filterParsedCsv(wf.vars.parsedCsv)
}lua"""
            ],
            fix_response="",
            verify_response=(
                '{"passed": false, "score": 25, "summary": "Returned result still contains rows with empty values.", '
                '"missing_requirements": ["Filter out rows where both Discount and Markdown are empty."], '
                '"warnings": [], '
                '"checks": {'
                '"workflow_path_usage": {"status": "pass", "reason": ""}, '
                '"source_shape_understood": {"status": "pass", "reason": ""}, '
                '"target_shape_satisfied": {"status": "fail", "reason": "Runtime result still includes rows that should be excluded."}, '
                '"logic_correctness": {"status": "fail", "reason": "A004 is still present even though both fields are empty strings."}, '
                '"helper_api_usage": {"status": "pass", "reason": ""}, '
                '"edge_case_handling": {"status": "fail", "reason": "Empty-string handling is incorrect."}'
                '}}'
            ),
        )
        prompt = """Отфильтруй элементы из массива, чтобы включить только те, у которых есть значения в
полях Discount или Markdown.

{
}
"wf": {
  "vars": {
    "parsedCsv": [
      { "SKU": "A001", "Discount": "10%", "Markdown": "" },
      { "SKU": "A002", "Discount": "", "Markdown": "5%" },
      { "SKU": "A003", "Discount": null, "Markdown": null },
      { "SKU": "A004", "Discount": "", "Markdown": "" }
    ]
  }
}"""

        with patch("src.graph.nodes.async_run_diagnostics", new=fake_run_diagnostics):
            engine = PipelineEngine(llm=llm, max_fix_iterations=0)
            result = asyncio.run(
                engine.process_message(
                    chat_id=1,
                    user_input=prompt,
                    workspace_root=str(self.tmp_path),
                    target_path="",
                )
            )

        verify_messages_text = "\n".join(str(message.get("content", "")) for message in llm.last_verify_messages)
        self.assertIn("Actual runtime result on the provided workflow context", verify_messages_text)
        self.assertIn("A004", verify_messages_text)
        self.assertFalse(result["verification"]["passed"])
        self.assertFalse(result["save_success"])

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
            verify_response=(
                '{"passed": false, "score": 10, "summary": "The script invents demo input and prints instead of returning the ISO value from workflow data.", '
                '"missing_requirements": ["Use wf.vars.json.IDOC.ZCDF_HEAD.DATUM and wf.vars.json.IDOC.ZCDF_HEAD.TIME directly and return the converted ISO 8601 string."], '
                '"warnings": [], '
                '"checks": {'
                '"workflow_path_usage": {"status": "fail", "reason": "The solution does not use the provided workflow paths directly."}, '
                '"source_shape_understood": {"status": "fail", "reason": "The code recreates payload instead of reading workflow data."}, '
                '"target_shape_satisfied": {"status": "fail", "reason": "The script prints text instead of returning the requested value."}, '
                '"logic_correctness": {"status": "fail", "reason": "The requested conversion is not performed on workflow data."}, '
                '"helper_api_usage": {"status": "pass", "reason": ""}, '
                '"edge_case_handling": {"status": "unclear", "reason": "No workflow-based conversion logic is present."}'
                '}}'
            ),
            fix_response="""lua{
local DATUM = wf.vars.json.IDOC.ZCDF_HEAD.DATUM
local TIME = wf.vars.json.IDOC.ZCDF_HEAD.TIME
return string.format("%s-%s-%sT%s:%s:%s.00000Z", string.sub(DATUM, 1, 4), string.sub(DATUM, 5, 6), string.sub(DATUM, 7, 8), string.sub(TIME, 1, 2), string.sub(TIME, 3, 4), string.sub(TIME, 5, 6))
}lua""",
        )
        target_path = self.tmp_path / "sample.lua"
        prompt = """Преобразуй DATUM/TIME из wf.vars.json.IDOC.ZCDF_HEAD в ISO 8601.

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
        self.assertTrue(result["verification"]["passed"])

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
            verify_response=(
                '{"passed": false, "score": 20, "summary": "The script returns the original array without removing the requested keys.", '
                '"missing_requirements": ["Remove ID, ENTITY_ID, and CALL from each item in wf.vars.RESTbody.result before return."], '
                '"warnings": [], '
                '"checks": {'
                '"workflow_path_usage": {"status": "pass", "reason": ""}, '
                '"source_shape_understood": {"status": "pass", "reason": ""}, '
                '"target_shape_satisfied": {"status": "fail", "reason": "The returned items still contain the forbidden keys."}, '
                '"logic_correctness": {"status": "fail", "reason": "No cleanup logic is applied."}, '
                '"helper_api_usage": {"status": "pass", "reason": ""}, '
                '"edge_case_handling": {"status": "unclear", "reason": "The plain return does not prove per-item cleanup."}'
                '}}'
            ),
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
            verify_response=(
                '{"passed": false, "score": 15, "summary": "The script uses the wrong workflow source and does not convert recallTime.", '
                '"missing_requirements": ["Use wf.initVariables.recallTime as the input source for the unix-time conversion."], '
                '"warnings": [], '
                '"checks": {'
                '"workflow_path_usage": {"status": "fail", "reason": "The solution reads wf.vars.RESTbody.result instead of wf.initVariables.recallTime."}, '
                '"source_shape_understood": {"status": "fail", "reason": "The request is about the recallTime scalar."}, '
                '"target_shape_satisfied": {"status": "fail", "reason": "The requested unix timestamp is never produced."}, '
                '"logic_correctness": {"status": "fail", "reason": "The script solves a different task."}, '
                '"helper_api_usage": {"status": "pass", "reason": ""}, '
                '"edge_case_handling": {"status": "unclear", "reason": "No recallTime handling is present."}'
                '}}'
            ),
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
        self.assertTrue(result["verification"]["passed"])

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
        self.assertIn("Current broken code with line numbers:", llm.last_fix_prompt)
        self.assertIn("  1 | local isoDate = wf.initVariables.recallTime", llm.last_fix_prompt)
        self.assertIn("Likely failing Lua line: 15", llm.last_fix_prompt)
        self.assertIn("Failing code context:", llm.last_fix_prompt)
        self.assertIn("Runtime context snapshot:", llm.last_fix_prompt)
        self.assertIn("offsetSign [string] = -", llm.last_fix_prompt)
        self.assertIn("offsetHours [nil] = nil", llm.last_fix_prompt)
        self.assertIn("When the runtime error points to a specific line", llm.last_fix_prompt)
        self.assertIn("For any value derived from pattern matching, table lookups, helper calls, or workflow input", llm.last_fix_prompt)
        self.assertIn("The proposed fix still fails during Lua validation", llm.last_fix_prompt)
        self.assertIn("Remaining runtime repair hint:", llm.last_fix_prompt)
        self.assertIn("The fix attempt did not materially change the code.", llm.last_fix_prompt)
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
