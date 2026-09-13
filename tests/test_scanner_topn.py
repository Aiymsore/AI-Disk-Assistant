from __future__ import annotations

import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from ai_disk_assistant.ai_advisor import HybridAdvisor
from ai_disk_assistant.config import Settings
from ai_disk_assistant.scanner import ScanPolicy, scan_with_stats


class ScannerTopNTests(unittest.TestCase):
    def test_full_traversal_retains_best_candidate_not_first_candidate(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            cache = root / "cache"
            cache.mkdir()
            first = cache / "first.tmp"
            best = cache / "best.tmp"
            first.write_bytes(b"x")
            best.write_bytes(b"x" * 1024 * 1024)
            old = time.time() - 400 * 24 * 60 * 60
            os.utime(first, (old, old))
            os.utime(best, (old, old))
            advisor = HybridAdvisor(Settings(None, "https://example.invalid/v1", "", 1, ai_cache_path=""), enable_ai=False)
            with patch("ai_disk_assistant.scanner._iter_files", return_value=iter([first, best])):
                result = scan_with_stats(root, advisor, ScanPolicy(max_candidates=1, ai_limit=1))
            self.assertEqual(result.stats.visited_files, 2)
            self.assertEqual(Path(result.candidates[0].metadata.path).name, "best.tmp")


if __name__ == "__main__":
    unittest.main()
