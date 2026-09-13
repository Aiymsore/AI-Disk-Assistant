"""判定单元层：把候选文件按"目录 + 后缀 + 大小档"归并为 AI 判定单元。

一致性与成本的核心机制：同模式文件共用一条 AI 判定与同一个缓存指纹
（指纹只含稳定模式，不含数量/体积/时间等易变值，跨扫描保持命中）。
有意不纳入年龄：Windows 下 mtime 不可靠，且文件跨年龄档会造成缓存无谓失效。
被 scanner.py（归并候选）与 ai_advisor.py（指纹缓存键）使用。
"""

from __future__ import annotations

import hashlib
from bisect import bisect_right
from typing import Sequence

from .models import FileMetadata, Unit

# 桶边界是缓存稳定性的组成部分：一经发布不得改动取值，只能追加新桶（并升级 RULES_VERSION）。
SIZE_BUCKET_EDGES = [
    10 * 1024,
    1024 * 1024,
    10 * 1024 * 1024,
    100 * 1024 * 1024,
    1024 * 1024 * 1024,
    10 * 1024 * 1024 * 1024,
]
SIZE_BUCKET_LABELS = ["<10KB", "10KB-1MB", "1-10MB", "10-100MB", "100MB-1GB", "1-10GB", ">10GB"]


def size_bucket(size_bytes: int) -> str:
    index = bisect_right(SIZE_BUCKET_EDGES, size_bytes)
    return SIZE_BUCKET_LABELS[index]


def unit_fingerprint(parent_folder: str, suffix: str, size_label: str) -> str:
    """模式指纹：只绑定稳定的模式事实，跨扫描（乃至跨机器同模式）保持一致。

    目录名统一小写并规范分隔符——Windows 路径大小写不敏感，避免因大小写差异漏缓存。
    """
    normalized_folder = str(parent_folder).replace("/", "\\").casefold()
    material = "\x1f".join((normalized_folder, suffix.casefold(), size_label))
    return hashlib.sha256(material.encode("utf-8")).hexdigest()[:16]


def build_units(
    records: Sequence[tuple[FileMetadata, str, float]],
) -> tuple[list[Unit], dict[str, str]]:
    """把 (元数据, 候选理由, 打分) 记录归并为判定单元。

    返回 (按最高候选分降序的单元列表, 成员路径 -> unit_id 映射)。
    """
    grouped: dict[str, Unit] = {}

    for metadata, _reason, score in records:
        size_label = size_bucket(metadata.size_bytes)
        unit_id = unit_fingerprint(metadata.parent_folder, metadata.suffix, size_label)

        unit = grouped.get(unit_id)
        if unit is None:
            unit = Unit(
                unit_id=unit_id,
                parent_folder=metadata.parent_folder,
                suffix=metadata.suffix,
                size_bucket=size_label,
                best_score=score,
            )
            grouped[unit_id] = unit

        unit.file_count += 1
        unit.total_size += metadata.size_bytes
        unit.best_score = max(unit.best_score, score)
        unit.members.append(metadata)

    units = sorted(grouped.values(), key=lambda unit: (unit.best_score, unit.total_size), reverse=True)
    path_to_unit = {
        member.path: unit.unit_id for unit in units for member in unit.members
    }
    return units, path_to_unit
