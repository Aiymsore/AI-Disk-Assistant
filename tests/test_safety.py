from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from ai_disk_assistant.ai_advisor import HybridAdvisor, _extract_json
from ai_disk_assistant.config import Settings
from ai_disk_assistant.metadata import get_file_metadata
from ai_disk_assistant.safety import can_move_to_trash, is_protected_path


class SafetyTests(unittest.TestCase):
    def test_windows_path_is_protected(self) -> None:
        self.assertTrue(is_protected_path(r"C:\Windows\System32\kernel.dll"))
        self.assertTrue(is_protected_path(r"C:\Program Files\Example\app.exe"))

    def test_user_document_requires_confirmation(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            file = Path(temp_dir) / "report.docx"
            file.write_bytes(b"demo")
            advisor = HybridAdvisor(
                Settings(None, "https://example.invalid/v1", "demo", 1),
                enable_ai=False,
            )
            advice = advisor.advise(get_file_metadata(file))
            self.assertFalse(advice.recommend_delete)
            self.assertEqual(advice.advice_level, "人工确认")

    def test_temp_log_can_be_suggested(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            cache_dir = Path(temp_dir) / "cache"
            cache_dir.mkdir()
            file = cache_dir / "old.log"
            file.write_text("demo", encoding="utf-8")
            advisor = HybridAdvisor(
                Settings(None, "https://example.invalid/v1", "demo", 1),
                enable_ai=False,
            )
            advice = advisor.advise(get_file_metadata(file))
            self.assertTrue(advice.recommend_delete)
            self.assertEqual(advice.advice_level, "建议删除")

    def test_directory_cannot_be_trashed(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            allowed, reason = can_move_to_trash(Path(temp_dir))
            self.assertFalse(allowed)
            self.assertIn("文件夹", reason)

    def test_extract_json_from_code_fence(self) -> None:
        result = _extract_json('```json\n{"recommend_delete": false}\n```')
        self.assertFalse(result["recommend_delete"])

    def test_model_name_is_required_for_ai_mode(self) -> None:
        advisor = HybridAdvisor(
            Settings("secret", "https://example.invalid/v1", "", 1),
            enable_ai=True,
        )
        self.assertFalse(advisor.ai_available)


if __name__ == "__main__":
    unittest.main()
