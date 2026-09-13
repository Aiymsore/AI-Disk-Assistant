"""快照库：SQLite 记录一次扫描见到的每个文件与目录聚合，是 analyze 的唯一事实源。

设计要点：
- 文件表存"事实"（路径/大小/mtime），目录表存递归聚合（总大小/文件数/主要后缀），
  AI 判读与下钻查询都从这里取数，保证同一次分析内所有阶段看到同一份事实。
- mtime 仅入库备查，不参与任何判定（Windows 下不可靠，见 scanner.py 的说明）。
"""

from __future__ import annotations

import json
import sqlite3
import threading
from collections import Counter
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

_BATCH_SIZE = 2000
_KEEP_SNAPSHOTS = 3  # 快照库只保留最近 N 次扫描：快照可随时重扫，历史没有保留价值


@dataclass(slots=True)
class DirAgg:
    path: str
    parent: str
    total_size: int
    file_count: int
    top_suffixes: list[str]
    mtime_ns: int = 0


@dataclass(slots=True)
class FileRow:
    """files 表的单行事实：路径/名字/后缀/大小/修改时间。"""

    path: str
    name: str
    suffix: str
    size_bytes: int
    mtime_ns: int


class Inventory:
    """Small SQLite snapshot store keyed by snapshot id."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self._lock = threading.Lock()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=10)
        connection.execute("PRAGMA journal_mode=WAL")
        return connection

    def _initialize(self) -> None:
        with closing(self._connect()) as connection, connection:
            self._migrate(connection)
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS snapshots (
                    snapshot_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    root TEXT NOT NULL,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    file_count INTEGER NOT NULL DEFAULT 0
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS files (
                    snapshot_id INTEGER NOT NULL,
                    path TEXT NOT NULL,
                    parent TEXT NOT NULL,
                    name TEXT NOT NULL,
                    suffix TEXT NOT NULL,
                    size_bytes INTEGER NOT NULL,
                    mtime_ns INTEGER NOT NULL,
                    PRIMARY KEY (snapshot_id, path)
                )
                """
            )
            # 目录聚合（refresh_dir_aggregates）与下钻查询都按 (snapshot_id, parent) 取数；
            # 没有这个索引，全量快照（几十万文件）的聚合会对每个目录各做一次全表扫描。
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_files_snapshot_parent ON files(snapshot_id, parent)"
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS dirs (
                    snapshot_id INTEGER NOT NULL,
                    path TEXT NOT NULL,
                    parent TEXT NOT NULL,
                    total_size INTEGER NOT NULL,
                    file_count INTEGER NOT NULL,
                    mtime_ns INTEGER NOT NULL DEFAULT 0,
                    top_suffixes TEXT NOT NULL DEFAULT '[]',
                    PRIMARY KEY (snapshot_id, path)
                )
                """
            )

    @staticmethod
    def _migrate(connection: sqlite3.Connection) -> None:
        """旧库 schema 不兼容时直接重建：快照本身可随时重扫，丢弃历史是安全选择。"""
        columns = {
            row[1] for row in connection.execute("PRAGMA table_info(dirs)").fetchall()
        }
        if columns and "mtime_ns" not in columns:
            connection.execute("DROP TABLE dirs")
            connection.execute("DROP TABLE IF EXISTS mft_staging")

    def begin_snapshot(self, root: str) -> int:
        with self._lock, closing(self._connect()) as connection, connection:
            cursor = connection.execute("INSERT INTO snapshots(root) VALUES (?)", (root,))
            return int(cursor.lastrowid)

    def add_files(self, snapshot_id: int, rows: Iterable[tuple[str, str, str, str, int, int]]) -> int:
        """批量写入文件行，返回写入数量。行序：(path, parent, name, suffix, size_bytes, mtime_ns)。"""
        written = 0
        batch: list[tuple[int, str, str, str, str, int, int]] = []
        for row in rows:
            batch.append((snapshot_id, *row))
            if len(batch) >= _BATCH_SIZE:
                written += self._insert_files(batch)
                batch.clear()
        if batch:
            written += self._insert_files(batch)
        return written

    def _insert_files(self, batch: list[tuple[int, str, str, str, str, int, int]]) -> int:
        with self._lock, closing(self._connect()) as connection, connection:
            connection.executemany(
                "INSERT OR REPLACE INTO files(snapshot_id, path, parent, name, suffix, size_bytes, mtime_ns) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                batch,
            )
        return len(batch)

    def volume_usage(self, snapshot_id: int) -> tuple[int, int]:
        """全卷已用字节与文件总数（来自快照文件表聚合）。"""
        with closing(self._connect()) as connection:
            row = connection.execute(
                "SELECT IFNULL(SUM(size_bytes), 0), COUNT(*) FROM files WHERE snapshot_id = ?",
                (snapshot_id,),
            ).fetchone()
        return int(row[0]), int(row[1])

    @staticmethod
    def _subtree_prefix(directory: str) -> str:
        """子树前缀：保留调用方路径自身的分隔符风格（Windows 反斜杠 / 测试环境正斜杠）。"""
        if directory.endswith(("\\", "/")):
            return directory
        separator = "\\" if "\\" in directory else "/"
        return directory + separator

    def extension_stats(self, snapshot_id: int, directory: str, limit: int = 50) -> list[dict[str, object]]:
        """某个目录子树内按后缀聚合（大小降序）——扩展名分类面板用。"""
        prefix = self._subtree_prefix(directory)
        with closing(self._connect()) as connection:
            rows = connection.execute(
                """
                SELECT suffix, SUM(size_bytes) AS total, COUNT(*) AS cnt
                FROM files
                WHERE snapshot_id = ? AND substr(path, 1, ?) = ?
                GROUP BY suffix ORDER BY total DESC LIMIT ?
                """,
                (snapshot_id, len(prefix), prefix, limit),
            ).fetchall()
        return [
            {"suffix": row[0] or "<无后缀>", "size_bytes": int(row[1]), "file_count": int(row[2])}
            for row in rows
        ]

    def add_dirs(self, snapshot_id: int, paths: Iterable[str]) -> None:
        """登记目录（聚合值稍后由 refresh_dir_aggregates 统一计算）。"""
        with self._lock, closing(self._connect()) as connection, connection:
            connection.executemany(
                "INSERT OR REPLACE INTO dirs(snapshot_id, path, parent, total_size, file_count, top_suffixes) "
                "VALUES (?, ?, ?, 0, 0, '[]')",
                [(snapshot_id, path, str(Path(path).parent)) for path in paths],
            )

    def refresh_dir_aggregates(self, snapshot_id: int) -> None:
        """重算目录聚合：直接子项统计 → 后缀 top3 → 沿父链向祖先递归累加。"""
        with self._lock, closing(self._connect()) as connection, connection:
            connection.execute(
                """
                UPDATE dirs SET
                    total_size = IFNULL((SELECT SUM(f.size_bytes) FROM files f WHERE f.snapshot_id = ? AND f.parent = dirs.path), 0),
                    file_count = IFNULL((SELECT COUNT(*) FROM files f WHERE f.snapshot_id = ? AND f.parent = dirs.path), 0)
                WHERE snapshot_id = ?
                """,
                (snapshot_id, snapshot_id, snapshot_id),
            )
            direct = connection.execute(
                "SELECT path, parent, total_size, file_count FROM dirs WHERE snapshot_id = ?",
                (snapshot_id,),
            ).fetchall()
            suffix_rows = connection.execute(
                "SELECT parent, suffix, COUNT(*) FROM files WHERE snapshot_id = ? GROUP BY parent, suffix",
                (snapshot_id,),
            ).fetchall()

        totals = {path: [size, count] for path, _parent, size, count in direct}
        parent_of = {path: parent for path, parent, _size, _count in direct}
        # 按路径深度降序累加：最深的目录先把自己的总数交给父目录，父目录随后再向上交。
        for path, _parent, _size, _count in sorted(direct, key=lambda row: row[0].count("\\"), reverse=True):
            parent = parent_of.get(path)
            if parent in totals:
                totals[parent][0] += totals[path][0]
                totals[parent][1] += totals[path][1]

        counters: dict[str, Counter[str]] = {}
        for parent, suffix, count in suffix_rows:
            counters.setdefault(parent, Counter())[suffix or "<无后缀>"] += count

        updates = [
            (totals[path][0], totals[path][1],
             json.dumps([f"{suffix}×{count}" for suffix, count in counters.get(path, Counter()).most_common(3)]),
             snapshot_id, path)
            for path, _parent, _size, _count in direct
        ]
        with self._lock, closing(self._connect()) as connection, connection:
            connection.executemany(
                "UPDATE dirs SET total_size = ?, file_count = ?, top_suffixes = ? "
                "WHERE snapshot_id = ? AND path = ?",
                updates,
            )

    def finish_snapshot(self, snapshot_id: int, file_count: int) -> None:
        """落盘快照的文件总数（文件行与目录登记由 add_files/add_dirs 完成）。"""
        with self._lock, closing(self._connect()) as connection, connection:
            connection.execute("UPDATE snapshots SET file_count = ? WHERE snapshot_id = ?", (file_count, snapshot_id))

    def latest_snapshot(self) -> int | None:
        with closing(self._connect()) as connection:
            row = connection.execute("SELECT MAX(snapshot_id) FROM snapshots").fetchone()
        return int(row[0]) if row and row[0] is not None else None

    def prune_snapshots(self, keep: int = _KEEP_SNAPSHOTS) -> int:
        """只保留最近 keep 次快照，更旧的连同 files/dirs 行一并删除，返回删除的快照数。

        在快照成功落库后调用：旧快照随窗口滑出被回收，库体积不随扫描次数线性增长。
        DELETE 只把页标记为空闲（文件不再增长但也不回落），故有清理时跟进 VACUUM
        把空间真正还给文件系统；并发读会让 VACUUM 拿不到锁，跳过即可，下次扫描页会被复用。
        """
        keep = max(keep, 1)
        with self._lock, closing(self._connect()) as connection, connection:
            threshold = connection.execute(
                "SELECT MIN(snapshot_id) FROM "
                "(SELECT snapshot_id FROM snapshots ORDER BY snapshot_id DESC LIMIT ?)",
                (keep,),
            ).fetchone()[0]
            if threshold is None:
                return 0
            stale = connection.execute(
                "SELECT COUNT(*) FROM snapshots WHERE snapshot_id < ?", (threshold,)
            ).fetchone()[0]
            if not stale:
                return 0
            for table in ("files", "dirs", "snapshots"):
                connection.execute(f"DELETE FROM {table} WHERE snapshot_id < ?", (threshold,))
        try:
            with closing(self._connect()) as connection:
                connection.execute("VACUUM")
        except sqlite3.OperationalError:
            pass
        return stale

    def top_dirs(self, snapshot_id: int, limit: int, *, exclude: str | None = None) -> list[DirAgg]:
        """按递归总大小降序返回目录聚合；exclude 用于剔除根目录自身。"""
        query = "SELECT path, parent, total_size, file_count, top_suffixes, mtime_ns FROM dirs WHERE snapshot_id = ?"
        params: list[object] = [snapshot_id]
        if exclude:
            query += " AND path != ?"
            params.append(exclude)
        query += " ORDER BY total_size DESC LIMIT ?"
        params.append(limit)
        with closing(self._connect()) as connection:
            rows = connection.execute(query, params).fetchall()
        return [
            DirAgg(
                path=row[0],
                parent=row[1],
                total_size=int(row[2]),
                file_count=int(row[3]),
                top_suffixes=json.loads(row[4]),
                mtime_ns=int(row[5]),
            )
            for row in rows
        ]

    def child_dirs(self, snapshot_id: int, parent: str) -> list[DirAgg]:
        """某个目录的直接子目录（dirs 表存的是递归聚合值，直接可用作下钻摘要）。"""
        with closing(self._connect()) as connection:
            rows = connection.execute(
                "SELECT path, parent, total_size, file_count, top_suffixes, mtime_ns FROM dirs "
                "WHERE snapshot_id = ? AND parent = ? ORDER BY total_size DESC",
                (snapshot_id, parent),
            ).fetchall()
        return [
            DirAgg(
                path=row[0],
                parent=row[1],
                total_size=int(row[2]),
                file_count=int(row[3]),
                top_suffixes=json.loads(row[4]),
                mtime_ns=int(row[5]),
            )
            for row in rows
        ]

    def direct_files(self, snapshot_id: int, parent: str) -> list[FileRow]:
        """某个目录下的直接文件（不含子目录内容）——递归下钻时覆盖散文件用。"""
        return self._file_rows(
            "SELECT path, name, suffix, size_bytes, mtime_ns FROM files "
            "WHERE snapshot_id = ? AND parent = ?",
            (snapshot_id, parent),
        )

    def files_under(self, snapshot_id: int, directory: str) -> list[FileRow]:
        """某个目录子树内的全部文件（含各级子目录）——叶子区域评分用。"""
        prefix = self._subtree_prefix(directory)
        return self._file_rows(
            "SELECT path, name, suffix, size_bytes, mtime_ns FROM files "
            "WHERE snapshot_id = ? AND substr(path, 1, ?) = ?",
            (snapshot_id, len(prefix), prefix),
        )

    def _file_rows(self, query: str, params: tuple) -> list[FileRow]:
        with closing(self._connect()) as connection:
            rows = connection.execute(query, params).fetchall()
        return [FileRow(path=r[0], name=r[1], suffix=r[2], size_bytes=r[3], mtime_ns=r[4]) for r in rows]

    def duplicate_groups(
        self, snapshot_id: int, min_size_bytes: int, limit: int
    ) -> list[dict[str, object]]:
        """同体积组：体积完全相同的文件按大小聚合，按"去重可释放潜力"降序。

        同体积 ≠ 同内容（可能巧合），但它是零成本的最高价值线索——真正的去重判断
        交给 AI 判读与人工确认。返回 [{size_bytes, count, sample_paths, wasted_bytes}]。
        """
        with closing(self._connect()) as connection:
            rows = connection.execute(
                """
                SELECT size_bytes, COUNT(*) AS cnt FROM files
                WHERE snapshot_id = ? AND size_bytes >= ?
                GROUP BY size_bytes HAVING cnt >= 2
                ORDER BY size_bytes * cnt DESC LIMIT ?
                """,
                (snapshot_id, min_size_bytes, limit),
            ).fetchall()
            groups: list[dict[str, object]] = []
            for size, count in rows:
                samples = [
                    row[0]
                    for row in connection.execute(
                        "SELECT path FROM files WHERE snapshot_id = ? AND size_bytes = ? LIMIT 4",
                        (snapshot_id, size),
                    )
                ]
                groups.append(
                    {
                        "size_bytes": int(size),
                        "count": int(count),
                        "sample_paths": samples,
                        "wasted_bytes": int(size) * (int(count) - 1),
                    }
                )
        return groups
