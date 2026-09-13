"""元数据采集层：文件大小格式化与 stat 快照采集（底层工具，被 scanner/report 使用）。"""

from __future__ import annotations

import time
from pathlib import Path

from .models import FileMetadata


def format_size(size: int) -> str:
    units = ("B", "KB", "MB", "GB", "TB")
    value = float(size)
    for unit in units:
        if value < 1024 or unit == units[-1]:
            return f"{value:.2f} {unit}" if unit != "B" else f"{int(value)} B"
        value /= 1024
    return f"{size} B"


def format_mtime(ns: int) -> str:
    """把纳秒时间戳格式化为可显示文本；快照里的垃圾时间（极端值）统一显示为 —。

    Windows 的 time.localtime 对负时间戳（1970 前）直接抛 OSError，必须防御，
    否则一条脏数据就能让整个目录列表渲染中断。
    """
    if not ns:
        return "—"
    try:
        return time.strftime("%Y-%m-%d %H:%M", time.localtime(ns / 1_000_000_000))
    except (OSError, OverflowError, ValueError):
        return "—"


def format_pct(size: int, total: int) -> str:
    """占父级（或全卷）的百分比，保留一位小数。

    整数地板除会把所有 <1% 的项都显示成 0%，小目录在巨型父目录里看起来像没有占比。
    """
    if total <= 0:
        return "0.0%"
    return f"{size * 100 / total:.1f}%"


def get_file_metadata(file_path: str | Path) -> FileMetadata:
    path = Path(file_path).expanduser()
    if not path.exists():
        raise FileNotFoundError(f"文件不存在：{path}")
    if not path.is_file():
        raise ValueError(f"目标不是普通文件：{path}")

    stat = path.stat()
    # st_mtime_ns / st_atime_ns 等纳秒字段在极老 Python/平台上可能缺失，getattr 回退保证可移植。
    return FileMetadata(
        path=str(path.resolve()),
        name=path.name,
        suffix=path.suffix.casefold(),
        parent_folder=str(path.parent.resolve()),
        size_bytes=stat.st_size,
        size_text=format_size(stat.st_size),
        modified_time=time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(stat.st_mtime)),
        accessed_time=time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(stat.st_atime)),
        modified_time_ns=getattr(stat, "st_mtime_ns", int(stat.st_mtime * 1_000_000_000)),
        accessed_time_ns=getattr(stat, "st_atime_ns", int(stat.st_atime * 1_000_000_000)),
        device_id=int(getattr(stat, "st_dev", 0)),
        file_id=int(getattr(stat, "st_ino", 0)),
    )
