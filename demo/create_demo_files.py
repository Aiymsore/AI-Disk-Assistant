from __future__ import annotations

import os
import shutil
import time
from pathlib import Path


def write_sparse(path: Path, size: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("wb") as file:
        file.write(b"demo")
        file.truncate(size)


def main() -> None:
    root = Path(__file__).resolve().parent / "sample_disk"
    if root.exists():
        shutil.rmtree(root)

    files: dict[Path, bytes] = {
        root / "ExampleApp" / "cache" / "session.tmp": b"temporary data",
        root / "ExampleApp" / "cache" / "data_001": b"rebuildable cache blob",
        root / "ExampleApp" / "logs" / "app.log": b"old application log",
        root / "ExampleApp" / "logs" / "latest": b"extensionless application log",
        root / "ExampleApp" / "CrashDumps" / "editor.dmp": b"crash dump placeholder",
        root / "Temp" / "preview.cache": b"preview cache placeholder",
    }
    for path, content in files.items():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)

    write_sparse(root / "old_installer.exe", 105 * 1024 * 1024)
    write_sparse(root / "Documents" / "coursework.docx", 110 * 1024 * 1024)
    write_sparse(root / "Backups" / "project_backup.7z", 120 * 1024 * 1024)

    old_time = time.time() - 365 * 24 * 60 * 60
    for path in root.rglob("*"):
        if path.is_file():
            os.utime(path, (old_time, old_time))

    print(root)


if __name__ == "__main__":
    main()
