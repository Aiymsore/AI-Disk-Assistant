from __future__ import annotations

import json
import re
import time
import urllib.error
import urllib.request
from dataclasses import asdict, dataclass
from itertools import islice
from typing import Any, Iterable, Sequence

from .cache import AdviceCache
from .config import Settings
from .models import ADVICE_LEVELS, PURPOSES, Advice, FileMetadata
from .privacy import metadata_for_ai
from .safety import is_auto_delete_eligible, local_safety_guard


SYSTEM_PROMPT = """你是 Windows 磁盘清理工具中的文件安全建议模块。
你只能根据文件路径、文件名、后缀、大小和时间等元数据判断，不能假设读取过文件内容。

安全原则：
1. 系统文件、程序核心文件、用户文档、媒体文件、代码和备份不得自动建议删除。
2. 只有明显的缓存、临时文件、日志、崩溃转储或下载残留，才能建议删除。
3. 不确定时选择“人工确认”，不得冒险。
4. reason 不超过 60 个中文字符。
5. 对每个输入 id 返回一条结果，不遗漏、不新增。
6. 只输出 JSON 对象，不输出 Markdown。

输出格式：
{"results":[{"id":0,"recommend_delete":false,"purpose":"未知用途","advice_level":"人工确认","reason":"依据不足"}]}

purpose 只能是：缓存文件、临时文件、日志文件、安装包或下载残留、程序配置文件、系统文件、用户文档、媒体文件、代码或项目文件、存档或备份文件、未知用途。
advice_level 只能是：建议删除、谨慎删除、不建议删除、人工确认。
"""


class AdvisorError(RuntimeError):
    pass


class BatchResponseError(AdvisorError):
    """The provider returned malformed or incomplete structured batch output."""


@dataclass(slots=True)
class AdvisorStats:
    api_style: str = ""
    api_calls: int = 0
    api_items: int = 0
    cache_hits: int = 0
    retries: int = 0
    failures: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    elapsed_seconds: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _extract_json(text: str) -> dict[str, Any]:
    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned, flags=re.IGNORECASE)
        cleaned = re.sub(r"\s*```$", "", cleaned)
    try:
        value = json.loads(cleaned)
        if isinstance(value, dict):
            return value
    except json.JSONDecodeError:
        pass

    start = cleaned.find("{")
    end = cleaned.rfind("}")
    if start != -1 and end > start:
        try:
            value = json.loads(cleaned[start : end + 1])
        except json.JSONDecodeError as exc:
            raise AdvisorError("AI 返回内容不是有效 JSON") from exc
        if isinstance(value, dict):
            return value
    raise AdvisorError("AI 返回内容不是有效 JSON")


def _validate_advice(data: dict[str, Any], source: str = "ai") -> Advice:
    required = {"recommend_delete", "purpose", "advice_level", "reason"}
    missing = required - data.keys()
    if missing:
        raise AdvisorError(f"AI 结果缺少字段：{', '.join(sorted(missing))}")
    if not isinstance(data["recommend_delete"], bool):
        raise AdvisorError("recommend_delete 必须是 boolean")
    if not isinstance(data["purpose"], str) or data["purpose"] not in PURPOSES:
        raise AdvisorError("purpose 不在允许范围内")
    if not isinstance(data["advice_level"], str) or data["advice_level"] not in ADVICE_LEVELS:
        raise AdvisorError("advice_level 不在允许范围内")
    if not isinstance(data["reason"], str) or not data["reason"].strip():
        raise AdvisorError("reason 必须是非空字符串")
    if len(data["reason"].strip()) > 120:
        raise AdvisorError("reason 过长")
    return Advice(
        recommend_delete=data["recommend_delete"],
        purpose=data["purpose"],
        advice_level=data["advice_level"],
        reason=data["reason"],
        source=source,
    )


def _coerce_advice(data: dict[str, Any], source: str) -> Advice:
    """Backward-compatible alias retained for external imports; now strictly validates."""
    return _validate_advice(data, source)


def _batched(items: Sequence[Any], size: int) -> Iterable[Sequence[Any]]:
    iterator = iter(items)
    while batch := list(islice(iterator, size)):
        yield batch


def _chat_content_to_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for part in content:
            if isinstance(part, str):
                parts.append(part)
            elif isinstance(part, dict):
                text = part.get("text") or part.get("content")
                if isinstance(text, str):
                    parts.append(text)
                elif isinstance(text, dict) and isinstance(text.get("value"), str):
                    parts.append(text["value"])
        if parts:
            return "\n".join(parts)
    raise AdvisorError("Chat Completions 响应中缺少文本内容")


def _responses_content_to_text(body: dict[str, Any]) -> str:
    # Some compatible gateways expose the SDK-style convenience field in raw JSON.
    direct = body.get("output_text")
    if isinstance(direct, str) and direct.strip():
        return direct

    texts: list[str] = []
    output = body.get("output")
    if isinstance(output, list):
        for item in output:
            if not isinstance(item, dict):
                continue
            content = item.get("content")
            if not isinstance(content, list):
                continue
            for part in content:
                if not isinstance(part, dict):
                    continue
                if part.get("type") not in {"output_text", "text"}:
                    continue
                text = part.get("text")
                if isinstance(text, str):
                    texts.append(text)
                elif isinstance(text, dict) and isinstance(text.get("value"), str):
                    texts.append(text["value"])
    if texts:
        return "\n".join(texts)
    raise AdvisorError("Responses API 响应中缺少 output_text")


def _response_text(body: dict[str, Any], api_style: str) -> str:
    if not isinstance(body, dict):
        raise AdvisorError("AI 接口响应不是 JSON 对象")
    error = body.get("error")
    if error:
        if isinstance(error, dict):
            message = error.get("message") or error.get("code") or json.dumps(error, ensure_ascii=False)
        else:
            message = str(error)
        raise AdvisorError(f"AI 接口返回错误：{message}")

    if api_style == "responses":
        return _responses_content_to_text(body)

    try:
        content = body["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError) as exc:
        raise AdvisorError("Chat Completions 响应结构不符合预期") from exc
    return _chat_content_to_text(content)


class HybridAdvisor:
    """Local safety rules first; AI is advisory, batched, cached and failure-safe."""

    def __init__(
        self,
        settings: Settings | None = None,
        enable_ai: bool = True,
        cache: AdviceCache | None = None,
    ) -> None:
        self.settings = settings or Settings.from_env()
        self.enable_ai = enable_ai
        self.cache = cache
        if self.cache is None and self.settings.ai_cache_path:
            try:
                self.cache = AdviceCache(self.settings.ai_cache_path)
            except OSError:
                self.cache = None
        self.stats = AdvisorStats(api_style=self.settings.ai_api_style)

    @property
    def ai_available(self) -> bool:
        return bool(self.enable_ai and self.settings.ai_api_key and self.settings.ai_model)

    def advise(self, metadata: FileMetadata) -> Advice:
        return self.advise_many([metadata])[0]

    def advise_many(self, metadata_items: Sequence[FileMetadata]) -> list[Advice]:
        """Return production-safe hybrid decisions in input order."""
        if not metadata_items:
            return []

        results: list[Advice | None] = [None] * len(metadata_items)
        ai_indices: list[int] = []
        guards: dict[int, Advice | None] = {}

        for index, metadata in enumerate(metadata_items):
            guard = local_safety_guard(metadata)
            guards[index] = guard
            if guard is not None and guard.source == "local-guard":
                results[index] = guard
            elif not self.ai_available:
                results[index] = guard or Advice(
                    recommend_delete=False,
                    purpose="未知用途",
                    advice_level="人工确认",
                    reason="未配置 AI，且本地规则无法安全确认用途。",
                    source="local-fallback",
                )
            else:
                ai_indices.append(index)

        if ai_indices:
            ai_metadata = [metadata_items[index] for index in ai_indices]
            try:
                raw_advices = self._get_ai_advices(ai_metadata)
            except (AdvisorError, urllib.error.URLError, TimeoutError, OSError, ValueError) as exc:
                self.stats.failures += len(ai_indices)
                for index in ai_indices:
                    guard = guards[index]
                    results[index] = self._fallback_advice(guard, exc)
            else:
                for index, ai_advice in zip(ai_indices, raw_advices, strict=True):
                    results[index] = self._apply_hybrid_guard(metadata_items[index], guards[index], ai_advice)

        return [
            result
            if result is not None
            else Advice(False, "未知用途", "人工确认", "内部状态异常，已安全跳过。", "local-fallback")
            for result in results
        ]

    def advise_ai_only_many(self, metadata_items: Sequence[FileMetadata]) -> list[Advice]:
        """Evaluation-only AI output. It is never used by the cleaner."""
        if not self.ai_available:
            return [
                Advice(False, "未知用途", "人工确认", "未配置 AI，无法执行纯 AI 评测。", "ai-unavailable")
                for _ in metadata_items
            ]
        return self._get_ai_advices(metadata_items)

    def probe(self) -> Advice:
        """Validate credentials, endpoint style and structured output with one harmless item."""
        if not self.ai_available:
            raise AdvisorError("请先在 .env 中配置 AI_API_KEY 和 AI_MODEL")
        payload = {
            "path": "<TEST>/Temp/connection_test.tmp",
            "name": "connection_test.tmp",
            "suffix": ".tmp",
            "parent_folder": "<TEST>/Temp",
            "size_bytes": 128,
            "size_text": "128 B",
            "modified_time": "2025-01-01 00:00:00",
            "accessed_time": "2025-01-01 00:00:00",
        }
        return self._request_ai_batch([payload])[0]

    def _fallback_advice(self, guard: Advice | None, exc: Exception) -> Advice:
        detail = " ".join(str(exc).split())[:70]
        if guard is not None:
            return Advice(
                recommend_delete=guard.recommend_delete,
                purpose=guard.purpose,
                advice_level=guard.advice_level,
                reason=f"{guard.reason}（AI 失败：{detail}；已采用本地规则）",
                source="local-fallback",
            )
        return Advice(
            recommend_delete=False,
            purpose="未知用途",
            advice_level="人工确认",
            reason=f"AI 判断失败，已安全跳过：{detail}",
            source="local-fallback",
        )

    @staticmethod
    def _apply_hybrid_guard(metadata: FileMetadata, guard: Advice | None, ai_advice: Advice) -> Advice:
        if ai_advice.recommend_delete and not is_auto_delete_eligible(metadata):
            return Advice(
                recommend_delete=False,
                purpose=ai_advice.purpose,
                advice_level="谨慎删除",
                reason="AI 倾向清理，但该文件不满足本地自动清理条件。",
                source="hybrid-guarded",
            )
        if guard is not None and guard.advice_level != "建议删除" and ai_advice.recommend_delete:
            return Advice(
                recommend_delete=False,
                purpose=ai_advice.purpose,
                advice_level="谨慎删除",
                reason="AI 倾向清理，但本地规则要求人工确认。",
                source="hybrid-guarded",
            )
        ai_advice.source = "hybrid-cache" if ai_advice.source == "ai-cache" else "hybrid-ai"
        return ai_advice

    def _get_ai_advices(self, metadata_items: Sequence[FileMetadata]) -> list[Advice]:
        output: list[Advice | None] = [None] * len(metadata_items)
        pending: list[tuple[int, FileMetadata, dict[str, Any], str]] = []

        provider_identity = (
            f"{self.settings.ai_base_url}|{self.settings.ai_api_style}|{self.settings.ai_model}"
        )
        for index, metadata in enumerate(metadata_items):
            payload = metadata_for_ai(metadata, self.settings.ai_privacy_mode)
            # Include snapshot values in the key, but not in data sent to AI.
            key_payload = {**payload, "_snapshot": metadata.snapshot()}
            cache_key = AdviceCache.make_key(
                provider_identity,
                self.settings.ai_privacy_mode,
                key_payload,
            )
            cached = self.cache.get(cache_key) if self.cache is not None else None
            if cached is not None:
                cached.source = "ai-cache"
                output[index] = cached
                self.stats.cache_hits += 1
            else:
                pending.append((index, metadata, payload, cache_key))

        for batch in _batched(pending, self.settings.ai_batch_size):
            batch_payloads = [entry[2] for entry in batch]
            advices = self._request_ai_batch_resilient(batch_payloads)
            for (index, _metadata, payload, cache_key), advice in zip(batch, advices, strict=True):
                output[index] = advice
                if self.cache is not None:
                    self.cache.set(cache_key, payload, advice)

        return [
            advice
            if advice is not None
            else Advice(False, "未知用途", "人工确认", "AI 未返回结果。", "local-fallback")
            for advice in output
        ]

    def _request_ai_batch_resilient(self, payloads: Sequence[dict[str, Any]]) -> list[Advice]:
        """Split malformed/unsupported batches so one bad item does not discard all results."""
        try:
            return self._request_ai_batch(payloads)
        except BatchResponseError:
            if len(payloads) <= 1:
                raise
            midpoint = len(payloads) // 2
            left = self._request_ai_batch_resilient(payloads[:midpoint])
            right = self._request_ai_batch_resilient(payloads[midpoint:])
            return left + right

    def _build_request(self, payloads: Sequence[dict[str, Any]]) -> tuple[str, dict[str, Any]]:
        items = [{"id": index, **payload} for index, payload in enumerate(payloads)]
        user_text = "请逐项判断以下文件元数据：\n" + json.dumps(items, ensure_ascii=False)

        if self.settings.ai_api_style == "responses":
            return (
                f"{self.settings.ai_base_url}/responses",
                {
                    "model": self.settings.ai_model,
                    "instructions": SYSTEM_PROMPT,
                    "input": user_text,
                    "store": False,
                },
            )

        return (
            f"{self.settings.ai_base_url}/chat/completions",
            {
                "model": self.settings.ai_model,
                "messages": [
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user", "content": user_text},
                ],
            },
        )

    def _request_ai_batch(self, payloads: Sequence[dict[str, Any]]) -> list[Advice]:
        endpoint, payload = self._build_request(payloads)
        request_data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        last_error: Exception | None = None
        started = time.perf_counter()

        for attempt in range(self.settings.ai_max_retries + 1):
            request = urllib.request.Request(
                endpoint,
                data=request_data,
                headers={
                    "Authorization": f"Bearer {self.settings.ai_api_key}",
                    "Content-Type": "application/json",
                    "Accept": "application/json",
                    "User-Agent": self.settings.ai_user_agent,
                },
                method="POST",
            )
            try:
                self.stats.api_calls += 1
                with urllib.request.urlopen(request, timeout=self.settings.ai_timeout) as response:
                    body = json.loads(response.read().decode("utf-8"))
                break
            except urllib.error.HTTPError as exc:
                detail = exc.read().decode("utf-8", errors="replace")[:500]
                last_error = AdvisorError(
                    f"{self.settings.ai_api_style} 接口返回 HTTP {exc.code}: {detail}"
                )
                retryable = exc.code == 429 or 500 <= exc.code < 600
                if not retryable or attempt >= self.settings.ai_max_retries:
                    raise last_error from exc
            except (urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError) as exc:
                last_error = exc
                if attempt >= self.settings.ai_max_retries:
                    raise AdvisorError(
                        f"{self.settings.ai_api_style} 请求失败：{exc}"
                    ) from exc

            self.stats.retries += 1
            delay = self.settings.ai_retry_backoff * (2**attempt)
            if delay:
                time.sleep(delay)
        else:  # pragma: no cover - defensive branch
            raise AdvisorError(f"AI 请求失败：{last_error}")

        self.stats.elapsed_seconds += time.perf_counter() - started
        self.stats.api_items += len(payloads)
        usage = body.get("usage", {}) if isinstance(body, dict) else {}
        if isinstance(usage, dict):
            self.stats.prompt_tokens += int(
                usage.get("input_tokens", usage.get("prompt_tokens", 0)) or 0
            )
            self.stats.completion_tokens += int(
                usage.get("output_tokens", usage.get("completion_tokens", 0)) or 0
            )

        content = _response_text(body, self.settings.ai_api_style)
        try:
            data = _extract_json(content)
            raw_results = data.get("results")
            if not isinstance(raw_results, list):
                # Some compatible services return one bare object for a one-item batch.
                if len(payloads) == 1 and {
                    "recommend_delete",
                    "purpose",
                    "advice_level",
                    "reason",
                } <= data.keys():
                    raw_results = [{"id": 0, **data}]
                else:
                    raise BatchResponseError("AI 结果缺少 results 数组")
            if len(raw_results) != len(payloads):
                raise BatchResponseError("AI 返回数量与输入数量不一致")

            indexed: dict[int, Advice] = {}
            for raw in raw_results:
                if not isinstance(raw, dict) or not isinstance(raw.get("id"), int):
                    raise BatchResponseError("AI 每条结果必须包含整数 id")
                item_id = raw["id"]
                if item_id in indexed or not 0 <= item_id < len(payloads):
                    raise BatchResponseError("AI 返回了重复或越界 id")
                indexed[item_id] = _validate_advice(raw, source="ai")
            if len(indexed) != len(payloads):
                raise BatchResponseError("AI 返回 id 不完整")
            return [indexed[index] for index in range(len(payloads))]
        except BatchResponseError:
            raise
        except AdvisorError as exc:
            raise BatchResponseError(str(exc)) from exc
