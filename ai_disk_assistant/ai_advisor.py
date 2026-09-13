"""AI 决策层：混合顾问 HybridAdvisor——本地安全规则优先，AI 仅做建议且失败可降级。

职责链：请求前隐私裁剪（privacy）→ 缓存查询（cache）→ 批量 HTTP 调用（重试/二分降级）→
严格校验 AI 返回（_validate_advice）→ 混合守卫降级（_apply_hybrid_guard）。
被 scanner.py（扫描时批量判断）、cli.py / gui.py（经 build_advisor 构造）使用。
"""

from __future__ import annotations

import json
import re
import time
import urllib.error
import urllib.request
from dataclasses import asdict, dataclass, replace
from itertools import islice
from typing import Any, Iterable, Sequence

from .cache import AdviceCache
from .config import Settings
from .models import (
    ADVICE_LEVELS,
    EVIDENCE_MAX_ITEMS,
    EVIDENCE_MAX_LENGTH,
    PURPOSES,
    REASON_MAX_LENGTH,
    Advice,
    FileMetadata,
    Unit,
    safe_fallback_advice,
)
from .privacy import metadata_for_ai, unit_payload_for_ai
from .safety import (
    RULES_VERSION,
    decide_unit_advice,
    local_safety_guard,
)


# ── 系统提示词：purpose / advice_level 的枚举清单与 models.py 的常量保持一致 ──
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

# 区域选择的 reason 长度上限（suggest_areas 解析时截断）。
_AREA_REASON_MAX_LENGTH = 60

# 重复组判读的合法结论集合。
DUPLICATE_VERDICTS = {"likely", "possible", "unlikely"}

DUPLICATE_SYSTEM_PROMPT = """你是 Windows 磁盘清理工具中的重复文件判读模块。
输入是若干"同体积文件组"：体积完全相同的文件聚合（同体积可能巧合，未必同内容）。
你的任务：根据路径模式与文件名，判读每组更可能是哪种情况，仅作提示：
- likely：大概率是真重复（下载残留、副本、旧版本散落多处）
- possible：有重复迹象但证据不足
- unlikely：更可能是巧合同体积或各有所用（如系统组件、数据分片）
不得输出删除建议；真正的去重必须由用户核对内容后自行决定。
规则：
1. 只输出 JSON 对象，不输出 Markdown。
2. 对每个输入 id 返回一条结果，不遗漏、不新增。
3. verdict 只能是 likely / possible / unlikely。
4. reason 不超过 40 个中文字符。
输出格式：
{"reviews":[{"id":0,"verdict":"likely","reason":"同名安装包散落多处"}]}
"""

ANALYZE_SYSTEM_PROMPT = """你是 Windows 磁盘清理工具中的目录区域判读模块。
输入是一次磁盘扫描中体积最大的目录聚合列表（路径模式、总大小、文件数、主要后缀）。
你的任务：挑出值得深入分析的可疑区域——通常是缓存、临时文件、日志、崩溃转储、
下载残留、安装包或过期备份集中的目录。
不要挑选：系统或程序目录、明显的用户文档/项目/媒体库、整个盘根或单一文件。
规则：
1. 只输出 JSON 对象，不输出 Markdown。
2. areas 数组最少 1 个、最多 8 个，按可疑程度降序。
3. reason 不超过 40 个中文字符，说明为什么可疑。
4. id 必须原样引用输入中的 id，不得新增或改写。
输出格式：
{"areas":[{"id":0,"reason":"日志缓存密集，体积大"}]}
"""

# 判定单元提示词版本：进入单元判定缓存键。任何提示词变更都必须递增，旧缓存自动失效。
PROMPT_VERSION = "unit-prompt-3"

UNIT_SYSTEM_PROMPT = """你是 Windows 磁盘清理工具中的文件组安全建议模块。
输入是"判定单元"：同一目录下、同后缀、大小同档的一组文件的聚合统计。
你只能根据输入给出的统计字段判断，不能假设读取过文件内容。

安全原则：
1. 系统文件、程序核心文件、用户文档、媒体文件、代码和备份不得建议删除。
2. 除此之外可以适度积极：缓存、临时文件、日志、崩溃转储、下载残留、安装包、
   过期压缩包，以及看起来已被遗弃的文件，在证据支持时可以直接建议删除。
3. 证据不足以支持"建议删除"时用"谨慎删除"，再弱则"人工确认"，不得冒险。
4. evidence 必须从输入字段中原样引用至少 1 条，格式为"字段名=值"，不得编造输入中没有的事实。
5. reason 不超过 60 个中文字符。
6. 对每个输入 id 返回一条结果，不遗漏、不新增。
7. 只输出 JSON 对象，不输出 Markdown。

输出格式：
{"results":[{"id":0,"recommend_delete":false,"purpose":"未知用途","advice_level":"人工确认","reason":"依据不足","evidence":["suffix=.tmp"]}]}

purpose 只能是：缓存文件、临时文件、日志文件、安装包或下载残留、程序配置文件、系统文件、用户文档、媒体文件、代码或项目文件、存档或备份文件、未知用途。
advice_level 只能是：建议删除、谨慎删除、不建议删除、人工确认。
"""


# ── 错误类型与运行统计 ───────────────────────────────────────────────────
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
    # 有意保留：token 计数与耗时不在 CLI/GUI 展示，仅随 summary JSON 落盘供事后核对。
    prompt_tokens: int = 0
    completion_tokens: int = 0
    elapsed_seconds: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


# ── AI 返回解析：JSON 提取 / 严格校验 / 两种协议的文本抽取 ────────────────
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
    if len(data["reason"].strip()) > REASON_MAX_LENGTH:
        raise AdvisorError("reason 过长")
    # evidence 可选（兼容旧的逐文件提示词），但出现时必须是短字符串数组。
    evidence = data.get("evidence", [])
    if not isinstance(evidence, list) or not all(isinstance(item, str) for item in evidence):
        raise AdvisorError("evidence 必须是字符串数组")
    if len(evidence) > EVIDENCE_MAX_ITEMS:
        raise AdvisorError("evidence 条数超限")
    if any(not item.strip() or len(item) > EVIDENCE_MAX_LENGTH for item in evidence):
        raise AdvisorError("evidence 存在空项或超长项")
    return Advice(
        recommend_delete=data["recommend_delete"],
        purpose=data["purpose"],
        advice_level=data["advice_level"],
        reason=data["reason"],
        source=source,
        evidence=evidence,
    )


def _validate_unit_evidence(payload: dict[str, Any], evidence: list[str]) -> bool:
    """证据校验：AI 引用的每条事实必须能在发送给它的载荷中原样找到。

    这是"严格基于数据库判断"的落点——载荷字段全部来自扫描事实，
    编造的字段名或对不上的取值都会让整条判定降级为人工确认。
    """
    if not evidence:
        return False
    for item in evidence:
        key, separator, value = item.partition("=")
        if not separator:
            return False
        expected = payload.get(key.strip())
        if expected is None or str(expected) != value.strip():
            return False
    return True


def _batched(items: Sequence[Any], size: int) -> Iterable[Sequence[Any]]:
    iterator = iter(items)
    while batch := list(islice(iterator, size)):
        yield batch


def _text_part_to_str(text: Any) -> str | None:
    """归一化单个文本片段：纯字符串原样返回，SDK 风格 {"value": ...} 包装则取 value。"""
    if isinstance(text, str):
        return text
    if isinstance(text, dict) and isinstance(text.get("value"), str):
        return text["value"]
    return None


def _chat_content_to_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for part in content:
            if isinstance(part, str):
                parts.append(part)
            elif isinstance(part, dict):
                text = _text_part_to_str(part.get("text") or part.get("content"))
                if text is not None:
                    parts.append(text)
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
                text = _text_part_to_str(part.get("text"))
                if text is not None:
                    texts.append(text)
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


# ── 混合决策主体 ─────────────────────────────────────────────────────────
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
                results[index] = guard or safe_fallback_advice("未配置 AI，且本地规则无法安全确认用途。")
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
            else safe_fallback_advice("内部状态异常，已安全跳过。")
            for result in results
        ]

    def advise_units(self, units: Sequence[Unit]) -> list[Advice]:
        """判定单元管道：本地硬守卫就地裁决 → 其余全部交 AI（缓存 → 证据校验 → 决策表）。

        有意不设"明显垃圾零 AI 直判"的短路：AI 是主判断者，对典型垃圾做独立确认，
        对守卫放行的未知类型有完整判断权（含"建议删除"）。缓存保证同单元只问一次。
        返回顺序与输入一致；最终建议永远出自 decide_unit_advice，AI 无权越过守卫上限。
        """
        if not units:
            return []

        results: list[Advice | None] = [None] * len(units)
        ai_indices: list[int] = []
        guards: dict[int, Advice | None] = {}

        for index, unit in enumerate(units):
            representative = unit.members[0] if unit.members else None
            guard = local_safety_guard(representative) if representative is not None else None
            guards[index] = guard
            if guard is not None and guard.source == "local-guard":
                # 本地硬规则（受保护目录/可执行/用户内容类）的结论 AI 无权推翻，就地裁决。
                results[index] = guard
            elif not self.ai_available:
                results[index] = (
                    guard
                    if guard is not None
                    else safe_fallback_advice("未配置 AI，且本地规则无法安全确认用途。", source="unit-fallback")
                )
            else:
                ai_indices.append(index)

        if ai_indices:
            ai_units = [units[index] for index in ai_indices]
            try:
                raw_advices = self._get_unit_advices(ai_units)
            except (AdvisorError, urllib.error.URLError, TimeoutError, OSError, ValueError) as exc:
                self.stats.failures += len(ai_indices)
                for index in ai_indices:
                    guard = guards[index]
                    results[index] = self._fallback_advice(guard, exc)
            else:
                for index, raw_advice in zip(ai_indices, raw_advices, strict=True):
                    unit = units[index]
                    payload = unit_payload_for_ai(unit, self.settings.ai_privacy_mode)
                    evidence_ok = _validate_unit_evidence(payload, raw_advice.evidence)
                    results[index] = decide_unit_advice(
                        guards[index],
                        raw_advice,
                        evidence_ok=evidence_ok,
                    )

        return [
            result
            if result is not None
            else safe_fallback_advice("内部状态异常，已安全跳过。", source="unit-fallback")
            for result in results
        ]

    def advise_ai_only_many(self, metadata_items: Sequence[FileMetadata]) -> list[Advice]:
        """Evaluation-only AI output. It is never used by the production scan pipeline."""
        # 有意保留：仅 evaluation/run_benchmark.py 的纯 AI 对照方案使用，生产扫描路径不走这里。
        if not self.ai_available:
            return [
                safe_fallback_advice("未配置 AI，无法执行纯 AI 评测。", source="ai-unavailable")
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

    def _get_unit_advices(self, units: Sequence[Unit]) -> list[Advice]:
        """单元级 AI 调用：缓存键 = 供应商 + 隐私档 + 单元指纹 + 提示词/规则版本。

        指纹只含稳定模式（不含数量/体积），同类文件跨扫描直接命中缓存——
        "同样的盘状态必得同样的建议"由缓存数学保证，不依赖模型逐字稳定。
        """
        output: list[Advice | None] = [None] * len(units)
        pending: list[tuple[int, Unit, dict[str, Any], str]] = []

        provider_identity = (
            f"{self.settings.ai_base_url}|{self.settings.ai_api_style}|{self.settings.ai_model}"
        )
        for index, unit in enumerate(units):
            payload = unit_payload_for_ai(unit, self.settings.ai_privacy_mode)
            key_material = {
                "unit_fingerprint": unit.unit_id,
                "prompt_version": PROMPT_VERSION,
                "rules_version": RULES_VERSION,
            }
            cache_key = AdviceCache.make_key(provider_identity, self.settings.ai_privacy_mode, key_material)
            cached = self.cache.get(cache_key) if self.cache is not None else None
            if cached is not None:
                cached.source = "unit-cache"
                output[index] = cached
                self.stats.cache_hits += 1
            else:
                pending.append((index, unit, payload, cache_key))

        for batch in _batched(pending, self.settings.ai_batch_size):
            batch_payloads = [entry[2] for entry in batch]
            advices = self._request_ai_batch_resilient(
                batch_payloads, system_prompt=UNIT_SYSTEM_PROMPT, user_heading="判定单元"
            )
            for (index, _unit, payload, cache_key), advice in zip(batch, advices, strict=True):
                output[index] = advice
                if self.cache is not None:
                    self.cache.set(cache_key, payload, advice)

        return [
            advice
            if advice is not None
            else safe_fallback_advice("AI 未返回结果。", source="unit-fallback")
            for advice in output
        ]

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
        return safe_fallback_advice(f"AI 判断失败，已安全跳过：{detail}")

    @staticmethod
    def _apply_hybrid_guard(metadata: FileMetadata, guard: Advice | None, ai_advice: Advice) -> Advice:
        # AI 建议删除时若本地守卫持保留意见（人工确认/不建议），统一降级为"谨慎删除"。
        blocked_reason = ""
        if ai_advice.recommend_delete and guard is not None and guard.advice_level != "建议删除":
            blocked_reason = "AI 倾向清理，但本地规则要求人工确认。"
        if blocked_reason:
            return Advice(
                recommend_delete=False,
                purpose=ai_advice.purpose,
                advice_level="谨慎删除",
                reason=blocked_reason,
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
            else safe_fallback_advice("AI 未返回结果。")
            for advice in output
        ]

    def _request_ai_batch_resilient(
        self,
        payloads: Sequence[dict[str, Any]],
        *,
        system_prompt: str = SYSTEM_PROMPT,
        user_heading: str = "文件元数据",
    ) -> list[Advice]:
        """Split malformed/unsupported batches so one bad item does not discard all results."""
        try:
            return self._request_ai_batch(payloads, system_prompt=system_prompt, user_heading=user_heading)
        except BatchResponseError:
            if len(payloads) <= 1:
                raise
            midpoint = len(payloads) // 2
            left = self._request_ai_batch_resilient(
                payloads[:midpoint], system_prompt=system_prompt, user_heading=user_heading
            )
            right = self._request_ai_batch_resilient(
                payloads[midpoint:], system_prompt=system_prompt, user_heading=user_heading
            )
            return left + right

    def _build_request(
        self,
        payloads: Sequence[dict[str, Any]],
        *,
        system_prompt: str = SYSTEM_PROMPT,
        user_heading: str = "文件元数据",
    ) -> tuple[str, dict[str, Any]]:
        items = [{"id": index, **payload} for index, payload in enumerate(payloads)]
        user_text = f"请逐项判断以下{user_heading}：\n" + json.dumps(items, ensure_ascii=False)

        if self.settings.ai_api_style == "responses":
            return (
                f"{self.settings.ai_base_url}/responses",
                {
                    "model": self.settings.ai_model,
                    "instructions": system_prompt,
                    "input": user_text,
                    "store": False,
                },
            )

        return (
            f"{self.settings.ai_base_url}/chat/completions",
            {
                "model": self.settings.ai_model,
                "messages": [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_text},
                ],
            },
        )

    def _post_with_retries(self, endpoint: str, payload: dict[str, Any]) -> dict[str, Any]:
        """HTTP POST + 重试 + 调用统计的公共核心；chat 判定与区域选择共用。"""
        request_data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        last_error: Exception | None = None
        started = time.perf_counter()
        body: dict[str, Any] | None = None

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
        if not isinstance(body, dict):
            raise AdvisorError("AI 接口响应不是 JSON 对象")
        usage = body.get("usage", {})
        if isinstance(usage, dict):
            self.stats.prompt_tokens += int(
                usage.get("input_tokens", usage.get("prompt_tokens", 0)) or 0
            )
            self.stats.completion_tokens += int(
                usage.get("output_tokens", usage.get("completion_tokens", 0)) or 0
            )
        return body

    def suggest_areas(self, area_payloads: Sequence[dict[str, Any]]) -> list[tuple[int, str]] | None:
        """阶段一：让 AI 从目录聚合列表里挑出可疑区域，返回 (id, 理由) 列表。

        未配置 AI、请求失败或结果不可解析时返回 None，调用方回退体积启发式；
        区域选择是建议性输入，任何失败都不应中断分析流程。
        """
        if not self.ai_available or not area_payloads:
            return None
        items = [{"id": index, **payload} for index, payload in enumerate(area_payloads)]
        user_text = "请从以下目录聚合中挑出可疑区域：\n" + json.dumps(items, ensure_ascii=False)
        if self.settings.ai_api_style == "responses":
            endpoint = f"{self.settings.ai_base_url}/responses"
            request_payload: dict[str, Any] = {
                "model": self.settings.ai_model,
                "instructions": ANALYZE_SYSTEM_PROMPT,
                "input": user_text,
                "store": False,
            }
        else:
            endpoint = f"{self.settings.ai_base_url}/chat/completions"
            request_payload = {
                "model": self.settings.ai_model,
                "messages": [
                    {"role": "system", "content": ANALYZE_SYSTEM_PROMPT},
                    {"role": "user", "content": user_text},
                ],
            }
        try:
            body = self._post_with_retries(endpoint, request_payload)
            content = _response_text(body, self.settings.ai_api_style)
            data = _extract_json(content)
        except (AdvisorError, urllib.error.URLError, TimeoutError, OSError, ValueError):
            return None

        raw = data.get("areas")
        if not isinstance(raw, list):
            return None
        selections: list[tuple[int, str]] = []
        seen: set[int] = set()
        for entry in raw:
            if not isinstance(entry, dict):
                continue
            area_id = entry.get("id")
            reason = entry.get("reason")
            if not isinstance(area_id, int) or isinstance(area_id, bool):
                continue
            if not 0 <= area_id < len(area_payloads) or area_id in seen:
                continue
            if not isinstance(reason, str) or not reason.strip():
                continue
            seen.add(area_id)
            selections.append((area_id, reason.strip()[:_AREA_REASON_MAX_LENGTH]))
            if len(selections) >= 8:
                break
        return selections or None

    def review_duplicates(
        self, group_payloads: Sequence[dict[str, Any]]
    ) -> list[tuple[int, str, str]] | None:
        """重复组判读：返回 (id, verdict, reason) 列表；未配置 AI 或失败时返回 None。

        与 suggest_areas 同一失败哲学：这是提示性信号，任何失败都不应中断分析。
        """
        if not self.ai_available or not group_payloads:
            return None
        items = [{"id": index, **payload} for index, payload in enumerate(group_payloads)]
        user_text = "请判读以下同体积文件组：\n" + json.dumps(items, ensure_ascii=False)
        if self.settings.ai_api_style == "responses":
            endpoint = f"{self.settings.ai_base_url}/responses"
            request_payload: dict[str, Any] = {
                "model": self.settings.ai_model,
                "instructions": DUPLICATE_SYSTEM_PROMPT,
                "input": user_text,
                "store": False,
            }
        else:
            endpoint = f"{self.settings.ai_base_url}/chat/completions"
            request_payload = {
                "model": self.settings.ai_model,
                "messages": [
                    {"role": "system", "content": DUPLICATE_SYSTEM_PROMPT},
                    {"role": "user", "content": user_text},
                ],
            }
        try:
            body = self._post_with_retries(endpoint, request_payload)
            content = _response_text(body, self.settings.ai_api_style)
            data = _extract_json(content)
        except (AdvisorError, urllib.error.URLError, TimeoutError, OSError, ValueError):
            return None

        raw = data.get("reviews")
        if not isinstance(raw, list):
            return None
        reviews: list[tuple[int, str, str]] = []
        seen: set[int] = set()
        for entry in raw:
            if not isinstance(entry, dict):
                continue
            group_id = entry.get("id")
            verdict = entry.get("verdict")
            reason = entry.get("reason")
            if not isinstance(group_id, int) or isinstance(group_id, bool):
                continue
            if not 0 <= group_id < len(group_payloads) or group_id in seen:
                continue
            if verdict not in DUPLICATE_VERDICTS or not isinstance(reason, str) or not reason.strip():
                continue
            seen.add(group_id)
            reviews.append((group_id, verdict, reason.strip()[:40]))
        return reviews or None

    def _request_ai_batch(
        self,
        payloads: Sequence[dict[str, Any]],
        *,
        system_prompt: str = SYSTEM_PROMPT,
        user_heading: str = "文件元数据",
    ) -> list[Advice]:
        endpoint, payload = self._build_request(
            payloads, system_prompt=system_prompt, user_heading=user_heading
        )
        body = self._post_with_retries(endpoint, payload)
        self.stats.api_items += len(payloads)

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


def build_advisor(enable_ai: bool = True, privacy_mode: str | None = None) -> HybridAdvisor:
    """从 .env 构造顾问；CLI 与 GUI 共用这一个入口，保证配置读取行为一致。"""
    settings = Settings.from_env()
    if privacy_mode:
        settings = replace(settings, ai_privacy_mode=privacy_mode)
    return HybridAdvisor(settings, enable_ai=enable_ai)
