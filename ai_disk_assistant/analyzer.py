"""全盘区域分析（MFT 唯一事实源）：阶段一圈区域 → 递归下钻 → 叶子才评分；同体积组判读。

token 花在刀刃上：被圈区域若子目录够多（≥ _DRILL_MIN_CHILDREN），先对子目录再做
一轮"摘要 → 圈子区域"，逐层深入，直到叶子目录才对其子树内文件整体评分；每层的散文件
（不属于任何子目录的）单独评审，保证不漏。全程不访问文件系统——所有事实来自 MFT
快照库，评分、归并、AI 判读都是纯计算。无 AI 时每层回退体积启发式。
同体积组（重复文件线索）按快照聚合查出，AI 仅作提示性判读，不给删除建议。
被 cli.py 与 gui.py 的 analyze 入口调用。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from .ai_advisor import HybridAdvisor
from .config import DEFAULT_INVENTORY_PATH
from .inventory import DirAgg, FileRow, Inventory
from .mft_scanner import snapshot_volume
from .metadata import format_mtime, format_size
from .models import Candidate, FileMetadata, ScanStats
from .privacy import anonymize_path
from .scanner import ScanPolicy, _signals_from_facts, judge_metadata_records
from .safety import is_protected_path

# 阶段一送给 AI 的目录数量上限：一份摘要几十行，控制在一次调用的预算内。
_STAGE1_DIR_LIMIT = 60
# 下钻策略：子目录少于该数直接对子树整体评分（拆了反而费 token）；
# drill_depth 是阶段一之后还允许"摘要 → 圈子区域"的层数。
_DRILL_MIN_CHILDREN = 6
_DEFAULT_DRILL_DEPTH = 2
# 同体积组（疑似重复）：阈值与组数上限。
_DUPLICATE_MIN_SIZE = 1024 * 1024
_DUPLICATE_MAX_GROUPS = 20


@dataclass(slots=True)
class Area:
    path: str
    reason: str
    size_bytes: int
    file_count: int
    candidate_count: int = 0
    candidate_size: int = 0

    def to_dict(self) -> dict[str, object]:
        return {
            "path": self.path,
            "reason": self.reason,
            "size_bytes": self.size_bytes,
            "size_text": format_size(self.size_bytes),
            "file_count": self.file_count,
            "candidate_count": self.candidate_count,
            "candidate_size_bytes": self.candidate_size,
            "candidate_size_text": format_size(self.candidate_size),
        }


@dataclass(slots=True)
class DuplicateGroup:
    """同体积文件组：同体积可能巧合，组内判读仅作提示，去重由用户核对内容后决定。"""

    size_bytes: int
    count: int
    sample_paths: list[str]
    wasted_bytes: int
    verdict: str | None = None
    comment: str | None = None

    def to_dict(self) -> dict[str, object]:
        return {
            "size_bytes": self.size_bytes,
            "size_text": format_size(self.size_bytes),
            "count": self.count,
            "wasted_bytes": self.wasted_bytes,
            "wasted_text": format_size(self.wasted_bytes),
            "sample_paths": self.sample_paths,
            "verdict": self.verdict,
            "comment": self.comment,
        }


@dataclass(slots=True)
class AnalyzeResult:
    areas: list[Area] = field(default_factory=list)
    candidates: list[Candidate] = field(default_factory=list)
    stats: ScanStats | None = None
    snapshot_file_count: int = 0
    duplicates: list[DuplicateGroup] = field(default_factory=list)


def _area_payloads(dir_rows: list[DirAgg]) -> list[dict[str, object]]:
    """目录摘要载荷：路径按 balanced 语义脱敏（区域级分析不细分 strict/full）。"""
    payloads: list[dict[str, object]] = []
    for row in dir_rows:
        payloads.append(
            {
                "path": anonymize_path(row.path),
                "total_size_text": format_size(row.total_size),
                "file_count": row.file_count,
                "top_suffixes": row.top_suffixes,
            }
        )
    return payloads


def _metadata_from_row(row: FileRow) -> FileMetadata:
    modified = format_mtime(row.mtime_ns)
    return FileMetadata(
        path=row.path,
        name=row.name,
        suffix=row.suffix,
        parent_folder=str(Path(row.path).parent),
        size_bytes=row.size_bytes,
        size_text=format_size(row.size_bytes),
        modified_time=modified,
        accessed_time=modified,
        modified_time_ns=row.mtime_ns,
        accessed_time_ns=row.mtime_ns,
    )


def _score_rows(
    rows: list[FileRow],
    area_path: Path,
    advisor: HybridAdvisor,
    policy: ScanPolicy,
) -> tuple[list[Candidate], int, int]:
    """对快照事实行做规则评分，命中的进单元管道评审。返回 (候选, 评分数, 单元数)。"""
    records: list[tuple[FileMetadata, str, float]] = []
    for row in rows:
        signal = _signals_from_facts(row.path, row.suffix, row.size_bytes, area_path, policy)
        if signal is None:
            continue
        reason, score, _size = signal
        records.append((_metadata_from_row(row), reason, score))
    if not records:
        return [], 0, 0
    candidates, unit_count = judge_metadata_records(records, advisor, policy)
    return candidates, len(records), unit_count


def _dedupe_nested(
    dir_rows: list[DirAgg], selections: list[tuple[int, str]], area_limit: int
) -> list[tuple[int, str]]:
    """圈选去重：若某选中目录已被更大的选中区域覆盖，跳过它，避免重复评分。"""
    chosen_prefixes: list[str] = []
    filtered: list[tuple[int, str]] = []
    for index, reason in selections:
        casefolded = dir_rows[index].path.casefold()
        if any(casefolded.startswith(prefix) for prefix in chosen_prefixes):
            continue
        filtered.append((index, reason))
        chosen_prefixes.append(casefolded + "\\")
        if len(filtered) >= area_limit:
            break
    return filtered


def _pick_sub_areas(
    children: list[DirAgg],
    advisor: HybridAdvisor,
    area_limit: int,
) -> list[tuple[int, str]]:
    """在一层子目录里圈选：AI 优先（仅过滤嵌套），无 AI 时按体积贪心选不嵌套的前 N 个。"""
    payloads = _area_payloads(children)
    selections = advisor.suggest_areas(payloads) if advisor.ai_available else None
    if selections:
        return _dedupe_nested(children, selections, area_limit)
    return _dedupe_nested(
        children,
        [
            (index, "体积启发式：未使用 AI，仅按目录体积选取")
            for index in range(len(children))
        ],
        area_limit,
    )


def _drill_area(
    area_path: Path,
    reason: str,
    size_bytes: int,
    file_count: int,
    inventory: Inventory,
    snapshot_id: int,
    advisor: HybridAdvisor,
    policy: ScanPolicy,
    depth: int,
    area_limit: int,
    log: Callable[[str], None],
) -> tuple[list[Area], list[Candidate], int, int, int]:
    """递归下钻：大区域先圈子区域，小区域（或已到深度下限）才对子树整体评分。

    返回 (区域列表, 候选列表, 评分文件数, 保留候选数, 单元总数)。
    区域列表只含"真正评分过的叶子"，reason 保留圈选链路（阶段一理由 > 子区域理由）。
    """
    children = inventory.child_dirs(snapshot_id, str(area_path))
    if depth > 0 and len(children) >= _DRILL_MIN_CHILDREN:
        selections = _pick_sub_areas(children, advisor, area_limit)
        areas: list[Area] = []
        candidates: list[Candidate] = []
        scored = kept = units = 0
        for index, sub_reason in selections:
            child = children[index]
            if is_protected_path(Path(child.path)):
                continue
            log(f"下钻子区域：{child.path}")
            sub_areas, sub_candidates, sub_scored, sub_kept, sub_units = _drill_area(
                Path(child.path),
                f"{reason} > {sub_reason}",
                child.total_size,
                child.file_count,
                inventory,
                snapshot_id,
                advisor,
                policy,
                depth - 1,
                area_limit,
                log,
            )
            areas += sub_areas
            candidates += sub_candidates
            scored += sub_scored
            kept += sub_kept
            units += sub_units
        # 本层散文件不因下钻而漏扫。
        loose_candidates, _loose_scored, loose_units = _score_rows(
            inventory.direct_files(snapshot_id, str(area_path)), area_path, advisor, policy
        )
        candidates += loose_candidates
        return areas, candidates, scored, kept, units + loose_units

    # 叶子：子树可控（或已到深度下限），整个子树的文件一起评分。
    rows = inventory.files_under(snapshot_id, str(area_path))
    candidates, _scored, units = _score_rows(rows, area_path, advisor, policy)
    candidate_size = sum(item.metadata.size_bytes for item in candidates)
    area = Area(
        path=str(area_path),
        reason=reason,
        size_bytes=size_bytes,
        file_count=file_count,
        candidate_count=len(candidates),
        candidate_size=candidate_size,
    )
    return [area], candidates, len(rows), len(candidates), units


def analyze_root(
    root: str | Path,
    advisor: HybridAdvisor,
    *,
    policy: ScanPolicy | None = None,
    area_limit: int = 6,
    inventory_path: str | Path = DEFAULT_INVENTORY_PATH,
    progress: Callable[[str], None] | None = None,
    drill_depth: int = _DEFAULT_DRILL_DEPTH,
) -> AnalyzeResult:
    """整卷 MFT 快照 → 圈区域 → 递归下钻评分 → 同体积组提示。"""
    root_path = Path(root).expanduser().resolve()
    if not root_path.exists() or not root_path.is_dir():
        raise ValueError(f"分析目录不存在或不是文件夹：{root_path}")
    if is_protected_path(root_path):
        raise ValueError(f"拒绝直接分析受保护的系统或程序目录：{root_path}")
    scan_policy = policy or ScanPolicy()
    log = progress or (lambda _message: None)

    inventory = Inventory(inventory_path)
    log(f"正在直读 MFT 建立快照：{Path(inventory_path).resolve()}")
    drive = root_path.anchor
    _snapshot_id, file_count = snapshot_volume(drive, inventory, progress=log)
    log(f"快照完成：{file_count} 个文件，开始圈定区域……")
    snapshot_id = inventory.latest_snapshot() or 0

    dir_rows = inventory.top_dirs(snapshot_id, _STAGE1_DIR_LIMIT * 4, exclude=str(root_path))
    # MFT 快照覆盖整卷，只保留分析根之下的目录。
    root_prefix = str(root_path).casefold()
    dir_rows = [row for row in dir_rows if row.path.casefold().startswith(root_prefix)][
        :_STAGE1_DIR_LIMIT
    ]

    selections = (
        advisor.suggest_areas(_area_payloads(dir_rows)) if advisor.ai_available and dir_rows else None
    )
    if selections:
        selections = _dedupe_nested(dir_rows, selections, area_limit)
    else:
        selections = _dedupe_nested(
            dir_rows,
            [
                (index, "体积启发式：未使用 AI，仅按目录体积选取")
                for index in range(len(dir_rows))
            ],
            area_limit,
        )

    areas: list[Area] = []
    all_candidates: list[Candidate] = []
    scored = kept = unit_count = 0

    for index, reason in selections:
        dir_row = dir_rows[index]
        area_path = Path(dir_row.path)
        if is_protected_path(area_path):
            continue
        log(f"深入分析区域：{area_path}")
        try:
            sub_areas, sub_candidates, sub_scored, sub_kept, sub_units = _drill_area(
                area_path,
                reason,
                dir_row.total_size,
                dir_row.file_count,
                inventory,
                snapshot_id,
                advisor,
                scan_policy,
                depth=max(drill_depth - 1, 0),
                area_limit=area_limit,
                log=log,
            )
        except (ValueError, OSError):
            continue
        areas += sub_areas
        all_candidates += sub_candidates
        scored += sub_scored
        kept += sub_kept
        unit_count += sub_units

    # 同体积组（重复文件线索）：零成本的规则信号，AI 仅作提示性判读，不给删除建议。
    duplicates: list[DuplicateGroup] = []
    raw_groups = inventory.duplicate_groups(snapshot_id, _DUPLICATE_MIN_SIZE, _DUPLICATE_MAX_GROUPS)
    kept_groups: list[dict[str, object]] = []
    review_payloads: list[dict[str, object]] = []
    for group in raw_groups:
        samples = [str(path) for path in group["sample_paths"]]
        if any(is_protected_path(path) for path in samples):
            continue  # 受保护目录里的同体积组件不是清理对象
        kept_groups.append(group)
        review_payloads.append(
            {
                "size_text": format_size(int(group["size_bytes"])),
                "file_count": group["count"],
                "samples": [anonymize_path(path) for path in samples],
            }
        )
    reviews = (
        advisor.review_duplicates(review_payloads) if advisor.ai_available and kept_groups else None
    )
    for position, group in enumerate(kept_groups):
        verdict = comment = None
        if reviews:
            for group_id, review_verdict, review_reason in reviews:
                if group_id == position:
                    verdict, comment = review_verdict, review_reason
                    break
        duplicates.append(
            DuplicateGroup(
                size_bytes=int(group["size_bytes"]),
                count=int(group["count"]),
                sample_paths=[str(path) for path in group["sample_paths"]],
                wasted_bytes=int(group["size_bytes"]) * (int(group["count"]) - 1),
                verdict=verdict,
                comment=comment,
            )
        )

    stats = ScanStats(root=str(root_path))
    stats.visited_files = file_count
    stats.matched_candidates = scored
    stats.retained_candidates = kept
    stats.unit_count = unit_count
    stats.units_judged = unit_count  # 无数量上限：全部单元实判。
    return AnalyzeResult(
        areas=areas,
        candidates=all_candidates,
        stats=stats,
        snapshot_file_count=file_count,
        duplicates=duplicates,
    )
