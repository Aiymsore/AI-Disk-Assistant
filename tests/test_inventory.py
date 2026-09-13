from __future__ import annotations

import sqlite3
import tempfile
import unittest
from contextlib import ExitStack, closing
from pathlib import Path

from ai_disk_assistant.inventory import Inventory


def seed_snapshot(inventory: Inventory, root: Path, files: list[tuple[str, int]], dirs: list[str]) -> int:
    """把合成布局写入快照库（与 snapshot_volume 的产物同构），返回 snapshot_id。

    files: [(相对路径, size)]；dirs: 相对目录列表。
    """
    # 统一长短路径：GitHub runner 的 TEMP 返回 8.3 短名（RUNNER~1），若文件与目录
    # 一边 resolve 一边不 resolve，父子路径匹配会全部失灵（聚合为 0、下钻为空）。
    root = Path(root).resolve()
    snapshot_id = inventory.begin_snapshot(str(root))
    file_rows = []
    dir_rows = []
    for relative, size in files:
        full = str(root / relative)
        name = relative.rsplit("/", 1)[-1]
        suffix = "." + name.rsplit(".", 1)[-1].lower() if "." in name else ""
        parent = str((root / relative).parent)
        file_rows.append((full, parent, name, suffix, size, 0))
    for relative in dirs:
        full = str(root / relative)
        dir_rows.append((full, str((root / relative).parent), 0, 0, []))
    inventory.add_files(snapshot_id, file_rows)
    inventory.add_dirs(snapshot_id, [str((root / relative).resolve()) for relative in dirs])
    inventory.refresh_dir_aggregates(snapshot_id)
    inventory.finish_snapshot(snapshot_id, len(file_rows))
    return snapshot_id


class InventoryQueryTests(unittest.TestCase):
    def setUp(self) -> None:
        self._stack = ExitStack()
        self.temp_dir = Path(self._stack.enter_context(tempfile.TemporaryDirectory()))
        inv_dir = Path(self._stack.enter_context(tempfile.TemporaryDirectory()))
        self.inventory = Inventory(inv_dir / "inv.sqlite3")
        # 布局：big/sub7(8KB log)、big/sub6(7KB log)、big 散文件 loose_old.tmp、other/note.txt + 同体积组
        self.snapshot_id = seed_snapshot(
            self.inventory,
            self.temp_dir,
            files=[
                ("big/sub7/junk.log", 8192),
                ("big/sub6/junk.log", 7168),
                ("big/loose_old.tmp", 512),
                ("other/note.txt", 4096),
                ("other/copy1.bin", 1024 * 1024 + 7),
                ("other/copy2.bin", 1024 * 1024 + 7),
            ],
            dirs=["big", "big/sub7", "big/sub6", "other"],
        )
        self.addCleanup(self._stack.close)

    def test_top_dirs_sorted_by_recursive_size(self) -> None:
        rows = self.inventory.top_dirs(self.snapshot_id, limit=10, exclude=str(self.temp_dir))
        names = [Path(row.path).name for row in rows]
        # other 带两个 1MB 同体积文件，是最大目录；big 次之但远小于 other。
        self.assertEqual(names[0], "other")
        self.assertIn("big", names)
        big = next(row for row in rows if Path(row.path).name == "big")
        self.assertEqual(big.total_size, 8192 + 7168 + 512)

    def test_child_dirs_returns_direct_children_only(self) -> None:
        big = str(self.temp_dir / "big")
        children = self.inventory.child_dirs(self.snapshot_id, big)
        self.assertEqual([Path(child.path).name for child in children], ["sub7", "sub6"])
        self.assertEqual(children[0].total_size, 8192)

    def test_direct_files_vs_files_under(self) -> None:
        big = str(self.temp_dir / "big")
        direct = {row.name for row in self.inventory.direct_files(self.snapshot_id, big)}
        self.assertEqual(direct, {"loose_old.tmp"})
        under = {row.name for row in self.inventory.files_under(self.snapshot_id, big)}
        self.assertEqual(under, {"loose_old.tmp", "junk.log"})

    def test_duplicate_groups_by_size(self) -> None:
        groups = self.inventory.duplicate_groups(self.snapshot_id, min_size_bytes=1024, limit=10)
        self.assertEqual(len(groups), 1)
        self.assertEqual(groups[0]["count"], 2)
        self.assertEqual(groups[0]["wasted_bytes"], 1024 * 1024 + 7)
        # 阈值抬高后无组。
        self.assertEqual(self.inventory.duplicate_groups(self.snapshot_id, min_size_bytes=2 * 1024 * 1024, limit=10), [])


class PruneSnapshotsTests(unittest.TestCase):
    def setUp(self) -> None:
        self._stack = ExitStack()
        inv_dir = Path(self._stack.enter_context(tempfile.TemporaryDirectory()))
        self.inventory = Inventory(inv_dir / "inv.sqlite3")
        self.addCleanup(self._stack.close)

    def _seed(self, tag: str) -> int:
        return seed_snapshot(self.inventory, Path("C:\\snap-" + tag), [(f"{tag}.log", 10)], [])

    def test_keeps_recent_and_removes_stale_with_rows(self) -> None:
        first = self._seed("a")
        second = self._seed("b")
        third = self._seed("c")
        self.assertEqual(self.inventory.prune_snapshots(keep=2), 1)
        self.assertEqual(self.inventory.latest_snapshot(), third)
        self.assertEqual(self.inventory.volume_usage(first), (0, 0))  # 旧行连同快照一并删除
        self.assertEqual(self.inventory.volume_usage(second), (10, 1))
        self.assertEqual(self.inventory.volume_usage(third), (10, 1))

    def test_noop_when_within_limit_or_empty(self) -> None:
        self.assertEqual(self.inventory.prune_snapshots(keep=3), 0)  # 空库不报错
        snapshot_id = self._seed("a")
        self.assertEqual(self.inventory.prune_snapshots(keep=3), 0)
        self.assertEqual(self.inventory.volume_usage(snapshot_id), (10, 1))

    def test_files_have_parent_index(self) -> None:
        # 目录聚合与下钻按 (snapshot_id, parent) 取数：缺索引时全量快照会按目录数全表扫描。
        with closing(sqlite3.connect(self.inventory.path)) as connection:
            indexes = {row[1] for row in connection.execute("PRAGMA index_list(files)")}
        self.assertIn("idx_files_snapshot_parent", indexes)


if __name__ == "__main__":
    unittest.main()
