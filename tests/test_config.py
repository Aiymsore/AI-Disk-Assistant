from __future__ import annotations

import os
import unittest
from unittest.mock import patch

from ai_disk_assistant.config import Settings, normalize_api_style


class ConfigTests(unittest.TestCase):
    def test_api_style_aliases(self) -> None:
        self.assertEqual(normalize_api_style("responses"), "responses")
        self.assertEqual(normalize_api_style("response"), "responses")
        self.assertEqual(normalize_api_style("chat"), "chat_completions")
        self.assertEqual(normalize_api_style("chat-completions"), "chat_completions")

    def test_invalid_api_style_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            normalize_api_style("unknown")

    def test_settings_read_responses_style(self) -> None:
        environment = {
            "AI_API_KEY": "secret",
            "AI_BASE_URL": "https://gateway.example/v1/",
            "AI_MODEL": "demo",
            "AI_API_STYLE": "responses",
        }
        with patch.dict(os.environ, environment, clear=True):
            settings = Settings.from_env()
        self.assertEqual(settings.ai_base_url, "https://gateway.example/v1")
        self.assertEqual(settings.ai_api_style, "responses")
        self.assertEqual(settings.ai_model, "demo")


if __name__ == "__main__":
    unittest.main()
