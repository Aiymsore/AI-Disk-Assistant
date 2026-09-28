from __future__ import annotations

import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path

from ai_disk_assistant.cleaner import (
    RecycleInfo,
    plan_deletion,
    prune_empty_dirs,
    recycle_paths,
    split_permanent,
)
from ai_disk_assistant.inventory import FileRow, Inventory
from tests.test_inventory import seed_snapshot


class PlanDeletionTests(unittest.TestCase):
    """删除计划：目录子树展开去重、受保护拒绝、快照外拒绝。"""

    def setUp(self) -> None:
        self._stack = ExitStack()
        self.temp_dir = Path(self._stack.enter_context(tempfile.TemporaryDirectory())).resolve()
        inv_dir = Path(self._stack.enter_context(tempfile.TemporaryDirectory()))
        self.inventory = Inventory(inv_dir / "inv.sqlite3")
        self.addCleanup(self._stack.close)
        self.files = [
            ("junk/a.tmp", 100),
            ("junk/b.log", 200),
            ("junk/sub/c.log", 300),
            ("keep/note.txt", 50),
            ("mixed/ok.txt", 1),
            ("mixed/readme.md", 2),
            ("mixed/drivers/d.sys", 5),
            ("windows/system32/evil.dll", 10),
        ]
        self.dirs = ["junk", "junk/sub", "keep", "mixed", "mixed/drivers", "windows/system32"]
        self.snapshot_id = seed_snapshot(self.inventory, self.temp_dir, self.files, self.dirs)

    def _path(self, relative: str) -> str:
        return str(self.temp_dir / relative)

    def test_expands_directory_and_dedupes(self) -> None:
        plan = plan_deletion(
            self.inventory,
            self.snapshot_id,
            [self._path("junk"), self._path("junk/a.tmp")],
        )
        targets = {row.path for row in plan.targets}
        self.assertEqual(
            targets, {self._path("junk/a.tmp"), self._path("junk/b.log"), self._path("junk/sub/c.log")}
        )
        self.assertEqual(plan.total_bytes, 600)
        self.assertEqual(plan.accepted_dir_marks, [self._path("junk")])
        self.assertEqual(plan.accepted_file_marks, [self._path("junk/a.tmp")])
        self.assertEqual(plan.refused, [])

    def test_refuses_protected_directories_and_files(self) -> None:
        plan = plan_deletion(
            self.inventory,
            self.snapshot_id,
            [self._path("windows/system32"), self._path("windows/system32/evil.dll")],
        )
        self.assertEqual(plan.targets, [])
        self.assertEqual(plan.accepted_dir_marks, [])
        self.assertEqual(plan.accepted_file_marks, [])
        self.assertEqual(
            {path for path, _reason in plan.refused},
            {self._path("windows/system32"), self._path("windows/system32/evil.dll")},
        )
        self.assertTrue(all("受保护" in reason for _path, reason in plan.refused))

    def test_refuses_protected_files_inside_marked_directory(self) -> None:
        # 勾选的目录本身放行，但子树内的受保护文件逐个被拒（mixed/drivers/d.sys）。
        plan = plan_deletion(self.inventory, self.snapshot_id, [self._path("mixed")])
        targets = {row.path for row in plan.targets}
        self.assertEqual(targets, {self._path("mixed/ok.txt"), self._path("mixed/readme.md")})
        self.assertEqual(plan.refused, [(self._path("mixed/drivers/d.sys"), plan.refused[0][1])])
        self.assertIn("受保护", plan.refused[0][1])

    def test_refuses_paths_missing_from_snapshot(self) -> None:
        missing = self._path("ghost/never-scanned.bin")
        plan = plan_deletion(self.inventory, self.snapshot_id, [missing])
        self.assertEqual(plan.targets, [])
        self.assertEqual([path for path, _reason in plan.refused], [missing])
        self.assertIn("快照", plan.refused[0][1])


class RecyclePathsTests(unittest.TestCase):
    """执行与复核：分块调用 executor，按 verify 的存在性复核输出结果。"""

    def test_chunks_executor_calls_and_reports_deleted(self) -> None:
        calls: list[list[str]] = []
        paths = [f"/x/{index}" for index in range(1200)]  # > _CHUNK_SIZE(500)
        deleted, failed = recycle_paths(paths, executor=calls.append, verify=lambda _p: False)
        self.assertEqual(deleted, paths)
        self.assertEqual(failed, [])
        self.assertEqual([len(chunk) for chunk in calls], [500, 500, 200])
        self.assertEqual(calls[0][0], paths[0])

    def test_reports_failed_when_files_still_exist(self) -> None:
        paths = ["/x/a", "/x/b"]
        deleted, failed = recycle_paths(paths, executor=lambda _chunk: None, verify=lambda _p: True)
        self.assertEqual(deleted, [])
        self.assertEqual(failed, paths)

    def test_deduplicates_paths(self) -> None:
        deleted, _failed = recycle_paths(
            ["/x/a", "/x/a", "/x/b"], executor=lambda _chunk: None, verify=lambda _p: False
        )
        self.assertEqual(deleted, ["/x/a", "/x/b"])

    def test_progress_reports_cumulative_counts_after_each_chunk(self) -> None:
        seen: list[tuple[int, int]] = []
        recycle_paths(
            [f"/x/{index}" for index in range(1200)],
            executor=lambda _chunk: None,
            verify=lambda _p: False,
            progress=lambda done, total: seen.append((done, total)),
        )
        self.assertEqual(seen, [(500, 1200), (1000, 1200), (1200, 1200)])

    def test_progress_not_required(self) -> None:
        # 不传 progress 时行为与旧版一致，自定义 executor（单参数）不受新增关键字影响。
        deleted, _failed = recycle_paths(["/x/a"], executor=lambda _chunk: None, verify=lambda _p: False)
        self.assertEqual(deleted, ["/x/a"])


class SplitPermanentTests(unittest.TestCase):
    """回收站容量预测：超容量/不支持的卷 → 永久删除，剩余容量按执行顺序逐个扣减。"""

    @staticmethod
    def _rows(*sizes: int) -> list[FileRow]:
        return [
            FileRow(path=f"/x/{index}.bin", name=f"{index}.bin", suffix=".bin", size_bytes=size, mtime_ns=0)
            for index, size in enumerate(sizes)
        ]

    @staticmethod
    def _info(max_bytes: int | None, used_bytes: int = 0, nuke: bool = False):
        return lambda _root: RecycleInfo(max_bytes, used_bytes, nuke)

    def test_headroom_consumed_in_execution_order(self) -> None:
        rows = self._rows(60, 30, 20)  # 上限 100：前两个占满 90，第三个 20 放不下
        permanent, recyclable = split_permanent(rows, info_for_root=self._info(100))
        self.assertEqual([row.size_bytes for row in recyclable], [60, 30])
        self.assertEqual([row.size_bytes for row in permanent], [20])

    def test_used_bytes_shrink_headroom(self) -> None:
        rows = self._rows(40, 20)  # 上限 100 已占用 70 → 只剩 30
        permanent, recyclable = split_permanent(rows, info_for_root=self._info(100, used_bytes=70))
        self.assertEqual([row.size_bytes for row in recyclable], [20])
        self.assertEqual([row.size_bytes for row in permanent], [40])

    def test_nuke_on_delete_marks_everything_permanent(self) -> None:
        rows = self._rows(1, 2)
        permanent, recyclable = split_permanent(rows, info_for_root=self._info(1000, nuke=True))
        self.assertEqual(permanent, rows)
        self.assertEqual(recyclable, [])

    def test_unknown_capacity_is_conservatively_permanent(self) -> None:
        rows = self._rows(1)
        permanent, recyclable = split_permanent(rows, info_for_root=self._info(None))
        self.assertEqual(permanent, rows)
        self.assertEqual(recyclable, [])

    def test_queried_once_per_volume_root(self) -> None:
        calls: list[str] = []

        def factory(root: str) -> RecycleInfo:
            calls.append(root)
            return RecycleInfo(100, 0, False)

        split_permanent(self._rows(1, 2, 3), info_for_root=factory)
        self.assertEqual(len(calls), 1)  # 同一卷只读一次容量事实


class PruneEmptyDirsTests(unittest.TestCase):
    def test_removes_only_empty_directories(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            (root / "empty1").mkdir()
            (root / "nested/empty2").mkdir(parents=True)
            (root / "nested/file.txt").write_text("x", encoding="utf-8")
            removed = prune_empty_dirs(str(root))
            self.assertIn(str(root / "empty1"), removed)
            self.assertIn(str(root / "nested" / "empty2"), removed)
            self.assertFalse((root / "empty1").exists())
            self.assertFalse((root / "nested" / "empty2").exists())
            self.assertTrue((root / "nested" / "file.txt").exists())


class InventorySyncTests(unittest.TestCase):
    """删除落盘后按磁盘事实回删快照行，聚合与下钻随之更新。"""

    def test_remove_files_and_dir_subtree_updates_aggregates(self) -> None:
        stack = ExitStack()
        temp_dir = Path(stack.enter_context(tempfile.TemporaryDirectory())).resolve()
        inv_dir = Path(stack.enter_context(tempfile.TemporaryDirectory()))
        inventory = Inventory(inv_dir / "inv.sqlite3")
        snapshot_id = seed_snapshot(
            inventory,
            temp_dir,
            files=[("junk/a.tmp", 100), ("junk/b.log", 200), ("keep/note.txt", 50)],
            dirs=["junk", "keep"],
        )
        junk_dir = str(temp_dir / "junk")
        plan = plan_deletion(inventory, snapshot_id, [junk_dir])
        self.assertEqual(plan.total_bytes, 300)

        inventory.remove_files(snapshot_id, [row.path for row in plan.targets])
        inventory.remove_dir_subtree(snapshot_id, junk_dir)
        inventory.refresh_dir_aggregates(snapshot_id)

        usage, count = inventory.volume_usage(snapshot_id)
        self.assertEqual((usage, count), (50, 1))
        remaining = {row.path for row in inventory.child_dirs(snapshot_id, str(temp_dir))}
        self.assertNotIn(junk_dir, remaining)
        dirs, files = inventory.classify_paths(snapshot_id, [junk_dir, str(temp_dir / "junk/a.tmp")])
        self.assertEqual((dirs, files), (set(), set()))
        stack.close()


if __name__ == "__main__":
    unittest.main()
