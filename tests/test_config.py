from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from ai_disk_assistant.config import (
    Settings,
    load_dotenv,
    normalize_api_style,
    read_dotenv,
    update_dotenv,
)


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

    def test_update_dotenv_preserves_comments_and_unknown_keys(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / ".env"
            path.write_text(
                "# existing comment\nOTHER_SETTING=keep\nAI_MODEL=old-model\n",
                encoding="utf-8",
            )
            with patch.dict(os.environ, {}, clear=True):
                updated = update_dotenv(
                    {
                        "AI_API_KEY": "secret",
                        "AI_BASE_URL": "https://gateway.example/v1",
                        "AI_MODEL": "new-model",
                        "AI_API_STYLE": "responses",
                    },
                    path,
                )
                values = read_dotenv(updated)
                self.assertEqual(values["OTHER_SETTING"], "keep")
                self.assertEqual(values["AI_MODEL"], "new-model")
                self.assertEqual(os.environ["AI_MODEL"], "new-model")
                self.assertIn("# existing comment", updated.read_text(encoding="utf-8"))

    def test_load_dotenv_override_refreshes_running_process(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / ".env"
            path.write_text("AI_MODEL=from-file\n", encoding="utf-8")
            with patch.dict(os.environ, {"AI_MODEL": "old"}, clear=True):
                load_dotenv(path)
                self.assertEqual(os.environ["AI_MODEL"], "old")
                load_dotenv(path, override=True)
                self.assertEqual(os.environ["AI_MODEL"], "from-file")

    def test_update_dotenv_rejects_unknown_keys(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            with self.assertRaises(ValueError):
                update_dotenv({"UNKNOWN_KEY": "value"}, Path(temp_dir) / ".env")


if __name__ == "__main__":
    unittest.main()
