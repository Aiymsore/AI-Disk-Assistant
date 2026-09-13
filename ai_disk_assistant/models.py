"""数据模型层：全项目共享的数据结构与安全默认值（最底层，不依赖项目内其他模块）。

- PURPOSES / ADVICE_LEVELS：purpose 与 advice_level 的合法值域，AI 返回结果按此严格校验；
  SYSTEM_PROMPT 中的枚举清单也来自这里，改动时必须同步 ai_advisor.py 的提示词。
- Advice.__post_init__：所有 Advice 的统一兜底约束（非法值回落、超长截断、非"建议删除"一律不自动删）。
- safe_fallback_advice：拿不准时的唯一安全默认值工厂。
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any


PURPOSES = {
    "缓存文件",
    "临时文件",
    "日志文件",
    "安装包或下载残留",
    "程序配置文件",
    "系统文件",
    "用户文档",
    "媒体文件",
    "代码或项目文件",
    "存档或备份文件",
    "未知用途",
}

ADVICE_LEVELS = {"建议删除", "谨慎删除", "不建议删除", "人工确认"}

# 建议理由的统一长度上限。两处使用、两种策略（均为有意设计）：
# - ai_advisor._validate_advice：AI 返回超限直接报错（严格校验，防止提示词被无视）
# - Advice.__post_init__：本地构造超限静默截断（兜底容错，保证任何来源都能落地）
REASON_MAX_LENGTH = 120

# AI 判断必须附带"证据"：从输入中原样引用的事实（"字段名=值"），由证据校验层回数据库核对。
# 上限与 reason 同策略：AI 返回超限直接报错（严格校验），本地构造超限静默截断（兜底容错）。
EVIDENCE_MAX_ITEMS = 6
EVIDENCE_MAX_LENGTH = 80


@dataclass(slots=True)
class FileMetadata:
    path: str
    name: str
    suffix: str
    parent_folder: str
    size_bytes: int
    size_text: str
    modified_time: str
    accessed_time: str
    modified_time_ns: int = 0
    # 有意保留：accessed_time_ns 不参与任何判定逻辑（Windows 可能禁用 atime 更新，不可靠），
    # 仅随 CSV/JSON 报告导出供人工核对；快照校验只认 modified_time_ns。
    accessed_time_ns: int = 0
    device_id: int = 0
    file_id: int = 0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def snapshot(self) -> dict[str, int]:
        """Fields used to verify that a file did not change after scanning."""
        return {
            "size_bytes": self.size_bytes,
            "modified_time_ns": self.modified_time_ns,
            "device_id": self.device_id,
            "file_id": self.file_id,
        }


@dataclass(slots=True)
class Unit:
    """判定单元：同一目录下同后缀、大小同档的候选文件聚合。

    unit_id 是模式指纹（目录+后缀+大小档），不含数量/体积等易变聚合值，
    也不含修改时间（mtime 不可靠且会造成缓存无谓失效）——
    这样同一类文件跨扫描复用同一条 AI 判定，保证结果一致并压低调用量。
    members 仅驻内存，用于把单元判定展开回文件；不参与序列化与缓存键。
    """

    unit_id: str
    parent_folder: str
    suffix: str
    size_bucket: str
    file_count: int = 0
    total_size: int = 0
    best_score: float = 0.0
    members: list[FileMetadata] = field(default_factory=list)


@dataclass(slots=True)
class Advice:
    recommend_delete: bool
    purpose: str
    advice_level: str
    reason: str
    source: str = "local-rule"
    # 判断所依据的事实清单（"字段名=值"），本地规则可留空；AI 结果由证据校验层核对。
    evidence: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        if self.purpose not in PURPOSES:
            self.purpose = "未知用途"
        if self.advice_level not in ADVICE_LEVELS:
            self.advice_level = "人工确认"
        self.reason = self.reason.strip()[:REASON_MAX_LENGTH] or "缺少可靠判断依据，建议人工确认。"
        if self.advice_level != "建议删除":
            self.recommend_delete = False
        self.evidence = [
            text.strip()[:EVIDENCE_MAX_LENGTH] for text in (self.evidence or []) if str(text).strip()
        ][:EVIDENCE_MAX_ITEMS]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def safe_fallback_advice(reason: str, source: str = "local-fallback") -> Advice:
    """兜底建议工厂：任何拿不准的场景统一落到"未知用途 + 人工确认 + 不自动删除"。

    全项目的安全默认值只在这一处定义，避免各处手写 Advice 造成取值漂移。
    """
    return Advice(
        recommend_delete=False,
        purpose="未知用途",
        advice_level="人工确认",
        reason=reason,
        source=source,
    )


@dataclass(slots=True)
class Candidate:
    metadata: FileMetadata
    local_reason: str
    advice: Advice
    candidate_score: float = 0.0

    def to_row(self) -> dict[str, Any]:
        row = self.metadata.to_dict()
        row.update(
            {
                "local_reason": self.local_reason,
                "candidate_score": round(self.candidate_score, 3),
                "recommend_delete": self.advice.recommend_delete,
                "purpose": self.advice.purpose,
                "advice_level": self.advice.advice_level,
                "advice_reason": self.advice.reason,
                "advice_source": self.advice.source,
                "advice_evidence": "；".join(self.advice.evidence),
            }
        )
        return row

    @property
    def path(self) -> Path:
        return Path(self.metadata.path)


@dataclass(slots=True)
class ScanStats:
    root: str
    visited_files: int = 0
    matched_candidates: int = 0
    retained_candidates: int = 0
    skipped_errors: int = 0
    elapsed_seconds: float = 0.0
    # 判定单元统计：unit_count 为归并后的单元总数，units_judged 为其中进入 AI 判定的数量。
    unit_count: int = 0
    units_judged: int = 0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class ScanResult:
    candidates: list[Candidate]
    stats: ScanStats
