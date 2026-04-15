import os
import tempfile
import textwrap
import unittest

from scripts import bootstrap_ollama


class BootstrapOllamaTests(unittest.TestCase):
    def test_parse_modelfile_builds_official_create_payload(self) -> None:
        with tempfile.NamedTemporaryFile("w", delete=False, encoding="utf-8") as handle:
            handle.write(
                textwrap.dedent(
                    """
                    FROM qwen2.5-coder:7b-instruct
                    PARAMETER num_ctx 4096
                    PARAMETER temperature 0.2
                    SYSTEM You are a strict coding assistant.
                    """
                ).strip()
            )
            path = handle.name

        try:
            payload = bootstrap_ollama._parse_modelfile(path)
        finally:
            os.unlink(path)

        self.assertEqual(payload["from"], "qwen2.5-coder:7b-instruct")
        self.assertEqual(
            payload["parameters"],
            {"num_ctx": 4096, "temperature": 0.2},
        )
        self.assertEqual(payload["system"], "You are a strict coding assistant.")


if __name__ == "__main__":
    unittest.main()
