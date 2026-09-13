"""MFT 扫描后端：直读 NTFS 卷的 $MFT，秒级产出全卷文件清单（inventory 的第二写入方）。

原理（与 WizTree/Everything 同路）：绕过文件系统 API，直接读 NTFS 主文件表——
每个文件/目录在 $MFT 里有定长记录，含父目录引用、文件名、大小、时间，
整表顺序读出后按父引用重建路径。需要管理员权限；仅 NTFS。
exFAT/FAT/网络盘没有 $MFT，调用方应回退 inventory.snapshot_tree（scandir 遍历）。

分层：
- 纯函数（parse_boot_sector / parse_runlist / parse_record）：字节解析，任何平台可测；
- 卷 IO（_open_volume / read_volume_bytes / iter_mft_entries）：Windows 专属，需管理员；
- snapshot_volume：staging 表 → SQLite 递归 CTE 重建路径 → files/dirs 落库。
  路径重建放在 SQL 里是刻意的：百万级文件在 32 位 Python 里组路径会撑爆地址空间，
  sqlite 的磁盘换内存让全卷扫描的内存占用保持恒定。

已知限制（有意取舍，v1）：
- $ATTRIBUTE_LIST 扩展记录不解析（极少数属性溢出的记录缺名，跳过不计）；
- 文件大小取 $DATA 非常驻真实大小，缺失时退回 $FILE_NAME 里的值（关闭时落盘，可能略旧）；
- 硬链接只计第一个名字；压缩/稀疏文件报告逻辑大小而非占用大小；
- 元文件（frn < 16，如 $MFT 自身）整体跳过——清理工具永远不该碰它们。
"""

from __future__ import annotations

import sqlite3
import struct
from dataclasses import dataclass
from typing import Callable, Iterator

from .inventory import Inventory

_WINDOWS_EPOCH_DELTA = 11_644_736_000_000_000_000  # 1601-01-01 → 1970-01-01，单位 100ns
_INT64_MAX = 2**63 - 1  # SQLite INTEGER 上限：$MFT 损坏记录的时间/体积可越界，入库前必须钳制
_STAGING_BATCH = 20_000
_READ_CHUNK = 8 * 1024 * 1024
_MAX_PATH_DEPTH = 64
_ROOT_FRN = 5
_META_FRN_LIMIT = 16


class MftError(RuntimeError):
    """MFT 扫描的前置条件不满足（非 NTFS / 无管理员权限 / 非 Windows）。"""


class ScanCancelled(MftError):
    """扫描被用户取消；半成品快照由调用方负责清理。"""


class ScanControl:
    """扫描过程的暂停/继续/取消控制 + 确定性进度（0~1），线程安全。

    检查点粒度为批/块边界（约每 2 万条或 8MB），暂停在检查点挂起工作线程，
    不烧 CPU；取消抛出 ScanCancelled。progress 由扫描线程持续更新。
    """

    def __init__(self) -> None:
        import threading

        self._paused = threading.Event()
        self._cancelled = threading.Event()
        self.progress = 0.0

    def pause(self) -> None:
        self._paused.set()

    def resume(self) -> None:
        self._paused.clear()

    def cancel(self) -> None:
        self._cancelled.set()
        self._paused.set()  # 唤醒挂起的暂停，让它走到取消分支

    def checkpoint(self) -> None:
        if self._cancelled.is_set():
            raise ScanCancelled("扫描已取消")
        if self._paused.is_set():
            self._paused.wait()
            if self._cancelled.is_set():
                raise ScanCancelled("扫描已取消")


@dataclass(slots=True)
class MftEntry:
    frn: int
    parent_frn: int
    name: str
    suffix: str
    size_bytes: int
    mtime_ns: int
    is_dir: bool


# ── 纯函数：字节解析（任何平台可测）───────────────────────────────────────
def parse_boot_sector(boot: bytes) -> tuple[int, int, int]:
    """从卷引导扇区解析 (mft_offset 字节地址, record_size 字节, cluster_size 字节)。"""
    if boot[3:7] != b"NTFS":
        raise MftError("卷不是 NTFS（引导扇区缺少 NTFS 标记）")
    bytes_per_sector = struct.unpack_from("<H", boot, 0x0B)[0]
    sectors_per_cluster = boot[0x0D]
    mft_lcn = struct.unpack_from("<q", boot, 0x30)[0]
    # $Boot 0x40 = 每个 MFT 记录占用的簇数（有符号；负值表示记录大小 = 2^-n 字节）。
    # 注意不是 0x44——那里是无关字节，读错会把 1KB 记录卷当成 4KB，frn 整体错位 4 倍。
    clusters_per_record = struct.unpack_from("<b", boot, 0x40)[0]
    cluster_size = bytes_per_sector * sectors_per_cluster
    record_size = clusters_per_record * cluster_size if clusters_per_record > 0 else 2 ** (-clusters_per_record)
    return mft_lcn * cluster_size, record_size, cluster_size


def parse_runlist(data: memoryview) -> list[tuple[int, int]]:
    """解析非常驻属性的 runlist，返回 [(lcn 相对偏移, cluster 长度)]；稀疏 run 记为 (0, 0)。"""
    runs: list[tuple[int, int]] = []
    lcn = 0
    pos = 0
    while pos < len(data):
        header = data[pos]
        if header == 0:
            break
        length_size = header & 0x0F  # 低 4 位：length 字段宽度
        offset_size = header >> 4  # 高 4 位：offset 字段宽度（有符号，cluster 相对偏移）
        pos += 1
        if length_size == 0:
            break
        length = int.from_bytes(data[pos : pos + length_size], "little")
        pos += length_size
        if offset_size == 0:
            runs.append((0, length))  # 稀疏 run：有长度、无磁盘数据
        else:
            offset = int.from_bytes(data[pos : pos + offset_size], "little", signed=True)
            pos += offset_size
            lcn += offset
            runs.append((lcn, length))
    return runs


def windows_to_ns(value: int) -> int:
    if value <= 0:
        return 0
    ns = value * 100 - _WINDOWS_EPOCH_DELTA
    if ns < 0:
        # 1970 年前的换算结果为负：基本是垃圾值，且负时间戳在 Windows 上无法显示
        # （time.localtime 直接抛 OSError），统一归零。
        return 0
    # 2262 年后纳秒时间戳超 int64，钳到 SQLite 可存上限，否则绑定抛 OverflowError。
    return min(ns, _INT64_MAX)


def parse_record(record: bytes, frn: int) -> MftEntry | None:
    """解析单个 MFT 记录；非 FILE 记录/无法取名的记录返回 None。frn 即记录序号。"""
    if len(record) < 0x30 or record[:4] != b"FILE":
        return None

    # 修复扇区尾（update sequence array）：每个 512B 扇区最后两个字节被校验值占用。
    usa_offset = struct.unpack_from("<H", record, 0x04)[0]
    usa_count = struct.unpack_from("<H", record, 0x06)[0]
    fixed = bytearray(record)
    for index in range(1, usa_count):
        sector_tail = index * 512 - 2
        if sector_tail + 2 > len(fixed) or usa_offset + index * 2 + 2 > len(fixed):
            break
        fixed[sector_tail : sector_tail + 2] = record[usa_offset + index * 2 : usa_offset + index * 2 + 2]

    flags = struct.unpack_from("<H", fixed, 0x16)[0]
    is_dir = bool(flags & 0x02)
    if not flags & 0x01:
        # 已释放记录只清"使用中"位，旧文件名/父引用/大小原样残留：把它当文件入库
        # 会产生磁盘上不存在的幽灵路径，还会与同名现役记录在 files 表撞唯一键。
        return None
    attr_off = struct.unpack_from("<H", fixed, 0x14)[0]

    si_mtime_ns: int | None = None
    posix_name: tuple[str, int, int, int] | None = None  # (name, parent, real_size, mtime_ns)
    dos_name: tuple[str, int, int, int] | None = None
    data_size: int | None = None

    while 0 < attr_off <= len(fixed) - 16:
        attr_type = struct.unpack_from("<I", fixed, attr_off)[0]
        if attr_type == 0xFFFFFFFF:
            break
        attr_len = struct.unpack_from("<I", fixed, attr_off + 4)[0]
        if attr_len == 0 or attr_off + attr_len > len(fixed):
            break
        non_resident = fixed[attr_off + 0x08]
        content_off = struct.unpack_from("<H", fixed, attr_off + 0x14)[0]
        content = attr_off + content_off

        if attr_type == 0x10 and not non_resident and si_mtime_ns is None:
            si_mtime_ns = windows_to_ns(struct.unpack_from("<Q", fixed, content + 0x20)[0])
        elif attr_type == 0x30 and not non_resident:
            parent = struct.unpack_from("<Q", fixed, content)[0] & 0x0000FFFFFFFFFFFF
            name_len = fixed[content + 0x40]
            namespace = fixed[content + 0x41]
            raw_name = bytes(fixed[content + 0x42 : content + 0x42 + name_len * 2])
            name = raw_name.decode("utf-16-le", errors="replace")
            real_size = struct.unpack_from("<Q", fixed, content + 0x30)[0]
            fn_mtime = windows_to_ns(struct.unpack_from("<Q", fixed, content + 0x18)[0])
            candidate = (name, parent, real_size, fn_mtime)
            if namespace == 2:  # DOS 8.3 别名：只作回退
                dos_name = dos_name or candidate
            else:
                posix_name = posix_name or candidate
        elif attr_type == 0x80 and non_resident and data_size is None:
            data_size = struct.unpack_from("<q", fixed, attr_off + 0x30)[0]

        attr_off += attr_len

    chosen = posix_name or dos_name
    if chosen is None:
        return None
    name, parent, fn_size, fn_mtime = chosen
    if not name:
        return None
    size = data_size if data_size is not None and data_size >= 0 else fn_size
    if size > _INT64_MAX:
        size = 0  # 损坏记录的不可能体积：钳到上限会被打分当成巨型文件，置 0 更干净。
    suffix = "." + name.rsplit(".", 1)[-1].lower() if "." in name else ""
    return MftEntry(
        frn=frn,
        parent_frn=parent,
        name=name,
        suffix=suffix,
        size_bytes=size,
        mtime_ns=si_mtime_ns if si_mtime_ns is not None else fn_mtime,
        is_dir=is_dir,
    )


# ── 卷 IO（Windows 专属，需管理员）────────────────────────────────────────
def _open_volume(drive_letter: str):
    import ctypes
    import msvcrt
    import os

    clean = drive_letter.rstrip(":\\")
    target = "\\\\.\\{}:".format(clean)
    handle = ctypes.windll.kernel32.CreateFileW(
        target,
        0x80000000,  # GENERIC_READ
        1 | 2,  # FILE_SHARE_READ | FILE_SHARE_WRITE
        None,
        3,  # OPEN_EXISTING
        0,
        None,
    )
    if handle in (-1, 0xFFFFFFFFFFFFFFFF):
        error = ctypes.windll.kernel32.GetLastError()
        if error in (5, 1314):  # ERROR_ACCESS_DENIED / ERROR_PRIVILEGE_NOT_HELD
            raise MftError("读取 $MFT 需要管理员权限：请以管理员身份运行，或去掉 --mft 回退普通扫描。")
        raise MftError(f"无法打开卷 {target}（WinError {error}）")
    fd = msvcrt.open_osfhandle(handle, os.O_RDONLY)
    return os.fdopen(fd, "rb")


def volume_capacity(drive_letter: str) -> tuple[int, int, int]:
    """卷容量（总/可用/已用 字节），来自 GetDiskFreeSpaceExW；非 Windows 返回 (0, 0, 0)。"""
    try:
        import ctypes

        root = drive_letter.rstrip(":\\") + ":\\"
        total = ctypes.c_longlong(0)
        free = ctypes.c_longlong(0)
        ctypes.windll.kernel32.GetDiskFreeSpaceExW(root, None, ctypes.byref(total), ctypes.byref(free))
        used = total.value - free.value
        return total.value, free.value, used
    except (AttributeError, OSError):
        return 0, 0, 0


def is_ntfs_volume(drive_letter: str) -> bool:
    import ctypes

    root = drive_letter.rstrip(":\\") + ":\\"
    fs_name = ctypes.create_unicode_buffer(64)
    ok = ctypes.windll.kernel32.GetVolumeInformationW(root, None, 0, None, None, None, fs_name, 64)
    return bool(ok) and fs_name.value.upper() == "NTFS"


def read_volume_bytes(drive_letter: str, offset: int, size: int) -> bytes:
    with _open_volume(drive_letter) as volume:
        volume.seek(offset)
        return volume.read(size)


def _mft_geometry(drive_letter: str) -> tuple[int, int, list[tuple[int, int]], int]:
    """解析引导扇区与 $MFT 0 号记录，返回 (record_size, cluster_size, runs, mft_total 字节)。"""
    boot = read_volume_bytes(drive_letter, 0, 4096)
    mft_offset, record_size, cluster_size = parse_boot_sector(boot)
    record0 = read_volume_bytes(drive_letter, mft_offset, record_size)
    data_attr = None
    attr_off = struct.unpack_from("<H", record0, 0x14)[0]
    while 0 < attr_off <= record_size - 16:
        attr_type = struct.unpack_from("<I", record0, attr_off)[0]
        if attr_type == 0xFFFFFFFF:
            break
        attr_len = struct.unpack_from("<I", record0, attr_off + 4)[0]
        if attr_len == 0:
            break
        if attr_type == 0x80 and record0[attr_off + 0x08] == 1:
            data_attr = record0[attr_off : attr_off + attr_len]
            break
        attr_off += attr_len
    if data_attr is None:
        raise MftError("在 $MFT 的 0 号记录中找不到 $DATA 属性。")
    runs = parse_runlist(memoryview(data_attr)[struct.unpack_from("<H", data_attr, 0x20)[0] :])
    mft_total = struct.unpack_from("<q", data_attr, 0x30)[0]
    return record_size, cluster_size, runs, mft_total


def _read_mft_image(drive_letter: str) -> Iterator[bytes]:
    """定位并顺序产出 $MFT 的原始字节流（按 cluster run 顺序）。"""
    _record_size, cluster_size, runs, _mft_total = _mft_geometry(drive_letter)

    with _open_volume(drive_letter) as volume:
        for lcn_offset, length in runs:
            if length <= 0:
                continue
            volume.seek(lcn_offset * cluster_size)
            run_bytes = length * cluster_size
            while run_bytes > 0:
                chunk = volume.read(min(_READ_CHUNK, run_bytes))
                if not chunk:
                    break
                run_bytes -= len(chunk)
                yield chunk


def iter_mft_entries(
    drive_letter: str,
    progress: Callable[[str], None] | None = None,
    control: "ScanControl | None" = None,
) -> Iterator[MftEntry]:
    """按记录序号（frn）流式产出整卷的 MftEntry；无法解析的记录跳过。

    每条记录边界都是一个控制检查点：暂停在此挂起，取消抛 ScanCancelled；
    control.progress 随解析进度更新（0~1）。
    """
    record_size, _cluster, _runs, mft_total = _mft_geometry(drive_letter)
    total_records = max(mft_total // record_size, 1)
    buffer = b""
    frn = 0
    for chunk in _read_mft_image(drive_letter):
        if control:
            control.checkpoint()
        buffer = buffer + chunk if buffer else chunk
        count = len(buffer) // record_size
        if not count:
            continue
        for index in range(count):
            if control:
                control.checkpoint()
                control.progress = frn / total_records
            entry = parse_record(buffer[index * record_size : (index + 1) * record_size], frn)
            frn += 1
            if entry is not None:
                if progress and frn % 500_000 == 0:
                    progress(f"MFT 已解析 {frn}/{total_records} 条记录（{frn * 100 // total_records}%）……")
                yield entry
        buffer = buffer[count * record_size :]


# ── 快照写入：inventory 的第二写入方 ──────────────────────────────────────
def snapshot_volume(
    drive_letter: str,
    inventory: Inventory,
    progress: Callable[[str], None] | None = None,
    *,
    control: ScanControl | None = None,
    entries: Iterator[MftEntry] | None = None,
) -> tuple[int, int]:
    """直读整卷 $MFT 并写入 inventory，返回 (snapshot_id, file_count)。

    与 inventory.snapshot_tree 产物等价（files 行 + dirs 递归聚合），但数据来源是
    $MFT。路径重建通过 SQLite 递归 CTE 在磁盘上完成，内存占用恒定。
    entries 参数仅供测试注入合成条目；生产路径使用 iter_mft_entries。
    """
    log = progress or (lambda _message: None)
    letter = drive_letter.strip()
    drive_prefix = letter.rstrip(":\\") + ":\\"
    entry_stream = entries if entries is not None else iter_mft_entries(letter, log, control)

    snapshot_id = inventory.begin_snapshot(drive_prefix)
    connection = sqlite3.connect(inventory.path, timeout=30)
    try:
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS mft_staging (
                frn INTEGER PRIMARY KEY,
                parent INTEGER NOT NULL,
                name TEXT NOT NULL,
                suffix TEXT NOT NULL,
                size INTEGER NOT NULL,
                mtime_ns INTEGER NOT NULL,
                is_dir INTEGER NOT NULL
            )
            """
        )
        connection.execute("DELETE FROM mft_staging")

        batch: list[tuple[int, int, str, str, int, int, int]] = []
        staged = 0
        file_count = 0
        for entry in entry_stream:
            if control:
                control.checkpoint()
            if entry.frn < _META_FRN_LIMIT:
                continue  # $MFT 等元文件：清理工具永远不该看见它们
            batch.append(
                (entry.frn, entry.parent_frn, entry.name, entry.suffix, entry.size_bytes, entry.mtime_ns, int(entry.is_dir))
            )
            if not entry.is_dir:
                file_count += 1
            if len(batch) >= _STAGING_BATCH:
                connection.executemany(
                    "INSERT OR REPLACE INTO mft_staging VALUES (?, ?, ?, ?, ?, ?, ?)", batch
                )
                staged += len(batch)
                batch.clear()
                log(f"MFT 条目已暂存 {staged} 条……")
        if batch:
            connection.executemany(
                "INSERT OR REPLACE INTO mft_staging VALUES (?, ?, ?, ?, ?, ?, ?)", batch
            )
            staged += len(batch)
        connection.commit()
        log(f"MFT 条目暂存完成：{staged} 条，重建路径……")

        # 暂存表必须按 parent 建索引：递归 CTE 每出队一条记录都要按父引用找子节点，
        # 没有索引就是 85 万次全表扫描（≈7×10^11 行访问），重建路径会看似卡死。
        connection.execute("CREATE INDEX IF NOT EXISTS idx_mft_staging_parent ON mft_staging(parent)")

        # 递归 CTE：从根（frn=5）的子节点出发，沿父引用拼出完整路径。
        # parent_path 随递归直接携带（= 父节点的 path），避免事后对几十万行临时表逐行回填。
        connection.execute(
            """
            CREATE TEMP TABLE mft_paths(
                frn INTEGER PRIMARY KEY,
                path TEXT NOT NULL,
                parent_path TEXT NOT NULL
            )
            """
        )
        connection.execute(
            f"""
            INSERT INTO mft_paths(frn, path, parent_path)
            WITH RECURSIVE p(frn, path, parent_path, depth) AS (
                SELECT frn, ? || name, ?, 1 FROM mft_staging WHERE parent = {_ROOT_FRN} AND frn != {_ROOT_FRN}
                UNION ALL
                SELECT s.frn, p.path || '\\' || s.name, p.path, p.depth + 1
                FROM mft_staging s JOIN p ON s.parent = p.frn
                WHERE p.depth < {_MAX_PATH_DEPTH}
            )
            SELECT frn, path, parent_path FROM p
            """,
            (drive_prefix, drive_prefix),
        )

        # OR REPLACE 兜底：正常卷现役记录路径唯一，但损坏卷可能出现同路径的两条记录，
        # 快照语义本来就是"一路径一行"，覆盖比整盘扫描中途失败合理。
        connection.execute(
            """
            INSERT OR REPLACE INTO files(snapshot_id, path, parent, name, suffix, size_bytes, mtime_ns)
            SELECT ?, p.path, p.parent_path, s.name, s.suffix, s.size, s.mtime_ns
            FROM mft_paths p JOIN mft_staging s ON s.frn = p.frn
            WHERE s.is_dir = 0
            """,
            (snapshot_id,),
        )
        connection.execute(
            """
            INSERT OR REPLACE INTO dirs(snapshot_id, path, parent, total_size, file_count, mtime_ns, top_suffixes)
            SELECT ?, p.path, p.parent_path, 0, 0, s.mtime_ns, '[]'
            FROM mft_paths p JOIN mft_staging s ON s.frn = p.frn
            WHERE s.is_dir = 1
            """,
            (snapshot_id,),
        )
        connection.execute("DELETE FROM mft_staging")
        connection.execute("DROP TABLE mft_paths")
        connection.commit()
    except BaseException:
        # 取消或中途失败：回滚后丢弃半成品与空壳快照行——begin_snapshot 已提交，
        # 不清理的话这条 file_count=0 的假快照会成为 latest_snapshot 选中的"最新"记录。
        connection.rollback()
        connection.execute("DELETE FROM mft_staging")
        connection.execute("DROP TABLE IF EXISTS mft_paths")
        connection.execute("DELETE FROM snapshots WHERE snapshot_id = ?", (snapshot_id,))
        connection.commit()
        raise
    finally:
        connection.close()

    # 目录聚合、文件总数与旧快照回收由 Inventory 统一完成（与测试 seed 共用同一实现）。
    inventory.refresh_dir_aggregates(snapshot_id)
    inventory.finish_snapshot(snapshot_id, file_count)
    pruned = inventory.prune_snapshots()
    log(f"快照写入完成：{file_count} 个文件" + (f"，已回收 {pruned} 个旧快照" if pruned else ""))
    return snapshot_id, file_count
