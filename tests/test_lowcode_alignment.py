import unittest

from src.tools.lua_tools import (
    _extract_runtime_context,
    _extract_runtime_result,
    build_lowcode_validation_harness,
    compile_lowcode_request,
    format_lowcode_json_payload,
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

    def test_parse_workflow_context_extracts_loose_wf_fragment(self) -> None:
        context = """{
}
"wf": {
  "vars": {
    "parsedCsv": [
      { "SKU": "A001", "Discount": "10%", "Markdown": "" },
      { "SKU": "A002", "Discount": "", "Markdown": "5%" }
    ]
  }
}"""

        parsed = parse_lowcode_workflow_context(context)

        self.assertTrue(parsed["has_parseable_context"])
        self.assertEqual(parsed["path_types"].get("wf.vars.parsedCsv"), "array_object")

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

    def test_normalize_lua_code_recovers_malformed_fenced_lowcode_wrapper(self) -> None:
        raw = """```lua{
local value = wf.vars.contacts
return value
}lua
```"""

        normalized = normalize_lua_code(raw)

        self.assertEqual(normalized, "local value = wf.vars.contacts\nreturn value")

    def test_extract_runtime_result_parses_serialized_payload(self) -> None:
        run_output = (
            "__TRUEHACK_RESULT_START__\n"
            '{"items":[{"sku":"A001"},{"sku":"A004"}]}\n'
            "__TRUEHACK_RESULT_END__\n"
        )

        cleaned, result_value, result_preview = _extract_runtime_result(run_output)

        self.assertEqual(cleaned, "")
        self.assertEqual(result_value, {"items": [{"sku": "A001"}, {"sku": "A004"}]})
        self.assertIn('"sku":"A004"', result_preview)

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
        self.assertIn('if wf.initVariables["recallTime"] == nil then', harness)
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
