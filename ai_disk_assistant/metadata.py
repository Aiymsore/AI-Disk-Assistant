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


def get_file_metadata(file_path: str | Path) -> FileMetadata:
    path = Path(file_path).expanduser()
    if not path.exists():
        raise FileNotFoundError(f"文件不存在：{path}")
    if not path.is_file():
        raise ValueError(f"目标不是普通文件：{path}")

    stat = path.stat()
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
