import unittest
from unittest.mock import patch

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

    def test_agent_model_env_key_uses_upper_snake_case(self) -> None:
        self.assertEqual(
            llm_module._agent_model_env_key("CodeGenerator"),
            "OLLAMA_MODEL_CODE_GENERATOR",
        )
        self.assertEqual(
            llm_module._agent_model_env_key("TemplateSelector"),
            "OLLAMA_MODEL_TEMPLATE_SELECTOR",
        )
        self.assertEqual(
            llm_module._agent_model_env_key("ContractVerifier"),
            "OLLAMA_MODEL_CONTRACT_VERIFIER",
        )
        self.assertEqual(
            llm_module._agent_model_env_key("UniversalVerificationFixer"),
            "OLLAMA_MODEL_UNIVERSAL_VERIFICATION_FIXER",
        )

    def test_resolve_agent_model_prefers_agent_override(self) -> None:
        with patch.dict(
            llm_module.os.environ,
            {"OLLAMA_MODEL_VALIDATION_FIXER": "qwen2.5-coder:3b-instruct"},
            clear=False,
        ):
            provider = llm_module.LLMProvider(model="qwen2.5-coder:7b-instruct")
            self.assertEqual(
                provider.resolve_model("ValidationFixer"),
                "qwen2.5-coder:3b-instruct",
            )
            self.assertEqual(
                provider.resolve_model("CodeGenerator"),
                "qwen2.5-coder:7b-instruct",
            )

    def test_provider_reads_shared_model_from_env_at_init_time(self) -> None:
        with patch.dict(
            llm_module.os.environ,
            {"OLLAMA_MODEL": "qwen2.5-coder:14b-instruct"},
            clear=False,
        ):
            provider = llm_module.LLMProvider()
            self.assertEqual(provider.resolve_model(""), "qwen2.5-coder:14b-instruct")


if __name__ == "__main__":
    unittest.main()
