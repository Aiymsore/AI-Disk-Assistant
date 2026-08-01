from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from ai_disk_assistant.models import Advice, Candidate, FileMetadata, ScanStats
from ai_disk_assistant.report import build_summary, write_html_report


class ReportTests(unittest.TestCase):
    def test_summary_and_html_report(self) -> None:
        metadata = FileMetadata(
            path="C:/Temp/a.tmp",
            name="a.tmp",
            suffix=".tmp",
            parent_folder="C:/Temp",
            size_bytes=1024,
            size_text="1.00 KB",
            modified_time="2025-01-01 00:00:00",
            accessed_time="2025-01-01 00:00:00",
        )
        candidate = Candidate(metadata, "临时文件", Advice(True, "临时文件", "建议删除", "可清理"))
        summary = build_summary([candidate], ScanStats(root="C:/Temp", visited_files=3))
        self.assertEqual(summary["recommended_count"], 1)
        with tempfile.TemporaryDirectory() as temp_dir:
            output = write_html_report(summary, Path(temp_dir) / "report.html")
            text = output.read_text(encoding="utf-8")
            self.assertIn("AI Disk Assistant", text)
            self.assertIn("1.00 KB", text)


if __name__ == "__main__":
    unittest.main()
