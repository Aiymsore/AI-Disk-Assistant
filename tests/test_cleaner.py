from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from ai_disk_assistant.cleaner import verify_file_unchanged
from ai_disk_assistant.metadata import get_file_metadata


class CleanerTests(unittest.TestCase):
    def test_unchanged_file_passes(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "cache.tmp"
            path.write_text("a", encoding="utf-8")
            metadata = get_file_metadata(path)
            self.assertEqual(verify_file_unchanged(metadata), (True, ""))

    def test_changed_file_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "cache.tmp"
            path.write_text("a", encoding="utf-8")
            metadata = get_file_metadata(path)
            path.write_text("changed content", encoding="utf-8")
            allowed, reason = verify_file_unchanged(metadata)
            self.assertFalse(allowed)
            self.assertIn("变化", reason)


if __name__ == "__main__":
    unittest.main()
