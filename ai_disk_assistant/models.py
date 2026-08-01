from __future__ import annotations

from dataclasses import asdict, dataclass
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
class Advice:
    recommend_delete: bool
    purpose: str
    advice_level: str
    reason: str
    source: str = "local-rule"

    def __post_init__(self) -> None:
        if self.purpose not in PURPOSES:
            self.purpose = "未知用途"
        if self.advice_level not in ADVICE_LEVELS:
            self.advice_level = "人工确认"
        self.reason = self.reason.strip()[:120] or "缺少可靠判断依据，建议人工确认。"
        if self.advice_level != "建议删除":
            self.recommend_delete = False

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


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

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class ScanResult:
    candidates: list[Candidate]
    stats: ScanStats
