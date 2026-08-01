from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from ai_disk_assistant.ai_advisor import AdvisorError, HybridAdvisor, _validate_advice
from ai_disk_assistant.cache import AdviceCache
from ai_disk_assistant.config import Settings
from ai_disk_assistant.models import FileMetadata


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


if __name__ == "__main__":
    unittest.main()
