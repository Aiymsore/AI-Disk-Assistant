from __future__ import annotations

import heapq
import math
import os
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Callable, Iterable

from .ai_advisor import HybridAdvisor
from .metadata import get_file_metadata
from .models import Advice, Candidate, FileMetadata, ScanResult, ScanStats
from .safety import PROTECTED_DIR_NAMES, SAFE_CONTEXT_NAMES, is_protected_path


JUNK_SUFFIXES = {".tmp", ".temp", ".log", ".bak", ".old", ".dmp"}
INSTALLER_SUFFIXES = {".exe", ".msi", ".msix", ".apk"}
ARCHIVE_SUFFIXES = {".zip", ".rar", ".7z", ".tar", ".gz", ".iso"}


@dataclass(frozen=True, slots=True)
class ScanPolicy:
    old_days: int = 180
    small_file_size: int = 10 * 1024
    big_file_size: int = 100 * 1024 * 1024
    ai_limit: int = 80
    max_candidates: int = 5000
    max_scan_files: int = 0  # 0 means full traversal.


def _iter_files(root: Path) -> Iterable[Path]:
    for current, dirnames, filenames in os.walk(root, topdown=True, followlinks=False):
        dirnames[:] = [
            name
            for name in dirnames
            if name.casefold() not in PROTECTED_DIR_NAMES
            and not (Path(current) / name).is_symlink()
        ]
        for filename in filenames:
            path = Path(current) / filename
            if not path.is_symlink():
                yield path


def _candidate_signals(path: Path, root: Path, policy: ScanPolicy) -> tuple[str, float, int] | None:
    try:
        stat = path.stat()
    except (PermissionError, OSError):
        return None

    # Modification time is the main signal. Access time is intentionally not treated
    # as authoritative because Windows may disable or delay atime updates.
    modified = datetime.fromtimestamp(stat.st_mtime)
    is_old = modified < datetime.now() - timedelta(days=policy.old_days)
    age_days = max((datetime.now() - modified).days, 0)
    is_small = stat.st_size <= policy.small_file_size
    is_big = stat.st_size >= policy.big_file_size
    suffix = path.suffix.casefold()
    try:
        relative_parts = path.relative_to(root).parts
    except ValueError:
        relative_parts = path.parts
    path_parts = {part.casefold() for part in relative_parts}
    in_safe_context = bool(path_parts & SAFE_CONTEXT_NAMES)

    reasons: list[str] = []
    score = 0.0
    if suffix in JUNK_SUFFIXES:
        reasons.append("临时/日志类后缀")
        score += 35
    if in_safe_context:
        reasons.append("位于缓存、日志或临时目录")
        score += 30
    if is_old:
        reasons.append(f"超过 {policy.old_days} 天未修改（访问时间仅供参考）")
        score += min(age_days / max(policy.old_days, 1), 3.0) * 8
    if is_small and (suffix in JUNK_SUFFIXES or in_safe_context):
        reasons.append("小型缓存类文件")
        score += 3
    if is_big and is_old:
        reasons.append("长期未修改的大文件")
        score += 20
    if suffix in INSTALLER_SUFFIXES and is_old:
        reasons.append("长期未修改的安装包")
        score += 12
    if suffix in ARCHIVE_SUFFIXES and is_old:
        reasons.append("长期未修改的压缩包或镜像")
        score += 10

    has_context_signal = (
        in_safe_context
        or suffix in JUNK_SUFFIXES | INSTALLER_SUFFIXES | ARCHIVE_SUFFIXES
        or is_big
    )
    if not reasons or not has_context_signal:
        return None

    # Size only breaks ties moderately; it cannot make a random user file safe.
    score += min(math.log2(max(stat.st_size, 1) + 1), 32) * 0.25
    return "；".join(dict.fromkeys(reasons)), score, stat.st_size


def _retain_top_candidate(
    heap: list[tuple[float, int, int, str, str]],
    path: Path,
    reason: str,
    score: float,
    size: int,
    counter: int,
    limit: int,
) -> None:
    item = (score, size, counter, str(path), reason)
    if len(heap) < limit:
        heapq.heappush(heap, item)
    elif item[:2] > heap[0][:2]:
        heapq.heapreplace(heap, item)


def scan_with_stats(
    root: str | Path,
    advisor: HybridAdvisor,
    policy: ScanPolicy | None = None,
    progress: Callable[[int], None] | None = None,
) -> ScanResult:
    scan_policy = policy or ScanPolicy()
    root_path = Path(root).expanduser().resolve()
    if not root_path.exists() or not root_path.is_dir():
        raise ValueError(f"扫描目录不存在或不是文件夹：{root_path}")
    if is_protected_path(root_path):
        raise ValueError(f"拒绝直接扫描受保护的系统或程序目录：{root_path}")
    if scan_policy.max_candidates < 1:
        raise ValueError("max_candidates 必须大于 0")

    started = time.perf_counter()
    stats = ScanStats(root=str(root_path))
    retained: list[tuple[float, int, int, str, str]] = []
    counter = 0

    for path in _iter_files(root_path):
        if scan_policy.max_scan_files and stats.visited_files >= scan_policy.max_scan_files:
            break
        stats.visited_files += 1
        if progress and stats.visited_files % 500 == 0:
            progress(stats.visited_files)

        signal = _candidate_signals(path, root_path, scan_policy)
        if signal is None:
            continue
        reason, score, size = signal
        stats.matched_candidates += 1
        _retain_top_candidate(
            retained,
            path,
            reason,
            score,
            size,
            counter,
            scan_policy.max_candidates,
        )
        counter += 1

    # Highest-priority candidates first after a full directory traversal.
    preliminary = sorted(retained, key=lambda item: (item[0], item[1]), reverse=True)
    metadata_records: list[tuple[FileMetadata, str, float]] = []
    for score, _size, _counter, path_text, reason in preliminary:
        try:
            metadata = get_file_metadata(path_text)
        except (FileNotFoundError, PermissionError, OSError, ValueError):
            stats.skipped_errors += 1
            continue
        metadata_records.append((metadata, reason, score))

    ai_count = min(max(scan_policy.ai_limit, 0), len(metadata_records))
    ai_metadata = [record[0] for record in metadata_records[:ai_count]]
    advices = advisor.advise_many(ai_metadata) if ai_metadata else []

    results: list[Candidate] = []
    for index, (metadata, reason, score) in enumerate(metadata_records):
        if index < ai_count:
            advice = advices[index]
        else:
            advice = Advice(
                recommend_delete=False,
                purpose="未知用途",
                advice_level="人工确认",
                reason=f"超过本次 AI 判断上限 {scan_policy.ai_limit}，需人工确认。",
                source="not-evaluated",
            )
        results.append(
            Candidate(
                metadata=metadata,
                local_reason=reason,
                advice=advice,
                candidate_score=score,
            )
        )

    stats.retained_candidates = len(results)
    stats.elapsed_seconds = round(time.perf_counter() - started, 4)
    return ScanResult(candidates=results, stats=stats)


def scan_candidates(
    root: str | Path,
    advisor: HybridAdvisor,
    policy: ScanPolicy | None = None,
    progress: Callable[[int], None] | None = None,
) -> list[Candidate]:
    """Backward-compatible wrapper returning only candidates."""
    return scan_with_stats(root, advisor, policy, progress).candidates
