from __future__ import annotations

import os
import tempfile
import time
import unittest
from pathlib import Path

from ai_disk_assistant.ai_advisor import HybridAdvisor
from ai_disk_assistant.config import Settings
from ai_disk_assistant.scanner import ScanPolicy, scan_candidates


class ScannerTests(unittest.TestCase):
    def test_scanner_finds_old_cache_log_but_not_normal_text(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            cache = root / "cache"
            cache.mkdir()
            log_file = cache / "app.log"
            log_file.write_text("log", encoding="utf-8")
            normal_file = root / "notes.txt"
            normal_file.write_text("important", encoding="utf-8")

            old_time = time.time() - 400 * 24 * 60 * 60
            os.utime(log_file, (old_time, old_time))
            os.utime(normal_file, (old_time, old_time))

            advisor = HybridAdvisor(Settings(None, "https://example.invalid/v1", "demo", 1), enable_ai=False)
            candidates = scan_candidates(root, advisor, ScanPolicy(old_days=180, ai_limit=10))
            paths = {Path(item.metadata.path).name for item in candidates}
            self.assertIn("app.log", paths)
            self.assertNotIn("notes.txt", paths)


if __name__ == "__main__":
    unittest.main()
