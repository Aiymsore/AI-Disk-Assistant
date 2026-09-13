from __future__ import annotations

import unittest

from ai_disk_assistant.models import FileMetadata
from ai_disk_assistant.privacy import anonymize_path, metadata_for_ai


class PrivacyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.metadata = FileMetadata(
            path=r"C:\Users\Alice\Documents\SecretProject\report.docx",
            name="report.docx",
            suffix=".docx",
            parent_folder=r"C:\Users\Alice\Documents\SecretProject",
            size_bytes=10,
            size_text="10 B",
            modified_time="2026-01-01 00:00:00",
            accessed_time="2026-01-01 00:00:00",
        )

    def test_balanced_mode_masks_windows_username(self) -> None:
        payload = metadata_for_ai(self.metadata, "balanced")
        self.assertNotIn("Alice", payload["path"])
        self.assertIn("<USER>", payload["path"])

    def test_strict_mode_omits_path(self) -> None:
        payload = metadata_for_ai(self.metadata, "strict")
        self.assertNotIn("path", payload)
        self.assertNotIn("parent_folder", payload)
        self.assertEqual(payload["name"], "report.docx")

    def test_full_mode_keeps_path(self) -> None:
        payload = metadata_for_ai(self.metadata, "full")
        self.assertEqual(payload["path"], self.metadata.path)

    def test_anonymize_is_idempotent(self) -> None:
        once = anonymize_path(self.metadata.path)
        self.assertEqual(anonymize_path(once), once)


if __name__ == "__main__":
    unittest.main()
