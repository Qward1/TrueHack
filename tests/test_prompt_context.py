import unittest

from src.graph.nodes import split_task_and_context


class SplitTaskAndContextTests(unittest.TestCase):
    def test_plain_prompt(self) -> None:
        task, context = split_task_and_context("Return the last email from the workflow input.")

        self.assertEqual(task, "Return the last email from the workflow input.")
        self.assertEqual(context, "")

    def test_trailing_json_block(self) -> None:
        prompt = """Return the last email.

{
  "wf": {
    "vars": {
      "emails": ["a@example.com", "b@example.com"]
    }
  }
}"""

        task, context = split_task_and_context(prompt)

        self.assertEqual(task, "Return the last email.")
        self.assertIn('"emails": ["a@example.com", "b@example.com"]', context)

    def test_fenced_block(self) -> None:
        prompt = """Increment the retry counter.

```json
{
  "wf": {
    "vars": {
      "try_count_n": 3
    }
  }
}
```"""

        task, context = split_task_and_context(prompt)

        self.assertEqual(task, "Increment the retry counter.")
        self.assertIn('"try_count_n": 3', context)


if __name__ == "__main__":
    unittest.main()
