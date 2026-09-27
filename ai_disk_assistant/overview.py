"""深度分析事实层：把快照库聚合成一份"给 AI 看的整盘/整目录视图"。

与判定单元的定位差异（有意设计，不要合并）：
- 判定单元（units.py）是**逐条**视角：一个目录 + 一个后缀 + 一个大小档，输出结构化建议，
  走严格校验与决策表，能影响 recommend_delete；
- 本层是**全局**视角：把体积排名、后缀构成、最大文件、重复组、本次评审分布一次性交给 AI，
  输出一份自由 Markdown 综述（ai_advisor.summarize_overview），只用于展示，**永不参与删除判定**。

这正是"把磁盘占用截图发给 AI"对应的能力：模型先看到全局，才可能讲出跨区域的结论。
事实只来自 MFT 快照库，全程不访问文件系统、不读取任何文件内容。
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any

from .inventory import DirAgg, FileRow, Inventory
from .metadata import format_pct, format_size
from .mft_scanner import volume_capacity
from .privacy import anonymize_path, normalize_privacy_mode
from .safety import SAFE_CONTEXT_NAMES, normalized_parts

if TYPE_CHECKING:  # 只为类型标注：运行时不导入 analyzer，避免与 analyzer → overview 形成循环。
    from .analyzer import AnalyzeResult

# 载荷条数上限：事实清单太长会挤占输出预算，也会让模型抓不住重点。
DIR_LIMIT = 12
SUFFIX_LIMIT = 12
FILE_LIMIT = 12
GROUP_LIMIT = 8
REVIEW_CANDIDATE_LIMIT = 10
# 重复组最小体积：与 analyzer 的同体积组阈值一致（小于 1MB 的重复没有清理价值）。
MIN_DUPLICATE_SIZE = 1024 * 1024


def _shape_path(path: str, mode: str) -> str | None:
    """按隐私档给出可展示路径：strict 不发路径，balanced 匿名化，full 原样。"""
    if mode == "strict":
        return None
    if mode == "balanced":
        return anonymize_path(path)
    return path


def _context_tags(path: str) -> list[str]:
    """strict 档下的目录语义标签（缓存/日志/临时等），替代路径本身。"""
    return sorted(set(normalized_parts(path)) & SAFE_CONTEXT_NAMES)


def _dir_entry(row: DirAgg, mode: str, scope_size: int) -> dict[str, Any]:
    entry: dict[str, Any] = {
        "size_text": format_size(row.total_size),
        "file_count": row.file_count,
        "size_share_percent": format_pct(row.total_size, scope_size),
        "top_suffixes": row.top_suffixes,
    }
    shaped = _shape_path(row.path, mode)
    if shaped is None:
        entry["context_tags"] = _context_tags(row.path)
        entry["path_depth"] = len(normalized_parts(row.path))
    else:
        entry["path"] = shaped
        entry["name"] = row.path.replace("\\", "/").rstrip("/").rsplit("/", 1)[-1]
    return entry


def _file_entry(row: FileRow, mode: str) -> dict[str, Any]:
    entry: dict[str, Any] = {"name": row.name, "size_text": format_size(row.size_bytes)}
    shaped = _shape_path(row.path, mode)
    if shaped is not None:
        entry["path"] = shaped
    return entry


def _volume_entry(scope: str) -> dict[str, Any] | None:
    """卷容量（非 Windows 或取不到时为 None，GUI/CLI 都不因此报错）。"""
    total, free, used = volume_capacity(Path(scope).anchor)
    if not total:
        return None
    return {
        "total_text": format_size(total),
        "used_text": format_size(used),
        "free_text": format_size(free),
        "used_percent": format_pct(used, total),
    }


def _review_summary(review: "AnalyzeResult | None", mode: str) -> dict[str, Any] | None:
    """把本次评审的候选结果压成"已判读结论"事实块；没有候选则返回 None。"""
    if review is None or not review.candidates:
        return None
    candidates = review.candidates
    levels: dict[str, int] = {}
    purposes: dict[str, int] = {}
    for item in candidates:
        levels[item.advice.advice_level] = levels.get(item.advice.advice_level, 0) + 1
        purposes[item.advice.purpose] = purposes.get(item.advice.purpose, 0) + 1
    deletable = [item for item in candidates if item.advice.recommend_delete]
    summary: dict[str, Any] = {
        "candidate_count": len(candidates),
        "candidate_size_text": format_size(sum(item.metadata.size_bytes for item in candidates)),
        "recommended_count": len(deletable),
        "recommended_size_text": format_size(sum(item.metadata.size_bytes for item in deletable)),
        "level_distribution": levels,
        "purpose_distribution": dict(
            sorted(purposes.items(), key=lambda pair: pair[1], reverse=True)
        ),
        "largest_candidates": [
            {
                key: value
                for key, value in (
                    ("name", item.metadata.name),
                    ("size_text", item.metadata.size_text),
                    ("advice_level", item.advice.advice_level),
                    ("reason", item.advice.reason),
                    ("path", _shape_path(item.metadata.path, mode)),
                )
                if value is not None
            }
            for item in sorted(candidates, key=lambda entry: entry.metadata.size_bytes, reverse=True)[
                :REVIEW_CANDIDATE_LIMIT
            ]
        ],
    }
    if review.areas:
        summary["scanned_areas"] = [
            {
                "path": _shape_path(area.path, mode),
                "size_text": format_size(area.size_bytes),
                "file_count": area.file_count,
                "candidate_count": area.candidate_count,
                "candidate_size_text": format_size(area.candidate_size),
            }
            for area in review.areas[:DIR_LIMIT]
        ]
    return summary


def build_overview_payload(
    inventory: Inventory,
    snapshot_id: int,
    scope: str,
    *,
    privacy_mode: str = "balanced",
    review: "AnalyzeResult | None" = None,
    dir_limit: int = DIR_LIMIT,
    suffix_limit: int = SUFFIX_LIMIT,
    file_limit: int = FILE_LIMIT,
    group_limit: int = GROUP_LIMIT,
    min_duplicate_size: int = MIN_DUPLICATE_SIZE,
) -> dict[str, Any]:
    """构建深度分析载荷：范围总量 + 子目录排名 + 深层热点 + 后缀构成 + 最大文件 + 重复组。

    全部数字来自快照库；路径按隐私档裁剪。载荷内容即 AI 能引用的全部事实——
    模型看不到的东西，综述里也不允许出现。
    """
    mode = normalize_privacy_mode(privacy_mode)
    scope_size, scope_files = inventory.subtree_usage(snapshot_id, scope)

    scope_block: dict[str, Any] = {
        "size_text": format_size(scope_size),
        "file_count": scope_files,
    }
    shaped_scope = _shape_path(scope, mode)
    if shaped_scope is None:
        scope_block["context_tags"] = _context_tags(scope)
        scope_block["path_depth"] = len(normalized_parts(scope))
    else:
        scope_block["path"] = shaped_scope

    payload: dict[str, Any] = {
        "scope": scope_block,
        "data_source": "直读 NTFS $MFT 的文件元数据快照（未读取任何文件内容，未访问文件）",
        "largest_subdirectories": [
            _dir_entry(row, mode, scope_size)
            for row in inventory.child_dirs(snapshot_id, scope, limit=dir_limit)
        ],
        # 深层热点：范围之下所有层级的目录按体积排名——只按直接子目录会漏掉真正的大户。
        "hotspot_directories": [
            _dir_entry(row, mode, scope_size)
            for row in inventory.top_dirs(snapshot_id, dir_limit, under=scope)
        ],
        "largest_suffixes": [
            {
                "suffix": entry["suffix"],
                "size_text": format_size(int(entry["size_bytes"])),
                "file_count": entry["file_count"],
                "size_share_percent": format_pct(int(entry["size_bytes"]), scope_size),
            }
            for entry in inventory.extension_stats(snapshot_id, scope, suffix_limit)
        ],
        "largest_files": [
            _file_entry(row, mode)
            for row in inventory.largest_files(snapshot_id, file_limit, under=scope)
        ],
    }

    volume = _volume_entry(scope)
    if volume is not None:
        payload["volume"] = volume

    groups: list[dict[str, Any]] = []
    for group in inventory.duplicate_groups(snapshot_id, min_duplicate_size, group_limit):
        groups.append(
            {
                "size_text": format_size(int(group["size_bytes"])),
                "count": group["count"],
                "wasted_text": format_size(int(group["wasted_bytes"])),
                "samples": [
                    shaped
                    for shaped in (_shape_path(str(path), mode) for path in group["sample_paths"])
                    if shaped is not None
                ],
            }
        )
    if groups:
        payload["duplicate_groups"] = groups

    review_block = _review_summary(review, mode)
    if review_block is not None:
        payload["review"] = review_block

    return payload
