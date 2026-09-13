from __future__ import annotations

import sqlite3
import struct
from contextlib import closing
import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path

from ai_disk_assistant.inventory import Inventory
from ai_disk_assistant.mft_scanner import (
    MftEntry,
    MftError,
    _INT64_MAX,
    parse_boot_sector,
    parse_record,
    parse_runlist,
    snapshot_volume,
    windows_to_ns,
)

# ── 合成 MFT 记录的构造器：覆盖 fixup / SI 时间 / FILE_NAME / 非常驻 DATA ────
def make_record(
    frn: int,
    parent: int = 5,
    name: str = "a.log",
    size: int = 100,
    is_dir: bool = False,
    mtime_windows: int = 132_000_000_000_000_000,
    namespace: int = 1,
    with_data: bool | None = None,
    magic: bytes = b"FILE",
    in_use: bool = True,
) -> bytes:
    record = bytearray(1024)
    record[0:4] = magic
    struct.pack_into("<H", record, 0x04, 0x30)  # usa offset
    struct.pack_into("<H", record, 0x06, 3)  # usa count：1 个校验值 + 2 个扇区尾
    struct.pack_into("<H", record, 0x14, 0x38)  # 第一个属性偏移
    # 记录标志：0x01 使用中（已释放记录会被 parse_record 跳过），0x02 目录
    struct.pack_into("<H", record, 0x16, (0x02 if is_dir else 0x00) | (0x01 if in_use else 0x00))
    # 扇区尾被校验值 0x1234 占用；usa 里保存的原始值是 0，解析时应回写为 0。
    struct.pack_into("<H", record, 0x30, 0x1234)
    record[510:512] = b"\x34\x12"
    record[1022:1024] = b"\x34\x12"

    attr_off = 0x38
    si = bytearray(0x40)
    struct.pack_into("<I", si, 0x00, 0x10)
    struct.pack_into("<I", si, 0x04, 0x40)
    struct.pack_into("<I", si, 0x10, 0x28)
    struct.pack_into("<H", si, 0x14, 0x18)
    struct.pack_into("<Q", si, 0x18 + 0x20, mtime_windows)
    record[attr_off : attr_off + 0x40] = si
    attr_off += 0x40

    name_bytes = name.encode("utf-16-le")
    content_size = 0x42 + len(name_bytes)
    attr_len = (0x18 + content_size + 7) & ~7
    fn = bytearray(attr_len)
    struct.pack_into("<I", fn, 0x00, 0x30)
    struct.pack_into("<I", fn, 0x04, attr_len)
    struct.pack_into("<H", fn, 0x14, 0x18)
    struct.pack_into("<I", fn, 0x10, content_size)
    struct.pack_into("<Q", fn, 0x18, parent)
    struct.pack_into("<Q", fn, 0x18 + 0x30, size)
    fn[0x18 + 0x40] = len(name)  # NTFS 语义：字符数，不是字节数
    fn[0x18 + 0x41] = namespace
    fn[0x18 + 0x42 : 0x18 + 0x42 + len(name_bytes)] = name_bytes
    record[attr_off : attr_off + attr_len] = fn
    attr_off += attr_len

    use_data = (size > 0 and not is_dir) if with_data is None else with_data
    if use_data:
        runlist = b"\x11\x01\x03" + b"\x00"
        da_len = 0x40 + len(runlist)
        da = bytearray(da_len)
        struct.pack_into("<I", da, 0x00, 0x80)
        struct.pack_into("<I", da, 0x04, da_len)
        da[0x08] = 1  # non-resident
        struct.pack_into("<H", da, 0x20, 0x40)  # runlist offset
        struct.pack_into("<Q", da, 0x28, size)  # allocated
        struct.pack_into("<Q", da, 0x30, size)  # real size
        struct.pack_into("<Q", da, 0x38, size)  # initialized
        da[0x40 : 0x40 + len(runlist)] = runlist
        record[attr_off : attr_off + da_len] = da
        attr_off += da_len

    struct.pack_into("<I", record, attr_off, 0xFFFFFFFF)
    return bytes(record)


def make_boot() -> bytes:
    boot = bytearray(4096)
    boot[3:7] = b"NTFS"
    struct.pack_into("<H", boot, 0x0B, 512)
    boot[0x0D] = 8  # cluster = 4096
    struct.pack_into("<q", boot, 0x30, 786_432)
    struct.pack_into("<b", boot, 0x40, -10)  # 真实 $Boot 布局：MFT 记录粒度字段在 0x40
    struct.pack_into("<b", boot, 0x44, 1)  # 0x44 是无关字节（曾误读此处导致粒度错 4 倍）：留陷阱值防回归
    return bytes(boot)


class ParseBootSectorTests(unittest.TestCase):
    def test_parses_offsets_and_sizes(self) -> None:
        mft_offset, record_size, cluster_size = parse_boot_sector(make_boot())
        self.assertEqual(mft_offset, 786_432 * 4096)
        self.assertEqual(record_size, 1024)
        self.assertEqual(cluster_size, 4096)

    def test_rejects_non_ntfs(self) -> None:
        with self.assertRaises(MftError):
            parse_boot_sector(b"\x00" * 16 + b"FAT32" + b"\x00" * 4075)


class ParseRunlistTests(unittest.TestCase):
    def test_single_run(self) -> None:
        self.assertEqual(parse_runlist(memoryview(b"\x11\x01\x03\x00")), [(3, 1)])

    def test_multi_run_with_negative_offset_and_sparse(self) -> None:
        runs = parse_runlist(memoryview(b"\x11\x01\x01\x21\x02\xfd\xff\x01\x01\x00"))
        self.assertEqual(runs[0], (1, 1))
        self.assertEqual(runs[1], (-2, 2))  # 相对偏移 -3 → lcn 1-3 = -2
        self.assertEqual(runs[2], (0, 1))  # 稀疏 run


class ParseRecordTests(unittest.TestCase):
    def test_extracts_all_fields(self) -> None:
        entry = parse_record(make_record(frn=7, parent=6, name="a.log", size=100), 7)
        self.assertIsNotNone(entry)
        self.assertEqual(entry.frn, 7)
        self.assertEqual(entry.parent_frn, 6)
        self.assertEqual(entry.name, "a.log")
        self.assertEqual(entry.suffix, ".log")
        self.assertEqual(entry.size_bytes, 100)
        self.assertFalse(entry.is_dir)
        self.assertEqual(entry.mtime_ns, 132_000_000_000_000_000 * 100 - 11_644_736_000_000_000_000)

    def test_directory_flag_and_no_data_stream(self) -> None:
        entry = parse_record(make_record(frn=6, name="cache", is_dir=True, with_data=False), 6)
        self.assertTrue(entry.is_dir)
        self.assertEqual(entry.size_bytes, 100)  # 回退到 FILE_NAME 里的真实大小

    def test_dos_name_used_only_as_fallback(self) -> None:
        dos_only = parse_record(
            make_record(frn=8, name="ABCDEF~1", namespace=2, size=7), 8
        )
        self.assertEqual(dos_only.name, "ABCDEF~1")

    def test_rejects_bad_magic_and_empty_name(self) -> None:
        self.assertIsNone(parse_record(make_record(frn=1, magic=b"BAAD"), 1))
        self.assertIsNone(parse_record(make_record(frn=2, name=""), 2))
        self.assertIsNone(parse_record(b"\x00" * 1024, 3))

    def test_rejects_freed_records(self) -> None:
        # 已删除文件只清"使用中"位，其余属性残留：计入会产生幽灵文件与重复路径。
        self.assertIsNone(parse_record(make_record(frn=4, in_use=False), 4))


class GarbageRecordTests(unittest.TestCase):
    """损坏/残留记录的垃圾时间与体积必须钳到 int64 内：真实卷扫描曾因
    SQLite 绑定 OverflowError 整盘失败。"""

    def test_windows_to_ns_clamps_extreme_values(self) -> None:
        self.assertEqual(windows_to_ns(0), 0)
        self.assertEqual(windows_to_ns(0xFFFFFFFFFFFFFFFF), _INT64_MAX)
        # 1970 年前的垃圾小值：换算为负，归零而不是保留负值（Windows 无法显示负时间戳）。
        self.assertEqual(windows_to_ns(0x100000000), 0)

    def test_garbage_fields_bind_to_sqlite(self) -> None:
        entry = parse_record(
            make_record(
                frn=9,
                name="corrupt.bin",
                size=0xFFFFFFFFFFFFFFFF,
                mtime_windows=0xFFFFFFFFFFFFFFFF,
            ),
            9,
        )
        self.assertIsNotNone(entry)
        self.assertEqual(entry.size_bytes, 0)  # 不可能的体积置 0，避免被打分当成巨型文件
        self.assertEqual(entry.mtime_ns, _INT64_MAX)
        for value in (entry.size_bytes, entry.mtime_ns):
            with closing(sqlite3.connect(":memory:")) as connection:
                connection.execute("SELECT ?", (value,)).fetchone()  # 超界会抛 OverflowError

    def test_small_garbage_mtime_binds_to_sqlite(self) -> None:
        entry = parse_record(
            make_record(frn=10, name="stale.bin", mtime_windows=0x100000000), 10
        )
        self.assertIsNotNone(entry)
        self.assertEqual(entry.mtime_ns, 0)
        with closing(sqlite3.connect(":memory:")) as connection:
            connection.execute("SELECT ?", (entry.mtime_ns,)).fetchone()


class SnapshotVolumeTests(unittest.TestCase):
    def test_writes_files_and_recursive_dir_totals(self) -> None:
        with ExitStack() as stack:
            inv_dir = Path(stack.enter_context(tempfile.TemporaryDirectory()))
            inventory = Inventory(inv_dir / "inv.sqlite3")
            entries = [
                MftEntry(100, 5, "cache", "", 0, 0, True),
                MftEntry(101, 100, "a.log", ".log", 100, 1, False),
                MftEntry(102, 100, "b.log", ".log", 50, 1, False),
                MftEntry(103, 5, "keep.txt", ".txt", 10, 1, False),
            ]
            snapshot_id, file_count = snapshot_volume("D:", inventory, entries=iter(entries))
            self.assertEqual(file_count, 3)

            dirs = {row.path: row for row in inventory.top_dirs(snapshot_id, limit=10)}
            self.assertEqual(dirs["D:\\cache"].total_size, 150)
            self.assertEqual(dirs["D:\\cache"].file_count, 2)
            self.assertIn(".log×2", dirs["D:\\cache"].top_suffixes)

            with closing(sqlite3.connect(inventory.path)) as connection:
                rows = connection.execute(
                    "SELECT path, parent FROM files WHERE snapshot_id = ? ORDER BY path",
                    (snapshot_id,),
                ).fetchall()
            self.assertEqual(
                rows,
                [
                    ("D:\\cache\\a.log", "D:\\cache"),
                    ("D:\\cache\\b.log", "D:\\cache"),
                    ("D:\\keep.txt", "D:\\"),
                ],
            )

    def test_meta_records_are_skipped(self) -> None:
        with ExitStack() as stack:
            inv_dir = Path(stack.enter_context(tempfile.TemporaryDirectory()))
            inventory = Inventory(inv_dir / "inv.sqlite3")
            entries = [
                MftEntry(0, 5, "$MFT", "", 999, 0, False),  # frn < 16 必须被丢弃
                MftEntry(2, 5, "$LogFile", "", 888, 0, False),
                MftEntry(20, 5, "ok.txt", ".txt", 1, 0, False),
            ]
            _snapshot_id, file_count = snapshot_volume("D:", inventory, entries=iter(entries))
            self.assertEqual(file_count, 1)

    def test_snapshot_volume_prunes_old_snapshots(self) -> None:
        with ExitStack() as stack:
            inv_dir = Path(stack.enter_context(tempfile.TemporaryDirectory()))
            inventory = Inventory(inv_dir / "inv.sqlite3")
            entries = [MftEntry(20, 5, "a.txt", ".txt", 1, 0, False)]
            for _ in range(4):
                snapshot_volume("D:", inventory, entries=iter(entries))
            with closing(sqlite3.connect(inventory.path)) as connection:
                count = connection.execute("SELECT COUNT(*) FROM snapshots").fetchone()[0]
            self.assertEqual(count, 3)  # 默认只保留最近 3 次快照

    def test_failed_scan_leaves_no_snapshot_row(self) -> None:
        with ExitStack() as stack:
            inv_dir = Path(stack.enter_context(tempfile.TemporaryDirectory()))
            inventory = Inventory(inv_dir / "inv.sqlite3")

            def broken_stream():
                yield MftEntry(20, 5, "a.txt", ".txt", 1, 0, False)
                raise RuntimeError("模拟中途崩溃")

            with self.assertRaises(RuntimeError):
                snapshot_volume("D:", inventory, entries=broken_stream())
            with closing(sqlite3.connect(inventory.path)) as connection:
                count = connection.execute("SELECT COUNT(*) FROM snapshots").fetchone()[0]
            self.assertEqual(count, 0)  # 空壳快照已清理，不会被 latest_snapshot 选中

    def test_duplicate_paths_collapse_instead_of_crashing(self) -> None:
        # 损坏卷可能出现同路径的两条记录（如删了又建）：快照语义是一路径一行，覆盖而非崩溃。
        with ExitStack() as stack:
            inv_dir = Path(stack.enter_context(tempfile.TemporaryDirectory()))
            inventory = Inventory(inv_dir / "inv.sqlite3")
            entries = [
                MftEntry(30, 5, "same.txt", ".txt", 10, 1, False),
                MftEntry(31, 5, "same.txt", ".txt", 20, 1, False),
            ]
            _snapshot_id, file_count = snapshot_volume("D:", inventory, entries=iter(entries))
            self.assertEqual(file_count, 2)  # 暂存按记录计数
            with closing(sqlite3.connect(inventory.path)) as connection:
                rows = connection.execute(
                    "SELECT size_bytes FROM files WHERE path = 'D:\\same.txt'"
                ).fetchall()
            self.assertEqual(len(rows), 1)  # files 表一路径一行

    def test_staging_has_parent_index_for_recursive_join(self) -> None:
        # 递归 CTE 每出队一条都按 parent 查子节点：无索引时全量快照（85 万条）会卡死在重建路径。
        with ExitStack() as stack:
            inv_dir = Path(stack.enter_context(tempfile.TemporaryDirectory()))
            inventory = Inventory(inv_dir / "inv.sqlite3")
            entries = [MftEntry(20, 5, "a.txt", ".txt", 1, 0, False)]
            snapshot_volume("D:", inventory, entries=iter(entries))
            with closing(sqlite3.connect(inventory.path)) as connection:
                plan = connection.execute(
                    "EXPLAIN QUERY PLAN SELECT * FROM mft_staging WHERE parent = 5"
                ).fetchall()
            self.assertTrue(any("idx_mft_staging_parent" in str(row) for row in plan), plan)




RECORD_SIZE = 1024


def make_record0(mft_total: int, run_lcn: int, run_clusters: int) -> bytes:
    """$MFT 的 0 号记录：非常驻 $DATA 指向整个 MFT 区域。"""
    rec = bytearray(RECORD_SIZE)
    rec[0:4] = b"FILE"
    struct.pack_into("<H", rec, 0x04, 0x30)
    struct.pack_into("<H", rec, 0x06, 3)
    struct.pack_into("<H", rec, 0x14, 0x38)
    struct.pack_into("<H", rec, 0x30, 0x1234)
    rec[510:512] = b"\x34\x12"
    rec[1022:1024] = b"\x34\x12"
    runlist = bytes([0x11, run_clusters, run_lcn]) + b"\x00"
    da_len = 0x40 + len(runlist)
    da = bytearray(da_len)
    struct.pack_into("<I", da, 0x00, 0x80)
    struct.pack_into("<I", da, 0x04, da_len)
    da[0x08] = 1
    struct.pack_into("<H", da, 0x20, 0x40)
    struct.pack_into("<Q", da, 0x28, mft_total)
    struct.pack_into("<Q", da, 0x30, mft_total)
    struct.pack_into("<Q", da, 0x38, mft_total)
    da[0x40 : 0x40 + len(runlist)] = runlist
    rec[0x38 : 0x38 + da_len] = da
    struct.pack_into("<I", rec, 0x38 + da_len, 0xFFFFFFFF)
    return bytes(rec)




class SyntheticVolumeTests(unittest.TestCase):
    """合成整卷 $MFT 镜像端到端：卷读取 → 记录流 → 快照落库。"""

    def test_snapshot_volume_over_fake_image(self) -> None:
        import io
        import tempfile

        import ai_disk_assistant.mft_scanner as m

        cluster = 4096
        mft_lcn = 100
        records = {
            5: make_record(frn=5, parent=5, name=".", is_dir=True, with_data=False),
            100: make_record(frn=100, parent=5, name="big", is_dir=True, with_data=False),
            101: make_record(frn=101, parent=100, name="sub", is_dir=True, with_data=False),
            102: make_record(frn=102, parent=101, name="junk.log", size=100),
            103: make_record(frn=103, parent=100, name="loose.tmp", size=10),
            104: make_record(frn=104, parent=5, name="other", is_dir=True, with_data=False),
            105: make_record(frn=105, parent=104, name="note.txt", size=5),
            106: make_record(
                frn=106,
                parent=5,
                name="corrupt.bin",
                size=0xFFFFFFFFFFFFFFFF,
                mtime_windows=0xFFFFFFFFFFFFFFFF,
            ),  # 损坏记录混入整卷：扫描必须照常完成而不是 OverflowError
            107: make_record(
                frn=107,
                parent=5,
                name="stale.bin",
                mtime_windows=0x100000000,
            ),  # 1970 年前的垃圾小时间戳：负向越界样本
        }
        max_frn = max(records) + 2
        mft_region = bytearray()
        for frn in range(max_frn + 1):
            mft_region += records.get(frn, b"\x00" * RECORD_SIZE)
        while len(mft_region) % cluster:
            mft_region += b"\x00"
        run_clusters = len(mft_region) // cluster
        records[0] = make_record0(len(mft_region), mft_lcn, run_clusters)
        # 0 号记录占据区域头部原本为全零的 frn=0 槽位（$MFT 自身）。
        mft_region[0:RECORD_SIZE] = records[0]

        image = bytearray(mft_lcn * cluster + len(mft_region))
        image[3:7] = b"NTFS"
        struct.pack_into("<H", image, 0x0B, 512)
        image[0x0D] = 8
        struct.pack_into("<q", image, 0x30, mft_lcn)
        struct.pack_into("<b", image, 0x40, -10)
        struct.pack_into("<b", image, 0x44, 1)  # 与真实卷一致：0x44 是无关字节
        image[mft_lcn * cluster : mft_lcn * cluster + len(mft_region)] = mft_region

        original_read = m.read_volume_bytes
        original_open = m._open_volume
        m.read_volume_bytes = lambda drive, offset, size: bytes(image[offset : offset + size])
        m._open_volume = lambda drive: io.BytesIO(bytes(image))
        try:
            with tempfile.TemporaryDirectory() as temp_dir:
                inventory = Inventory(Path(temp_dir) / "inv.sqlite3")
                snapshot_id, file_count = m.snapshot_volume("C:", inventory)
                self.assertEqual(file_count, 5)
                dirs = {
                    Path(row.path).name: row.total_size
                    for row in inventory.top_dirs(snapshot_id, 10, exclude="C:\\")
                }
                self.assertEqual(dirs["big"], 110)  # 递归聚合：sub 的 100 + 散文件 10
        finally:
            m.read_volume_bytes = original_read
            m._open_volume = original_open


if __name__ == "__main__":
    unittest.main()
