from __future__ import annotations

import json
import tempfile
import unittest
from unittest.mock import patch

from ai_disk_assistant.ai_advisor import HybridAdvisor
from ai_disk_assistant.config import Settings
from ai_disk_assistant.models import FileMetadata, Unit


def metadata(path: str) -> FileMetadata:
    normalized = path.replace("\\", "/")
    name = normalized.rsplit("/", 1)[-1]
    suffix = "." + name.rsplit(".", 1)[-1].lower() if "." in name else ""
    return FileMetadata(
        path=path,
        name=name,
        suffix=suffix,
        parent_folder=normalized.rsplit("/", 1)[0],
        size_bytes=100,
        size_text="100 B",
        modified_time="2020-01-01 00:00:00",
        accessed_time="2020-01-01 00:00:00",
        modified_time_ns=1,
    )


def unit(parent: str, suffix: str, names: list[str]) -> Unit:
    members = [metadata(f"{parent}\\{name}") for name in names]
    return Unit(
        unit_id="test-fingerprint",
        parent_folder=parent,
        suffix=suffix,
        size_bucket="<10KB",
        file_count=len(members),
        total_size=100 * len(members),
        members=members,
    )


class FakeResponse:
    def __init__(self, body: dict) -> None:
        self.body = json.dumps(body, ensure_ascii=False).encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def read(self) -> bytes:
        return self.body


def ai_body(results: list[dict]) -> dict:
    return {"choices": [{"message": {"content": json.dumps({"results": results}, ensure_ascii=False)}}]}


def agree_body(suffix: str, count: int) -> dict:
    return ai_body(
        [
            {
                "id": 0,
                "recommend_delete": True,
                "purpose": "日志文件" if suffix == ".log" else "临时文件",
                "advice_level": "建议删除",
                "reason": "垃圾残留，可清理。",
                "evidence": [f"suffix={suffix}", f"file_count={count}"],
            }
        ]
    )


class AdviseUnitsTests(unittest.TestCase):
    def test_obvious_junk_unit_goes_through_ai_and_agrees(self) -> None:
        """典型垃圾单元也交给 AI 独立确认；AI 同意则给"建议删除"。"""
        with tempfile.TemporaryDirectory() as temp_dir:
            settings = Settings("key", "https://example.invalid/v1", "demo", 1, ai_cache_path=f"{temp_dir}/c.sqlite3")
            advisor = HybridAdvisor(settings)
            units = [unit(r"C:\Users\T\AppData\Local\Temp", ".tmp", ["a.tmp", "b.tmp"])]
            with patch(
                "ai_disk_assistant.ai_advisor.urllib.request.urlopen",
                return_value=FakeResponse(agree_body(".tmp", 2)),
            ):
                advices = advisor.advise_units(units)
            self.assertEqual(advisor.stats.api_calls, 1)
            self.assertEqual(advices[0].advice_level, "建议删除")
            self.assertTrue(advices[0].recommend_delete)
            self.assertEqual(advices[0].source, "unit-hybrid-ai")

    def test_without_ai_local_rule_resolves_obvious_junk(self) -> None:
        """未配置 AI 时，缓存/临时目录中的直判后缀由本地规则给出"建议删除"。"""
        with tempfile.TemporaryDirectory() as temp_dir:
            settings = Settings(None, "https://example.invalid/v1", "demo", 1, ai_cache_path=f"{temp_dir}/c.sqlite3")
            advisor = HybridAdvisor(settings, enable_ai=False)
            units = [unit(r"C:\Users\T\AppData\Local\Temp", ".tmp", ["a.tmp"])]
            advices = advisor.advise_units(units)
            self.assertEqual(advices[0].advice_level, "建议删除")
            self.assertTrue(advices[0].recommend_delete)
            self.assertEqual(advices[0].source, "local-rule")

    def test_local_guard_unit_never_reaches_ai(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            settings = Settings("key", "https://example.invalid/v1", "demo", 1, ai_cache_path=f"{temp_dir}/c.sqlite3")
            advisor = HybridAdvisor(settings)
            units = [unit(r"C:\Windows\System32", ".log", ["setup.log"])]
            with patch("ai_disk_assistant.ai_advisor.urllib.request.urlopen") as mocked:
                advices = advisor.advise_units(units)
            mocked.assert_not_called()
            self.assertEqual(advices[0].advice_level, "不建议删除")
            self.assertEqual(advisor.stats.api_calls, 0)

    def test_ai_delete_for_guard_silent_unit(self) -> None:
        """守卫放行的未知类型：AI 的"建议删除"不再被降级——这是激进化的主路径。"""
        body = agree_body(".log", 2)
        with tempfile.TemporaryDirectory() as temp_dir:
            settings = Settings("key", "https://example.invalid/v1", "demo", 1, ai_cache_path=f"{temp_dir}/c.sqlite3")
            advisor = HybridAdvisor(settings)
            units = [unit(r"C:\app\trace", ".log", ["a.log", "b.log"])]
            with patch(
                "ai_disk_assistant.ai_advisor.urllib.request.urlopen", return_value=FakeResponse(body)
            ):
                first = advisor.advise_units(units)
            self.assertEqual(first[0].advice_level, "建议删除")
            self.assertTrue(first[0].recommend_delete)
            self.assertEqual(first[0].source, "unit-hybrid-ai")
            self.assertIn("suffix=.log", first[0].evidence)
            self.assertEqual(advisor.stats.api_calls, 1)

            with patch(
                "ai_disk_assistant.ai_advisor.urllib.request.urlopen", return_value=FakeResponse(body)
            ) as mocked:
                second = advisor.advise_units(units)
            mocked.assert_not_called()
            self.assertEqual(second[0].advice_level, "建议删除")
            self.assertEqual(second[0].source, "unit-hybrid-cache")
            self.assertEqual(advisor.stats.api_calls, 1)
            self.assertEqual(advisor.stats.cache_hits, 1)

    def test_ai_unit_with_fabricated_evidence_is_downgraded(self) -> None:
        body = ai_body(
            [
                {
                    "id": 0,
                    "recommend_delete": True,
                    "purpose": "日志文件",
                    "advice_level": "建议删除",
                    "reason": "编造依据的判断。",
                    "evidence": ["age_min_days=1"],
                }
            ]
        )
        with tempfile.TemporaryDirectory() as temp_dir:
            settings = Settings("key", "https://example.invalid/v1", "demo", 1, ai_cache_path=f"{temp_dir}/c.sqlite3")
            advisor = HybridAdvisor(settings)
            units = [unit(r"C:\app\trace", ".log", ["a.log"])]
            with patch("ai_disk_assistant.ai_advisor.urllib.request.urlopen", return_value=FakeResponse(body)):
                advices = advisor.advise_units(units)
            self.assertEqual(advices[0].advice_level, "人工确认")
            self.assertFalse(advices[0].recommend_delete)
            self.assertEqual(advices[0].source, "unit-fallback")

    def test_unit_without_ai_and_guard_falls_back(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            settings = Settings(None, "https://example.invalid/v1", "demo", 1, ai_cache_path=f"{temp_dir}/c.sqlite3")
            advisor = HybridAdvisor(settings, enable_ai=False)
            units = [unit(r"C:\app\trace", ".log", ["a.log"])]
            advices = advisor.advise_units(units)
            self.assertEqual(advices[0].advice_level, "人工确认")
            self.assertFalse(advices[0].recommend_delete)


if __name__ == "__main__":
    unittest.main()
