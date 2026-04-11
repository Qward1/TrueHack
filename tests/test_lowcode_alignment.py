import unittest

from src.tools.lua_tools import (
    compile_lowcode_request,
    inspect_lowcode_request_alignment,
    parse_lowcode_workflow_context,
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

    def test_compile_request_builds_count_script_for_cart_items(self) -> None:
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
            allow_deterministic=True,
        )

        self.assertEqual(compiled["selected_operation"], "count")
        self.assertEqual(compiled["selected_primary_path"], "wf.vars.cart.items")
        self.assertEqual(compiled["deterministic_code"], "return #wf.vars.cart.items")
        self.assertTrue(compiled["use_deterministic_fast_path"])

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
            allow_deterministic=True,
        )

        self.assertTrue(compiled["needs_clarification"])
        self.assertIn("wf.vars.cart.items", compiled["clarifying_question"])
        self.assertIn("wf.vars.wishlist.items", compiled["clarifying_question"])

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


if __name__ == "__main__":
    unittest.main()
