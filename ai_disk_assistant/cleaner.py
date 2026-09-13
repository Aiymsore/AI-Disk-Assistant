from __future__ import annotations

from pathlib import Path
from typing import Iterable

from .models import Candidate, FileMetadata
from .safety import can_move_to_trash


class CleanerUnavailable(RuntimeError):
    pass


def select_auto_candidates(candidates: Iterable[Candidate]) -> list[Candidate]:
    return [
        item
        for item in candidates
        if item.advice.recommend_delete and item.advice.advice_level == "建议删除"
    ]


def verify_file_unchanged(metadata: FileMetadata) -> tuple[bool, str]:
    """Prevent TOCTOU deletion by comparing the current file with the scan snapshot."""
    path = Path(metadata.path)
    try:
        stat = path.stat()
    except FileNotFoundError:
        return False, "文件在扫描后已不存在"
    except OSError as exc:
        return False, f"无法重新读取文件状态：{exc}"

    current_mtime_ns = getattr(stat, "st_mtime_ns", int(stat.st_mtime * 1_000_000_000))
    if stat.st_size != metadata.size_bytes:
        return False, "文件大小在扫描后发生变化"
    if metadata.modified_time_ns and current_mtime_ns != metadata.modified_time_ns:
        return False, "文件修改时间在扫描后发生变化"

    current_device = int(getattr(stat, "st_dev", 0))
    current_file_id = int(getattr(stat, "st_ino", 0))
    if metadata.device_id and current_device and current_device != metadata.device_id:
        return False, "文件所在设备在扫描后发生变化"
    if metadata.file_id and current_file_id and current_file_id != metadata.file_id:
        return False, "路径当前指向的文件与扫描时不同"
    return True, ""


def move_to_trash(candidates: Iterable[Candidate]) -> tuple[list[Path], list[tuple[Path, str]]]:
    try:
        from send2trash import send2trash
    except ImportError as exc:
        raise CleanerUnavailable("缺少 Send2Trash，请先运行 pip install -r requirements.txt") from exc

    moved: list[Path] = []
    failed: list[tuple[Path, str]] = []
    for candidate in candidates:
        path = candidate.path
        allowed, reason = can_move_to_trash(path)
        if not allowed:
            failed.append((path, reason))
            continue
        unchanged, reason = verify_file_unchanged(candidate.metadata)
        if not unchanged:
            failed.append((path, reason))
            continue
        try:
            send2trash(str(path))
            moved.append(path)
        except Exception as exc:  # platform-specific trash implementations
            failed.append((path, str(exc)))
    return moved, failed
