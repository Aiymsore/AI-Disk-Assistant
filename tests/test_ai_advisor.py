from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from ai_disk_assistant.ai_advisor import (
    SYSTEM_PROMPT,
    UNIT_SYSTEM_PROMPT,
    AdvisorError,
    HybridAdvisor,
    _validate_advice,
    _validate_unit_evidence,
)
from ai_disk_assistant.cache import AdviceCache
from ai_disk_assistant.config import Settings
from ai_disk_assistant.models import ADVICE_LEVELS, PURPOSES, FileMetadata, Unit
from ai_disk_assistant.privacy import unit_payload_for_ai


class FakeResponse:
    def __init__(self, body: dict) -> None:
        self.body = json.dumps(body, ensure_ascii=False).encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def read(self) -> bytes:
        return self.body


def metadata(path: str = r"C:\Users\Test\AppData\Local\Temp\old.tmp") -> FileMetadata:
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
        modified_time="2025-01-01 00:00:00",
        accessed_time="2025-01-01 00:00:00",
        modified_time_ns=1,
    )


class AdvisorTests(unittest.TestCase):
    def test_schema_rejects_string_boolean(self) -> None:
        with self.assertRaises(AdvisorError):
            _validate_advice(
                {
                    "recommend_delete": "true",
                    "purpose": "临时文件",
                    "advice_level": "建议删除",
                    "reason": "临时文件",
                }
            )

    def test_batch_response_and_cache(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            settings = Settings(
                "secret",
                "https://example.invalid/v1",
                "demo-model",
                1,
                ai_batch_size=10,
                ai_max_retries=0,
                ai_cache_path=str(Path(temp_dir) / "cache.sqlite3"),
                ai_privacy_mode="balanced",
            )
            body = {
                "choices": [
                    {
                        "message": {
                            "content": json.dumps(
                                {
                                    "results": [
                                        {
                                            "id": 0,
                                            "recommend_delete": True,
                                            "purpose": "临时文件",
                                            "advice_level": "建议删除",
                                            "reason": "位于临时目录。",
                                        }
                                    ]
                                },
                                ensure_ascii=False,
                            )
                        }
                    }
                ],
                "usage": {"prompt_tokens": 20, "completion_tokens": 10},
            }
            advisor = HybridAdvisor(settings, cache=AdviceCache(settings.ai_cache_path))
            with patch("urllib.request.urlopen", return_value=FakeResponse(body)) as mocked:
                first = advisor.advise(metadata())
                second = advisor.advise(metadata())
            self.assertTrue(first.recommend_delete)
            self.assertEqual(second.source, "hybrid-cache")
            self.assertEqual(mocked.call_count, 1)
            self.assertEqual(advisor.stats.cache_hits, 1)
            self.assertEqual(advisor.stats.prompt_tokens, 20)

    def test_malformed_batch_is_split(self) -> None:
        settings = Settings(
            "secret",
            "https://example.invalid/v1",
            "demo",
            1,
            ai_batch_size=2,
            ai_max_retries=0,
            ai_cache_path="",
        )
        malformed = FakeResponse(
            {"choices": [{"message": {"content": '{"results": []}'}}]}
        )

        def single_response(item_id: int) -> FakeResponse:
            return FakeResponse(
                {
                    "choices": [
                        {
                            "message": {
                                "content": json.dumps(
                                    {
                                        "results": [
                                            {
                                                "id": item_id,
                                                "recommend_delete": True,
                                                "purpose": "临时文件",
                                                "advice_level": "建议删除",
                                                "reason": "位于临时目录。",
                                            }
                                        ]
                                    },
                                    ensure_ascii=False,
                                )
                            }
                        }
                    ]
                }
            )

        advisor = HybridAdvisor(settings)
        first = metadata(r"C:\Users\Test\AppData\Local\Temp\one.tmp")
        second = metadata(r"C:\Users\Test\AppData\Local\Temp\two.tmp")
        with patch(
            "urllib.request.urlopen",
            side_effect=[malformed, single_response(0), single_response(0)],
        ) as mocked:
            results = advisor.advise_many([first, second])
        self.assertEqual(mocked.call_count, 3)
        self.assertTrue(all(result.recommend_delete for result in results))


    def test_responses_api_request_and_output_parsing(self) -> None:
        settings = Settings(
            "secret",
            "https://gateway.example/v1",
            "demo-responses",
            1,
            ai_api_style="responses",
            ai_max_retries=0,
            ai_cache_path="",
        )
        response_text = json.dumps(
            {
                "results": [
                    {
                        "id": 0,
                        "recommend_delete": True,
                        "purpose": "临时文件",
                        "advice_level": "建议删除",
                        "reason": "位于临时目录。",
                    }
                ]
            },
            ensure_ascii=False,
        )
        body = {
            "status": "completed",
            "output": [
                {
                    "type": "message",
                    "content": [{"type": "output_text", "text": response_text}],
                }
            ],
            "usage": {"input_tokens": 31, "output_tokens": 17},
        }
        advisor = HybridAdvisor(settings)
        with patch("urllib.request.urlopen", return_value=FakeResponse(body)) as mocked:
            result = advisor.advise(metadata())

        request = mocked.call_args.args[0]
        sent = json.loads(request.data.decode("utf-8"))
        self.assertEqual(request.full_url, "https://gateway.example/v1/responses")
        self.assertEqual(sent["model"], "demo-responses")
        self.assertIn("instructions", sent)
        self.assertIn("input", sent)
        self.assertIs(sent["store"], False)
        self.assertEqual(result.source, "hybrid-ai")
        self.assertEqual(advisor.stats.api_style, "responses")
        self.assertEqual(advisor.stats.prompt_tokens, 31)
        self.assertEqual(advisor.stats.completion_tokens, 17)

    def test_responses_api_accepts_top_level_output_text(self) -> None:
        settings = Settings(
            "secret",
            "https://gateway.example/v1",
            "demo-responses",
            1,
            ai_api_style="responses",
            ai_max_retries=0,
            ai_cache_path="",
        )
        body = {
            "output_text": json.dumps(
                {
                    "results": [
                        {
                            "id": 0,
                            "recommend_delete": False,
                            "purpose": "未知用途",
                            "advice_level": "人工确认",
                            "reason": "依据不足。",
                        }
                    ]
                },
                ensure_ascii=False,
            )
        }
        advisor = HybridAdvisor(settings)
        with patch("urllib.request.urlopen", return_value=FakeResponse(body)):
            result = advisor.advise(metadata())
        self.assertFalse(result.recommend_delete)
        self.assertEqual(result.source, "hybrid-ai")

    def test_ai_cannot_override_user_document_guard(self) -> None:
        settings = Settings("secret", "https://example.invalid/v1", "demo", 1, ai_cache_path="")
        advisor = HybridAdvisor(settings)
        result = advisor.advise(metadata(r"C:\Users\Test\Documents\report.docx"))
        self.assertFalse(result.recommend_delete)
        self.assertEqual(result.source, "local-guard")


class PromptEnumSyncTests(unittest.TestCase):
    """维护守则：PURPOSES / ADVICE_LEVELS 变更必须同步结构化提示词的枚举清单。"""

    def test_structured_prompts_list_all_enums(self) -> None:
        for prompt in (SYSTEM_PROMPT, UNIT_SYSTEM_PROMPT):
            for purpose in sorted(PURPOSES):
                self.assertIn(purpose, prompt, f"{purpose!r} 缺失于提示词枚举清单")
            for level in sorted(ADVICE_LEVELS):
                self.assertIn(level, prompt, f"{level!r} 缺失于提示词枚举清单")

    def test_unit_prompt_evidence_examples_use_real_payload_fields(self) -> None:
        # 证据闸门按载荷字段名核对：提示词里的示例字段必须真实存在，否则示例本身就会降级。
        unit = Unit("fp", r"C:\Temp", ".tmp", "小(≤1MB)", 3, 3000)
        payload = unit_payload_for_ai(unit, "balanced")
        for field in ("suffix", "file_count", "path_pattern"):
            self.assertIn(f"{field}=", UNIT_SYSTEM_PROMPT)
            self.assertIn(field, payload)


class EvidenceLengthTests(unittest.TestCase):
    """回归：路径型证据的真实取值长度必须能通过校验。

    事故复盘：上限 80 时，AI 如实引用深层目录的 path_pattern（90+ 字符）被整批拒绝，
    二分重试全败后全部候选兜底成"人工确认"，AI 判读形同虚设。
    """

    def test_long_path_evidence_passes_validation_and_gate(self) -> None:
        deep = r"D:\百度网盘\BaiduNetdisk\module\BrowserEngine\BrowserEngine\resources\web\locales"
        evidence = [f"path_pattern={deep}"]
        self.assertGreater(len(evidence[0]), 80)  # 旧上限（80）必然拒掉的长度
        advice = _validate_advice(
            {
                "recommend_delete": False,
                "purpose": "程序配置文件",
                "advice_level": "人工确认",
                "reason": "程序目录中的运行库",
                "evidence": evidence,
            }
        )
        self.assertEqual(advice.evidence, evidence)
        self.assertTrue(_validate_unit_evidence({"path_pattern": deep}, evidence))


class DescribeTaskTests(unittest.TestCase):
    """文件介绍任务：结构化校验、结果对齐、缓存复用、失败语义。"""

    def _advisor(self, temp_dir: str) -> HybridAdvisor:
        settings = Settings(
            "secret",
            "https://example.invalid/v1",
            "demo",
            1,
            ai_max_retries=0,
            ai_retry_backoff=0.0,
            ai_cache_path=f"{temp_dir}/c.sqlite3",
        )
        return HybridAdvisor(settings)

    def test_describe_items_aligned_and_cached(self) -> None:
        payloads = [
            {"kind": "文件", "name": "libcef.dll", "path": "D:\\App", "suffix": ".dll", "size_bytes": 1},
            {"kind": "目录", "name": "Cache", "path": "D:\\App\\Cache",
             "total_size_bytes": 10, "file_count": 2, "top_suffixes": []},
        ]
        body = {
            "choices": [
                {
                    "message": {
                        "content": json.dumps(
                            {
                                "descriptions": [
                                    {"id": 0, "what": "CEF 浏览器框架库", "impact": "程序无法启动", "handle": "别动"},
                                    {"id": 1, "what": "应用缓存目录", "impact": "可重新生成", "handle": "可清理"},
                                ]
                            },
                            ensure_ascii=False,
                        )
                    }
                }
            ],
            "usage": {},
        }
        with tempfile.TemporaryDirectory() as temp_dir:
            advisor = self._advisor(temp_dir)
            with patch("urllib.request.urlopen", return_value=FakeResponse(body)):
                first = advisor.describe_items(payloads)
            self.assertEqual([item.handle for item in first], ["别动", "可清理"])
            self.assertEqual(first[0].what, "CEF 浏览器框架库")
            self.assertEqual(advisor.stats.api_calls, 1)

            with patch("urllib.request.urlopen") as mocked:
                second = advisor.describe_items(payloads)
            mocked.assert_not_called()
            self.assertEqual(second, first)
            self.assertEqual(advisor.stats.cache_hits, 2)

    def test_invalid_handle_rejected(self) -> None:
        body = {
            "choices": [
                {
                    "message": {
                        "content": json.dumps(
                            {"descriptions": [{"id": 0, "what": "x", "impact": "y", "handle": "建议删除"}]},
                            ensure_ascii=False,
                        )
                    }
                }
            ],
            "usage": {},
        }
        with tempfile.TemporaryDirectory() as temp_dir:
            advisor = self._advisor(temp_dir)
            with patch("urllib.request.urlopen", return_value=FakeResponse(body)):
                with self.assertRaises(AdvisorError):
                    advisor.describe_items(
                        [{"kind": "文件", "name": "a", "path": "C:\\a", "suffix": ".a", "size_bytes": 1}]
                    )

    def test_without_ai_raises(self) -> None:
        settings = Settings(None, "https://example.invalid/v1", "demo", 1, ai_cache_path="")
        advisor = HybridAdvisor(settings, enable_ai=True)
        with self.assertRaises(AdvisorError):
            advisor.describe_items([{"kind": "文件", "name": "a", "path": "C:\\a"}])


if __name__ == "__main__":
    unittest.main()
