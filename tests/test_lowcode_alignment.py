import unittest

from src.tools.lua_tools import (
    _extract_runtime_context,
    build_lowcode_validation_harness,
    compile_lowcode_request,
    format_lowcode_json_payload,
    inspect_lowcode_request_alignment,
    normalize_lua_code,
    parse_lowcode_workflow_context,
    suggest_json_payload_field_name,
    validate_lowcode_llm_output,
)


class LowcodeAlignmentTests(unittest.TestCase):
    def test_parse_workflow_context_extracts_cart_items_inventory(self) -> None:
        context = """{
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

        parsed = parse_lowcode_workflow_context(context)

        self.assertTrue(parsed["has_parseable_context"])
        self.assertEqual(parsed["path_types"].get("wf.vars.cart.items"), "array_object")

    def test_compile_request_resolves_count_operation_and_path_for_cart_items(self) -> None:
        compiled = compile_lowcode_request(
            task_text="Посчитай количество товаров в корзине.",
            raw_context="""{
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
}""",
        )

        self.assertEqual(compiled["selected_operation"], "count")
        self.assertEqual(compiled["selected_primary_path"], "wf.vars.cart.items")
        self.assertFalse(compiled["needs_clarification"])
        self.assertNotIn("deterministic_code", compiled)
        self.assertNotIn("use_deterministic_fast_path", compiled)

    def test_compile_request_asks_for_clarification_on_ambiguous_arrays(self) -> None:
        compiled = compile_lowcode_request(
            task_text="Посчитай количество товаров.",
            raw_context="""{
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
}""",
        )

        self.assertTrue(compiled["needs_clarification"])
        self.assertIn("wf.vars.cart.items", compiled["clarifying_question"])
        self.assertIn("wf.vars.wishlist.items", compiled["clarifying_question"])

    def test_compile_request_infers_array_normalization_expectation(self) -> None:
        compiled = compile_lowcode_request(
            task_text="Если поле contacts не массив, оберни его в массив.",
            raw_context="""{
  "wf": {
    "vars": {
      "contacts": {
        "name": "Ivan"
      }
    }
  }
}""",
        )

        self.assertEqual(compiled["selected_primary_path"], "wf.vars.contacts")
        self.assertIn("array_normalization", compiled["semantic_expectations"])

    def test_compile_request_preserves_parsed_context_for_runtime_validation(self) -> None:
        compiled = compile_lowcode_request(
            task_text="Convert wf.initVariables.recallTime to unix timestamp.",
            raw_context="""{
  "wf": {
    "initVariables": {
      "recallTime": "2023-10-15T15:30:00+00:00"
    }
  }
}""",
        )

        self.assertEqual(
            compiled["parsed_context"]["wf"]["initVariables"]["recallTime"],
            "2023-10-15T15:30:00+00:00",
        )

    def test_array_normalization_flags_next_check_and_source_marking_shortcuts(self) -> None:
        prompt = """Если поле contacts не массив, оберни его в массив.

{
  "wf": {
    "vars": {
      "contacts": {
        "name": "Ivan"
      }
    }
  }
}"""
        compiled = compile_lowcode_request(
            task_text="Если поле contacts не массив, оберни его в массив.",
            raw_context="""{
  "wf": {
    "vars": {
      "contacts": {
        "name": "Ivan"
      }
    }
  }
}""",
        )
        code = """
if type(wf.vars.contacts) ~= 'table' or next(wf.vars.contacts) == nil then
    wf.vars.contacts = _utils.array.new()
    _utils.array.markAsArray(wf.vars.contacts)
end
return wf.vars.contacts
""".strip()

        result = inspect_lowcode_request_alignment(prompt, code, compiled_request=compiled)

        self.assertFalse(result["passed"])
        self.assertTrue(
            any("Do not relabel the original workflow value as an array in place" in item for item in result["missing_requirements"])
        )
        self.assertTrue(
            any("Checking `next(value)` only distinguishes empty vs non-empty tables" in item for item in result["missing_requirements"])
        )

    def test_fails_when_explicit_workflow_path_is_ignored(self) -> None:
        prompt = "Use wf.vars.emails and return the last email."
        code = """
local emails = {"user1@example.com", "user2@example.com"}
return emails[#emails]
""".strip()

        result = inspect_lowcode_request_alignment(prompt, code)

        self.assertFalse(result["passed"])
        self.assertIn("wf.vars.emails", result["expected_workflow_paths"])
        self.assertEqual(result["actual_workflow_paths"], [])
        self.assertTrue(any("wf.vars.emails" in item for item in result["missing_requirements"]))

    def test_passes_when_init_variable_path_is_used_directly(self) -> None:
        prompt = "Convert wf.initVariables.recallTime and return it."
        code = "return wf.initVariables.recallTime"

        result = inspect_lowcode_request_alignment(prompt, code)

        self.assertTrue(result["passed"])
        self.assertIn("wf.initVariables.recallTime", result["actual_workflow_paths"])

    def test_flags_demo_tables_when_workflow_context_is_present(self) -> None:
        prompt = """Return the last email from the provided workflow context.

{
  "wf": {
    "vars": {
      "emails": ["user1@example.com", "user2@example.com"]
    }
  }
}"""
        code = """
local data = {
    users = {
        { email = "user1@example.com" },
        { email = "user2@example.com" }
    }
}
return data.users[#data.users].email
""".strip()

        result = inspect_lowcode_request_alignment(prompt, code)

        self.assertFalse(result["passed"])
        self.assertTrue(result["anti_patterns"])
        self.assertTrue(any("invented" in item for item in result["anti_patterns"]))

    def test_requires_direct_return_for_simple_workflow_tasks(self) -> None:
        prompt = "Use wf.vars.emails and return the last email."
        code = "local last_email = wf.vars.emails[#wf.vars.emails]"

        result = inspect_lowcode_request_alignment(prompt, code)

        self.assertFalse(result["passed"])
        self.assertTrue(
            any("Return the computed workflow value directly" in item for item in result["missing_requirements"])
        )

    def test_passes_for_public_sample_style_code(self) -> None:
        prompt = """Return the last email from the provided workflow context.

{
  "wf": {
    "vars": {
      "emails": ["user1@example.com", "user2@example.com", "user3@example.com"]
    }
  }
}"""
        code = "return wf.vars.emails[#wf.vars.emails]"

        result = inspect_lowcode_request_alignment(prompt, code)

        self.assertTrue(result["passed"])
        self.assertIn("wf.vars.emails", result["actual_workflow_paths"])

    def test_normalize_lua_code_extracts_embedded_json_payload(self) -> None:
        raw = """lua{
json
{
  "lua": "return _utils.array.find(wf.vars.orders, function(order) return order.status == 'NEW' end).id"
}
}lua"""

        normalized = normalize_lua_code(raw)

        self.assertEqual(
            normalized,
            "return _utils.array.find(wf.vars.orders, function(order) return order.status == 'NEW' end).id",
        )

    def test_normalize_lua_code_extracts_lua_from_fenced_json_payload(self) -> None:
        raw = """```json
{"contacts": "lua{\\r\\n\\n  local contacts = wf.vars.contacts\\n  return contacts\\n\\r\\n}lua"}
```"""

        normalized = normalize_lua_code(raw)

        self.assertEqual(
            normalized,
            "local contacts = wf.vars.contacts\n  return contacts",
        )

    def test_normalize_lua_code_extracts_lua_from_fenced_json_payload_with_meta_prefix(self) -> None:
        raw = """Ответ:

```json
{"contacts": "lua{\\r\\n\\n  local contacts = wf.vars.contacts\\n  if type(contacts) ~= 'table' then\\n    local arr = _utils.array.new()\\n    arr[1] = contacts\\n    _utils.array.markAsArray(arr)\\n    return arr\\n  end\\n  return contacts\\n\\r\\n}lua"}
```"""

        normalized = normalize_lua_code(raw)

        self.assertIn("local contacts = wf.vars.contacts", normalized)
        self.assertNotIn("\\n", normalized)
        self.assertNotIn('{"contacts":', normalized)

    def test_normalize_lua_code_unwraps_nested_lowcode_wrappers(self) -> None:
        raw = """```json
{"recallTimeEpoch": "lua{\\r\\nlua{\\r\\nlocal value = wf.initVariables.recallTime\\r\\nreturn value\\r\\n}lua\\r\\n}lua"}
```"""

        normalized = normalize_lua_code(raw)

        self.assertEqual(
            normalized,
            "local value = wf.initVariables.recallTime\nreturn value",
        )

    def test_validate_lowcode_llm_output_rejects_fenced_wrapper(self) -> None:
        analysis = validate_lowcode_llm_output("```lua\nlua{return wf.vars.contacts}lua\n```")

        self.assertFalse(analysis["valid"])
        self.assertIn("without markdown code fences", analysis["reason"])

    def test_validate_lowcode_llm_output_rejects_quoted_wrapper(self) -> None:
        analysis = validate_lowcode_llm_output('"lua{return wf.vars.contacts}lua"')

        self.assertFalse(analysis["valid"])
        self.assertIn("without surrounding quotes", analysis["reason"])

    def test_format_lowcode_json_payload_uses_workflow_leaf_name(self) -> None:
        payload = format_lowcode_json_payload(
            "return wf.initVariables.recallTime",
            compiled_request={
                "selected_primary_path": "wf.initVariables.recallTime",
            },
        )

        self.assertIn('"recallTime"', payload)
        self.assertIn('lua{\\r\\nreturn wf.initVariables.recallTime\\r\\n}lua', payload)

    def test_normalize_lua_code_extracts_payload_from_named_json_field(self) -> None:
        raw = format_lowcode_json_payload(
            "return iso_date",
            compiled_request={"selected_primary_path": "wf.vars.time"},
        )

        self.assertEqual(normalize_lua_code(raw), "return iso_date")
        self.assertEqual(
            suggest_json_payload_field_name(compiled_request={"selected_primary_path": "wf.vars.time"}),
            "time",
        )

    def test_fails_for_generic_top_level_table_with_structured_context(self) -> None:
        prompt = """Посчитай количество товаров в корзине.

{
  "wf": {
    "vars": {
      "cart": {
        "items": [
          { "sku": "A001" }
        ]
      }
    }
  }
}"""
        code = """
local cart = {
    items = {
        { sku = "A001" }
    }
}
return #cart.items
""".strip()

        result = inspect_lowcode_request_alignment(prompt, code)

        self.assertFalse(result["passed"])
        self.assertTrue(any("invented top-level table" in item for item in result["anti_patterns"]))

    def test_validation_harness_uses_provided_workflow_context_before_mocks(self) -> None:
        harness, mocked = build_lowcode_validation_harness(
            "sample.lua",
            "return wf.initVariables.recallTime",
            workflow_context={
                "wf": {
                    "initVariables": {
                        "recallTime": "2023-10-15T15:30:00+00:00",
                    }
                }
            },
        )

        self.assertIn('wf.initVariables = {["recallTime"] = "2023-10-15T15:30:00+00:00"}', harness)
        self.assertIn("if wf.initVariables[\"recallTime\"] == nil then", harness)
        self.assertEqual(mocked["initVariables"], ["recallTime"])

    def test_extract_runtime_context_parses_locals_and_strips_markers(self) -> None:
        run_output = """__TRUEHACK_CONTEXT_START__
__TRUEHACK_FRAME__\t15\t\t@C:\\temp\\script.lua
__TRUEHACK_LOCAL__\toffsetSign\tstring\t-
__TRUEHACK_LOCAL__\toffsetHours\tnil\tnil
__TRUEHACK_CONTEXT_END__
C:\\temp\\script.lua:15: attempt to perform arithmetic on a nil value"""

        cleaned, runtime_context = _extract_runtime_context(run_output)

        self.assertEqual(
            cleaned,
            "C:\\temp\\script.lua:15: attempt to perform arithmetic on a nil value",
        )
        self.assertEqual(runtime_context["line"], 15)
        self.assertEqual(runtime_context["locals"][0]["name"], "offsetSign")
        self.assertEqual(runtime_context["locals"][1]["type"], "nil")


if __name__ == "__main__":
    unittest.main()
