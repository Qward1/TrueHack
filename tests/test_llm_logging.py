import unittest

from src.core import llm as llm_module


class LLMLoggingHelpersTests(unittest.TestCase):
    def test_normalize_messages_serializes_non_string_content(self) -> None:
        messages = [
            {"role": "system", "content": {"policy": "strict", "retries": 2}},
            {"role": "user", "content": "return wf.vars.value"},
        ]

        payload = llm_module._normalize_messages_for_logging(messages)

        self.assertEqual(len(payload), 2)
        self.assertEqual(payload[0]["role"], "system")
        self.assertIn('"policy": "strict"', payload[0]["content"])
        self.assertEqual(payload[1]["content"], "return wf.vars.value")

    def test_prompt_audit_truncation(self) -> None:
        previous = llm_module.PROMPT_MAX_CHARS
        llm_module.PROMPT_MAX_CHARS = 5
        try:
            truncated = llm_module._truncate_for_prompt_audit("123456789")
            self.assertIn("[truncated 4 chars]", truncated)
            self.assertTrue(truncated.startswith("12345"))
        finally:
            llm_module.PROMPT_MAX_CHARS = previous


if __name__ == "__main__":
    unittest.main()
