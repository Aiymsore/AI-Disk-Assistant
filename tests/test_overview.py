from __future__ import annotations

import json
import tempfile
import unittest
import urllib.error
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch

from ai_disk_assistant.ai_advisor import HybridAdvisor
from ai_disk_assistant.config import Settings
from ai_disk_assistant.inventory import Inventory
from ai_disk_assistant.overview import build_overview_payload
from ai_disk_assistant.privacy import anonymize_path
from ai_disk_assistant.report import build_summary, markdown_to_html, write_all_reports
from tests.test_inventory import seed_snapshot


def markdown_response(text: str):
    """构造一个 Chat Completions 响应，正文为自由文本（非 JSON）。"""

    class _Resp:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def read(self):
            return json.dumps(
                {"choices": [{"message": {"content": text}}], "usage": {"prompt_tokens": 10}}, 
                ensure_ascii=False,
            ).encode("utf-8")

    return _Resp()


class OverviewPayloadTests(unittest.TestCase):
    """载荷层：范围总量、排名、最大文件、重复组、三档隐私。"""

    def setUp(self) -> None:
        self._stack = ExitStack()
        self.temp_dir = Path(self._stack.enter_context(tempfile.TemporaryDirectory())).resolve()
        inv_dir = Path(self._stack.enter_context(tempfile.TemporaryDirectory()))
        self.inventory_path = inv_dir / "inv.sqlite3"
        self.addCleanup(self._stack.close)
        self.files = [
            ("big/a.log", 3 * 1024 * 1024),
            ("big/b.log", 4 * 1024 * 1024),
            ("big/sub/c.log", 1024 * 1024),
            ("small/note.txt", 4096),
            ("dup/x1.iso", 2 * 1024 * 1024),
            ("dup/x2.iso", 2 * 1024 * 1024),
        ]
        self.dirs = ["big", "big/sub", "small", "dup"]

    def _payload(self, privacy_mode: str) -> dict:
        inventory = Inventory(self.inventory_path)
        snapshot_id = seed_snapshot(inventory, self.temp_dir, self.files, self.dirs)
        return build_overview_payload(
            inventory, snapshot_id, str(self.temp_dir), privacy_mode=privacy_mode
        )

    def test_payload_reports_ranking_and_duplicates(self) -> None:
        payload = self._payload("full")
        self.assertEqual(payload["scope"]["file_count"], len(self.files))
        self.assertEqual(payload["scope"]["path"], str(self.temp_dir))
        self.assertIn("MFT", payload["data_source"])

        subdirs = {entry["name"] for entry in payload["largest_subdirectories"]}
        self.assertEqual(subdirs, {"big", "small", "dup"})
        # 深层热点包含 big 之下的 sub（只看直接子目录会漏掉嵌套大户）。
        hosts = {entry["name"] for entry in payload["hotspot_directories"]}
        self.assertIn("sub", hosts)

        suffixes = {entry["suffix"]: entry for entry in payload["largest_suffixes"]}
        self.assertEqual(suffixes[".log"]["file_count"], 3)
        self.assertEqual(payload["largest_files"][0]["name"], "b.log")
        # 只有两个 2MB 的 .iso 同体积；.log 三兄弟体积互不相同，不构成重复组。
        self.assertEqual(len(payload["duplicate_groups"]), 1)
        self.assertEqual(payload["duplicate_groups"][0]["count"], 2)
        self.assertEqual(payload["duplicate_groups"][0]["size_text"], "2.00 MB")

    def test_strict_privacy_sends_no_paths(self) -> None:
        payload = self._payload("strict")
        self.assertNotIn("path", payload["scope"])
        self.assertIn("path_depth", payload["scope"])
        for entry in payload["largest_subdirectories"] + payload["largest_files"]:
            self.assertNotIn("path", entry)
        for group in payload.get("duplicate_groups", []):
            self.assertEqual(group["samples"], [])

    def test_balanced_privacy_anonymizes_paths(self) -> None:
        payload = self._payload("balanced")
        self.assertEqual(payload["scope"]["path"], anonymize_path(str(self.temp_dir)))

    def test_review_block_only_present_with_candidates(self) -> None:
        inventory = Inventory(self.inventory_path)
        snapshot_id = seed_snapshot(inventory, self.temp_dir, self.files, self.dirs)
        empty = build_overview_payload(inventory, snapshot_id, str(self.temp_dir), review=None)
        self.assertNotIn("review", empty)


class SummarizeOverviewTests(unittest.TestCase):
    """综述调用：自由文本、缓存、失败降级。"""

    def _advisor(self, temp_dir: str, *, enabled: bool = True) -> HybridAdvisor:
        settings = Settings(
            "key" if enabled else None,
            "https://example.invalid/v1",
            "demo",
            1,
            ai_max_retries=0,
            ai_retry_backoff=0.0,
            ai_cache_path=f"{temp_dir}/c.sqlite3",
        )
        return HybridAdvisor(settings, enable_ai=enabled)

    def test_returns_markdown_and_reuses_cache(self) -> None:
        narrative = "## 总体判断\n日志占了 7 MB，主要是可清理的轮转日志。\n\n## 建议处理顺序\n1. 先核对 big 目录。"
        with tempfile.TemporaryDirectory() as temp_dir:
            advisor = self._advisor(temp_dir)
            facts = {"scope": {"size_text": "13 MB", "file_count": 6}}
            with patch(
                "ai_disk_assistant.ai_advisor.urllib.request.urlopen",
                return_value=markdown_response(narrative),
            ):
                first = advisor.summarize_overview(facts)
            self.assertEqual(first, narrative)
            self.assertEqual(advisor.stats.api_calls, 1)

            with patch("ai_disk_assistant.ai_advisor.urllib.request.urlopen") as mocked:
                second = advisor.summarize_overview(facts)
            mocked.assert_not_called()
            self.assertEqual(second, narrative)
            self.assertEqual(advisor.stats.cache_hits, 1)

    def test_strips_code_fence(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            advisor = self._advisor(temp_dir)
            body = "```markdown\n## 总体判断\n正文。\n```"
            with patch(
                "ai_disk_assistant.ai_advisor.urllib.request.urlopen",
                return_value=markdown_response(body),
            ):
                text = advisor.summarize_overview({"scope": {}})
            self.assertTrue(text.startswith("## 总体判断"))
            self.assertNotIn("```", text)

    def test_request_failure_returns_none_without_raising(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            advisor = self._advisor(temp_dir)
            with patch(
                "ai_disk_assistant.ai_advisor.urllib.request.urlopen",
                side_effect=urllib.error.URLError("boom"),
            ):
                self.assertIsNone(advisor.summarize_overview({"scope": {}}))
            self.assertEqual(advisor.stats.failures, 1)

    def test_without_ai_returns_none_without_request(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            advisor = self._advisor(temp_dir, enabled=False)
            with patch("ai_disk_assistant.ai_advisor.urllib.request.urlopen") as mocked:
                self.assertIsNone(advisor.summarize_overview({"scope": {}}))
            mocked.assert_not_called()

    def test_empty_facts_short_circuits(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            advisor = self._advisor(temp_dir)
            with patch("ai_disk_assistant.ai_advisor.urllib.request.urlopen") as mocked:
                self.assertIsNone(advisor.summarize_overview({}))
            mocked.assert_not_called()


class ReportNarrativeTests(unittest.TestCase):
    def test_markdown_subset_is_escaped_and_rendered(self) -> None:
        # 报告页已有 <h1>/<h2> 层级，综述里的 "##" 落在 h3，保持语义嵌套。
        rendered = markdown_to_html("## 标题\n- 项目 **加粗**\n1. 步骤\n<script>alert(1)</script>")
        self.assertIn("<h3>标题</h3>", rendered)
        self.assertIn("<ul>", rendered)
        self.assertIn("<strong>加粗</strong>", rendered)
        self.assertIn("<ol>", rendered)
        self.assertNotIn("<script>", rendered)
        self.assertIn("&lt;script&gt;", rendered)

    def test_narrative_lands_in_summary_html_and_markdown(self) -> None:
        narrative = "## 总体判断\n缓存目录占了 4 GB，可优先处理。"
        summary = build_summary([], None, None, narrative)
        self.assertEqual(summary["narrative"], narrative)
        with tempfile.TemporaryDirectory() as temp_dir:
            paths = write_all_reports(
                [],
                None,
                None,
                csv_path=Path(temp_dir) / "r.csv",
                summary_json_path=Path(temp_dir) / "r.json",
                html_path=Path(temp_dir) / "r.html",
                narrative=narrative,
                overview_md_path=Path(temp_dir) / "r.md",
            )
            self.assertIsNotNone(paths.overview)
            markdown = (Path(temp_dir) / "r.md").read_text(encoding="utf-8")
            self.assertIn("缓存目录占了 4 GB", markdown)
            page = (Path(temp_dir) / "r.html").read_text(encoding="utf-8")
            self.assertIn("AI 深度分析", page)
            self.assertIn("缓存目录占了 4 GB", page)
            self.assertIn("<h3>总体判断</h3>", page)

    def test_without_narrative_no_overview_artifact(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            paths = write_all_reports(
                [],
                None,
                None,
                csv_path=Path(temp_dir) / "r.csv",
                summary_json_path=Path(temp_dir) / "r.json",
                html_path=Path(temp_dir) / "r.html",
            )
            self.assertIsNone(paths.overview)
            self.assertNotIn("AI 深度分析", (Path(temp_dir) / "r.html").read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
