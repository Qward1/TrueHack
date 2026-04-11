import unittest

from src.tools.lua_tools import inspect_lowcode_request_alignment


class LowcodeAlignmentTests(unittest.TestCase):
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
        self.assertTrue(any("invented demo table" in item for item in result["anti_patterns"]))

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


if __name__ == "__main__":
    unittest.main()
