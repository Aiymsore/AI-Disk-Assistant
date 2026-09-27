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
# 判据细则/分级标尺/语录风格是给模型的"裁判手册"；改动任何判定口径都要递增
# PROMPT_VERSION（进单元缓存键），否则同类文件的旧判定会被静默复用。
SYSTEM_PROMPT = """你是 Windows 磁盘清理工具中的文件安全建议模块。
输入是单个文件的元数据（路径/文件名/后缀/大小），你只能依据这些字段判断，
不能假设读取过文件内容，也不要以文件年龄作为依据——Windows 的修改时间
常被安装器回写，不可靠。路径可能已匿名化（%USERPROFILE%、<USER>），
请按目录结构与文件名模式判断，不要因路径被遮蔽而拒绝下结论。

安全原则：
1. 系统/程序目录中的文件、用户文档、媒体、代码与备份不得建议删除。
2. 明显的缓存、临时文件、日志、崩溃转储、下载残留与安装包，在证据支持时可直接建议删除。
3. 信号单一或互相矛盾时降级为"谨慎删除"或"人工确认"，不得冒险；
   仅凭体积大、数量多或文件名可疑，不足以建议删除。
4. .db/.sqlite/.dat 可能是浏览器历史、聊天记录等有个人价值的数据，倾向人工确认。

reason 写法（不超过 60 个中文字符）：一句话 = 定性 + 关键依据（引用字段值）+
需要人工核对点，不要套用空话；例如"Temp 目录的崩溃转储残留，可整体清理"。
evidence（可选）必须从输入字段中原样引用，格式为"字段名=值"，例如 "suffix=.tmp"。

5. 对每个输入 id 返回一条结果，不遗漏、不新增。
6. 只输出 JSON 对象，不输出 Markdown。

输出格式：
{"results":[{"id":0,"recommend_delete":false,"purpose":"未知用途","advice_level":"人工确认","reason":"依据不足","evidence":["suffix=.tmp"]}]}

purpose 只能是：缓存文件、临时文件、日志文件、安装包或下载残留、程序配置文件、系统文件、用户文档、媒体文件、代码或项目文件、存档或备份文件、未知用途。拿不准时用"未知用途"，不要硬套。
advice_level 只能是：建议删除、谨慎删除、不建议删除、人工确认。
"""

# ── 深度分析（自由综述）提示词：唯一一个不返回 JSON 的 AI 任务 ─────────────
# 定位：把"整盘/整目录的聚合事实"一次性交给模型，让它像人看过磁盘占用截图那样写分析。
# 与判定单元的根本差异：综述**不产生任何可执行结论**，只做展示，因此不需要严格枚举校验
# （枚举校验是为了约束 recommend_delete，而这里根本没有这个字段）。
DEEP_ANALYSIS_SYSTEM_PROMPT = """你是 Windows 磁盘空间分析顾问。用户提供一份由本地扫描器生成的聚合事实清单：
扫描器直读 NTFS 主文件表（$MFT）取得文件路径、大小、数量等元数据，不读取、不上传任何文件内容。

你的任务：写一份中文分析报告，效果等同于"有人把磁盘占用截图发给你"之后的专业解读——
先给总体判断，再解释空间到底去了哪，再逐个点评重点区域，指出风险与不确定项，最后给出处理顺序。

硬性要求：
1. 只能依据输入事实推理。不得声称读过文件内容，不得虚构输入里没有的路径、数字或程序名称。
2. 引用数字必须与输入一致，可以换算单位，不得放大或臆测可释放空间。
3. 不输出任何删除命令、脚本、批处理或自动化步骤；只给"建议做什么"与"需要人工核对什么"。
   可以建议用户在本工具内用手动删除功能处理（勾选后移入回收站、可还原），
   但语气必须是建议而非指令，并说明删除前应核对的内容。
4. 严格区分证据与推测：推测必须显式写成"推测"。证据不足时直接说证据不足。
5. 系统目录、程序安装目录、用户文档/代码/媒体等不得建议直接删除；拿不准就说明需要人工确认。
6. 只输出 Markdown 正文：不要 JSON、不要代码块、不要表格、不要输出文件内容片段。
7. 不要复述整份输入，只讲结论与支撑结论的关键数字。
8. 语气平实专业：不夸张、不渲染恐慌、不用营销腔；重复组只说"疑似重复，
   建议核对后处理"，不要断言内容相同。

建议结构（可按事实裁剪，但"总体判断"与"建议处理顺序"两节必须有）：
## 总体判断
## 空间去向
## 重点区域点评
## 风险与不确定项
## 建议处理顺序（每步写明预期释放的空间、动手前需要核对什么）

篇幅 400–1200 字；如果事实丰富，可以写到 2000 字左右，把关键区域的判断讲透。
"""

# 综述提示词版本：进入文本缓存键。任何提示词调整都必须递增，否则旧综述会被静默复用。
OVERVIEW_PROMPT_VERSION = "overview-prompt-2"

# 综述正文长度上限：模型偶发失控输出时兜底，防止把巨型文本写进报告与缓存。
OVERVIEW_MAX_LENGTH = 12000

# 区域选择的 reason 长度上限（suggest_areas 解析时截断）。
_AREA_REASON_MAX_LENGTH = 60

# 重复组判读的合法结论集合。
DUPLICATE_VERDICTS = {"likely", "possible", "unlikely"}

DUPLICATE_SYSTEM_PROMPT = """你是 Windows 磁盘清理工具中的重复文件判读模块。
输入是若干"同体积文件组"：体积完全相同的文件聚合（同体积可能巧合，未必同内容）。
你的任务：根据路径模式与文件名，判读每组更可能是哪种情况，仅作提示：
- likely：大概率是真重复——同名或带 (1)/(2)/副本/新文件夹 等副本后缀；
  版本演进残留（同名不同版本号散落多处）；同一安装包或压缩包在
  下载、临时、多个项目目录各有一份。
- possible：有重复迹象但证据不足——同名不同目录、同类型同大小但命名无关。
- unlikely：更可能是巧合同体积或各有所用——系统组件、程序资源文件、
  数据分片（part1/part2）、同一模板批量导出的产物。
不得输出删除建议；真正的去重必须由用户核对内容后自行决定。
规则：
1. 只输出 JSON 对象，不输出 Markdown。
2. 对每个输入 id 返回一条结果，不遗漏、不新增。
3. verdict 只能是 likely / possible / unlikely。
4. reason 不超过 40 个中文字符：点名依据（文件名或路径模式），不要只写"可能重复"。
输出格式：
{"reviews":[{"id":0,"verdict":"likely","reason":"同名安装包散落下载与临时目录"}]}
"""

ANALYZE_SYSTEM_PROMPT = """你是 Windows 磁盘清理工具中的目录区域判读模块。
输入是一次磁盘扫描中体积最大的目录聚合列表（路径模式、总大小、文件数、主要后缀）。
你的任务：挑出值得深入分析的可疑区域并说明理由。

优先挑选（按证据强度）：
- 临时/缓存/日志/崩溃转储集中目录：名称含 Temp/Cache/Logs/CrashDumps/WER/缓存/日志，
  或位于 AppData 下的缓存型路径；
- 下载残留：Downloads/下载 及其子目录，主要后缀是安装包或压缩包；
- 包管理与构建缓存：npm-cache/pip/cache/conda 等命名模式的大目录；
- 后缀结构高度单一的大目录（top_suffixes 里一两种后缀占绝对多数，且属于可清理类型）。
不要挑选：系统或程序目录（Windows/Program Files）、明显的用户文档/项目/媒体库、
整个盘根、单一文件，以及名称虽含 cache 但实为应用主要数据的大目录。
规则：
1. 只输出 JSON 对象，不输出 Markdown。
2. areas 数组最少 1 个、最多 8 个，按可疑程度降序。
3. reason 不超过 40 个中文字符：写"什么类型的残留 + 为什么可疑"，不要只写"体积大"。
4. id 必须原样引用输入中的 id，不得新增或改写。
输出格式：
{"areas":[{"id":0,"reason":"日志缓存密集，体积大"}]}
"""

# 判定单元提示词版本：进入单元判定缓存键。任何提示词变更都必须递增，旧缓存自动失效。
PROMPT_VERSION = "unit-prompt-4"

UNIT_SYSTEM_PROMPT = """你是 Windows 磁盘清理工具中的文件组安全建议模块。
输入是"判定单元"：同一目录下、同后缀、大小同档的一组文件的聚合统计。
你只能依据输入字段判断，不能假设读取过文件内容，也不要以文件年龄作为依据——
载荷刻意不含时间字段，Windows 的修改时间常被安装器回写，不可靠。
隐私说明：balanced 档下路径中的用户名已匿名化（%USERPROFILE%、<USER>），
这不是数据缺失，请按目录结构与文件名模式判断，不要因路径被遮蔽而拒绝下结论。

判据参考（目录上下文 × 后缀 × 数量/大小档互相印证）：
- 强清理信号：Temp/Cache/Logs/CrashDumps/WER 等目录中的 .tmp/.temp/.log/.dmp/.old；
  下载目录中的 .exe/.msi/.msix 安装包与 .zip/.rar 压缩包；
  路径含 npm-cache/pip/cache2/Code Cache/ShaderCache/DXCache 等缓存型命名。
- 构建产物：路径含 node_modules/dist/build/__pycache__ 等且非用户项目主目录，
  通常可再生，可积极处理；但注意其中可能混有 .env/密钥/配置等不应删除的文件。
- 保守信号：.doc/.pdf/.jpg/.mp4 等用户内容；.exe/.dll 位于程序目录；
  .db/.sqlite/.dat 可能是浏览器历史、聊天记录等有个人价值的数据。
- 数量信号：同目录同后缀文件多且大小同档，更像程序批量生成（日志/缓存/分卷）；
  只有 1-2 个则更可能是用户有意放置，倾向保守。

分级标尺：
- 建议删除：清理信号相互印证（目录上下文与后缀一致、无用户内容特征）才允许。
- 谨慎删除：只有单一信号，或对象可再生但证据不完全。
- 人工确认：信号矛盾、可能是用户数据或备份、或证据不足。
- 不建议删除：路径结构指向系统/程序目录，或明显是用户文档、媒体、代码、备份。
- 仅凭体积大或数量多不足以建议删除；有个人价值风险的永远不要建议删除。

reason 写法（不超过 60 个中文字符）：一句话 = 定性 + 关键依据（引用字段值）+
需要人工核对点；不同单元不要套用同一句套话。示例：
- "Temp 目录下 15 个 .dmp 崩溃转储，可整体清理"
- "下载目录的旧安装包，重装可能还需要，确认后可删"
- "同后缀同档大小，可能是日志轮转也可能是导出数据，建议抽查"
evidence 规则：必须从输入字段中原样引用至少 1 条，格式为"字段名=值"，
例如 "suffix=.tmp"、"file_count=15"、"path_pattern=C:\\Windows\\Temp"；
不得编造输入中没有的事实。
对每个输入 id 返回一条结果，不遗漏、不新增；只输出 JSON 对象，不输出 Markdown。

输出格式：
{"results":[{"id":0,"recommend_delete":true,"purpose":"临时文件","advice_level":"建议删除","reason":"Temp 目录成批 .tmp 残留，可整体清理","evidence":["suffix=.tmp","path_pattern=C:\\Windows\\Temp"]}]}

purpose 只能是：缓存文件、临时文件、日志文件、安装包或下载残留、程序配置文件、系统文件、用户文档、媒体文件、代码或项目文件、存档或备份文件、未知用途。拿不准时用"未知用途"，不要硬套。
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


def _clean_markdown(text: str) -> str:
    """清理自由文本响应：去掉模型习惯性包裹的 ``` 围栏与空行噪声。

    与 _extract_json 的区别：这里不需要结构，只需要"模型实际想说的正文"。
    围栏必须剥掉——否则综述在 Tkinter 文本框和 HTML 报告里都会多出一对 ```。
    """
    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```[a-zA-Z]*[ \t]*\r?\n?", "", cleaned)
        cleaned = re.sub(r"\r?\n?```$", "", cleaned)
    return cleaned.strip()


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
            # 提示词版本一并进键：提示词升级后旧缓存自动失效（与单元管线同一保证）。
            key_payload = {
                **payload,
                "_snapshot": metadata.snapshot(),
                "_prompt_version": PROMPT_VERSION,
            }
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

    def summarize_overview(self, facts: dict[str, Any]) -> str | None:
        """深度分析：把聚合事实交给 AI，返回一份自由 Markdown 综述；失败返回 None。

        与其余 AI 任务的三点差异（有意设计）：
        - 不返回结构化数据、不经过 _validate_advice，因为它**不产生任何删除判定**，
          只用于展示；安全性由"综述无法写回 recommend_delete"这一结构约束保证；
        - 独立缓存表（cache.get_text），键 = 供应商 + 隐私档 + 提示词版本 + 事实内容哈希，
          盘面没变就命中，重复分析零成本；
        - 显式声明输出预算（AI_OVERVIEW_MAX_TOKENS），否则服务端默认上限会把长文截断。
        """
        if not self.ai_available or not facts:
            return None

        provider_identity = (
            f"{self.settings.ai_base_url}|{self.settings.ai_api_style}|{self.settings.ai_model}"
        )
        key_material = {
            "kind": "deep-analysis",
            "prompt_version": OVERVIEW_PROMPT_VERSION,
            "facts": facts,
        }
        cache_key = AdviceCache.make_key(
            provider_identity, self.settings.ai_privacy_mode, key_material
        )
        if self.cache is not None:
            cached = self.cache.get_text(cache_key)
            if cached:
                self.stats.cache_hits += 1
                return cached

        user_text = "以下是本次磁盘扫描的聚合事实：\n" + json.dumps(facts, ensure_ascii=False)
        if self.settings.ai_api_style == "responses":
            endpoint = f"{self.settings.ai_base_url}/responses"
            request_payload: dict[str, Any] = {
                "model": self.settings.ai_model,
                "instructions": DEEP_ANALYSIS_SYSTEM_PROMPT,
                "input": user_text,
                "store": False,
                "max_output_tokens": self.settings.ai_overview_max_tokens,
            }
        else:
            endpoint = f"{self.settings.ai_base_url}/chat/completions"
            request_payload = {
                "model": self.settings.ai_model,
                "messages": [
                    {"role": "system", "content": DEEP_ANALYSIS_SYSTEM_PROMPT},
                    {"role": "user", "content": user_text},
                ],
                "max_tokens": self.settings.ai_overview_max_tokens,
            }

        try:
            body = self._post_with_retries(endpoint, request_payload)
            narrative = _clean_markdown(_response_text(body, self.settings.ai_api_style))
        except (AdvisorError, urllib.error.URLError, TimeoutError, OSError, ValueError):
            # 与 suggest_areas / review_duplicates 同一失败哲学：综述是附加价值，
            # 任何失败都不得中断分析，也不得影响已经产出的判定结果。
            self.stats.failures += 1
            return None

        if not narrative:
            return None
        if len(narrative) > OVERVIEW_MAX_LENGTH:
            narrative = narrative[:OVERVIEW_MAX_LENGTH] + "\n\n（正文过长，已截断）"
        if self.cache is not None:
            self.cache.set_text(cache_key, key_material, narrative)
        return narrative

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
