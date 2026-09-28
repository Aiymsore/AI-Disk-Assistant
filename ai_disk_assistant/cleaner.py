"""手动删除执行层：把用户显式勾选并二次确认的目标移入回收站（Windows）。

与 safety.py 的守卫哲学同源：
- 自动流程（扫描 / 评审 / 报告 / 综述）永不触碰文件系统；只有 GUI 收集到的勾选路径
  经 `plan_deletion` 归类展开与守卫后，才会交给 `recycle_paths` 执行。
- 删除一律走回收站（SHFileOperationW + FOF_ALLOWUNDO，可在回收站还原），不做永久删除；
  受保护目录（safety.PROTECTED_DIR_NAMES）中的目标无条件拒绝，快照外的路径不执行。
- 系统弹窗由调用方传入的 hwnd 父化并显示进度（此前 hwnd=None + 静默执行，确认框
  可能落在主窗口背后被漏点，表现为"删除卡死"）；会永久删除的目标由 split_permanent
  预先算出并交给调用方在自己的确认框里一次说清。
- 本模块不维护快照库：执行成功后由调用方按磁盘事实回删快照行并刷新目录聚合。
"""

from __future__ import annotations

import ctypes
import os
import shutil
import sys
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from functools import partial
from pathlib import Path

try:  # winreg 仅 Windows 提供；其他平台按"不支持回收站"保守处理。
    import winreg
except ImportError:  # pragma: no cover
    winreg = None  # type: ignore[assignment]

from .inventory import FileRow, Inventory
from .safety import is_protected_path

# 一次 SHFileOperationW 携带的路径数上限：路径列表以 \0 连接成缓冲区，
# 分块让单次调用远小于系统的缓冲区上限，也便于逐块执行后统一按存在性复核。
_CHUNK_SIZE = 500

_REFUSED_PROTECTED = "位于受保护目录（系统/程序关键目录），不允许删除"
_REFUSED_UNKNOWN = "快照中不存在（可能已被移动、删除或来自旧扫描）"

# 回收站容量事实的来源：Windows 按卷管理回收站，注册表
# HKCU/HKLM\...\Explorer\BitBucket\Volume\{卷GUID} 下 MaxCapacity（单位 MB）是该卷上限，
# NukeOnDelete=1 表示该卷不进回收站直接永久删除；缺项时按系统默认（卷容量 5%）估算。
_BITBUCKET_KEY = r"Software\Microsoft\Windows\CurrentVersion\Explorer\BitBucket\Volume"
_DEFAULT_QUOTA_RATIO = 0.05
_MB = 1024 * 1024

if sys.platform.startswith("win"):
    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _kernel32.GetVolumePathNameW.argtypes = (ctypes.c_wchar_p, ctypes.c_wchar_p, ctypes.c_uint)
    _kernel32.GetVolumePathNameW.restype = ctypes.c_bool
    _kernel32.GetVolumeNameForVolumeMountPointW.argtypes = (ctypes.c_wchar_p, ctypes.c_wchar_p, ctypes.c_uint)
    _kernel32.GetVolumeNameForVolumeMountPointW.restype = ctypes.c_bool
    _kernel32.GetDiskFreeSpaceExW.argtypes = (
        ctypes.c_wchar_p,
        ctypes.POINTER(ctypes.c_ulonglong),
        ctypes.POINTER(ctypes.c_ulonglong),
        ctypes.POINTER(ctypes.c_ulonglong),
    )
    _kernel32.GetDiskFreeSpaceExW.restype = ctypes.c_bool
    _user32 = ctypes.WinDLL("user32", use_last_error=True)
    _user32.GetParent.argtypes = (ctypes.c_void_p,)
    _user32.GetParent.restype = ctypes.c_void_p

    class _SHQUERYRBINFO(ctypes.Structure):
        _fields_ = [("i64Size", ctypes.c_longlong), ("i64NumItems", ctypes.c_longlong)]

    _shell32 = ctypes.WinDLL("shell32", use_last_error=True)
    _shell32.SHQueryRecycleBinW.argtypes = (ctypes.c_wchar_p, ctypes.POINTER(_SHQUERYRBINFO))
    _shell32.SHQueryRecycleBinW.restype = ctypes.c_int
else:
    _kernel32 = _user32 = _shell32 = None
    _SHQUERYRBINFO = None


@dataclass(slots=True)
class DeletionPlan:
    """一次手动删除的完整计划：待删文件（目录已展开去重）、被拒目标与确认用统计。"""

    targets: list[FileRow] = field(default_factory=list)
    refused: list[tuple[str, str]] = field(default_factory=list)
    accepted_dir_marks: list[str] = field(default_factory=list)
    accepted_file_marks: list[str] = field(default_factory=list)

    @property
    def total_bytes(self) -> int:
        return sum(row.size_bytes for row in self.targets)


def plan_deletion(inventory: Inventory, snapshot_id: int, marks: Iterable[str]) -> DeletionPlan:
    """把勾选路径展开成待删文件清单：目录子树展开、去重，受保护/未知路径拒绝。

    只依据快照库事实（不触碰文件系统）：目录按 files_under 展开到文件，
    逐一过 is_protected_path 守卫——勾选的目录本身放行、但子树内的受保护文件仍会被拒。
    """
    plan = DeletionPlan()
    unique = list(dict.fromkeys(marks))
    if not unique:
        return plan

    dir_marks, file_marks = inventory.classify_paths(snapshot_id, unique)
    known = dir_marks | file_marks
    for path in unique:
        if path not in known:
            plan.refused.append((path, _REFUSED_UNKNOWN))

    for path in sorted(dir_marks):
        if is_protected_path(path):
            plan.refused.append((path, _REFUSED_PROTECTED))
        else:
            plan.accepted_dir_marks.append(path)
    file_rows: dict[str, FileRow] = {}
    accepted_files: list[str] = []
    for path in sorted(file_marks):
        if is_protected_path(path):
            plan.refused.append((path, _REFUSED_PROTECTED))
        else:
            accepted_files.append(path)
    plan.accepted_file_marks = accepted_files

    for directory in plan.accepted_dir_marks:
        for row in inventory.files_under(snapshot_id, directory):
            if is_protected_path(row.path):
                plan.refused.append((row.path, _REFUSED_PROTECTED))
            else:
                file_rows[row.path] = row
    if accepted_files:
        for row in inventory.file_rows_by_paths(snapshot_id, accepted_files):
            file_rows[row.path] = row

    plan.targets = sorted(file_rows.values(), key=lambda row: row.path)
    return plan


@dataclass(slots=True)
class RecycleInfo:
    """一个卷的回收站判定输入：max_bytes 上限（None=未知）、used_bytes 当前占用。"""

    max_bytes: int | None
    used_bytes: int
    nuke_on_delete: bool


def recycle_info(root: str) -> RecycleInfo:
    """读取卷根（如 ``D:\\``）的回收站容量事实。

    非 Windows、UNC 网络路径、读不到卷信息时保守返回"不支持回收站"（nuke_on_delete=True），
    即这些位置的删除一律按永久删除对待并提前告知用户。
    """
    if _kernel32 is None or not root or root.startswith("\\\\"):
        return RecycleInfo(None, 0, True)

    max_bytes: int | None = None
    nuke = False
    guid = _volume_guid(root)
    if guid:
        max_mb, nuke = _read_bitbucket(guid)
        if max_mb is not None:
            max_bytes = max_mb * _MB
    if max_bytes is None:  # 注册表缺项 → 系统默认的"卷容量 5%"兜底
        total = _volume_total_bytes(root)
        if total:
            max_bytes = int(total * _DEFAULT_QUOTA_RATIO)
    return RecycleInfo(max_bytes, _recycle_usage(root), nuke)


def split_permanent(
    targets: Iterable[FileRow],
    *,
    info_for_root: Callable[[str], RecycleInfo] | None = None,
) -> tuple[list[FileRow], list[FileRow]]:
    """预测哪些目标会被**永久删除**，返回 ``(permanent, recyclable)``。

    Windows 只把"放得进回收站"的文件收进回收站：超出该卷 MaxCapacity、卷被设置为
    NukeOnDelete、或卷不支持回收站（UNC/读不到）的文件，shell 会直接永久删除。
    按执行顺序逐个扣减剩余容量，与 SHFileOperationW 逐项移入的行为一致；
    ``info_for_root`` 便于测试注入，默认读真实注册表与卷信息。
    """
    factory = info_for_root or recycle_info
    headroom: dict[str, int | None] = {}
    permanent: list[FileRow] = []
    recyclable: list[FileRow] = []
    for row in targets:
        root = _volume_root(row.path) or ""
        if root not in headroom:
            info = factory(root)
            headroom[root] = (
                None
                if info.nuke_on_delete or info.max_bytes is None
                else max(0, info.max_bytes - info.used_bytes)
            )
        remaining = headroom[root]
        if remaining is None or row.size_bytes > remaining:
            permanent.append(row)
        else:
            recyclable.append(row)
            headroom[root] = remaining - row.size_bytes
    return permanent, recyclable


def _volume_root(path: str) -> str | None:
    """path 所在的卷根（``D:\\``；UNC 为 ``\\\\server\\share\\``），拿不到返回 None。"""
    if _kernel32 is None:
        return None
    buffer = ctypes.create_unicode_buffer(512)
    if not _kernel32.GetVolumePathNameW(path, buffer, 512):
        return None
    return buffer.value


def _volume_guid(root: str) -> str | None:
    """卷根 → 注册表键名格式的 ``{GUID}``（``\\\\?\\Volume{...}\\`` → ``{...}``）。"""
    if _kernel32 is None:
        return None
    buffer = ctypes.create_unicode_buffer(512)
    if not _kernel32.GetVolumeNameForVolumeMountPointW(root, buffer, 512):
        return None
    name = buffer.value
    prefix = "\\\\?\\Volume"
    if name.startswith(prefix) and name.endswith("\\"):
        return name[len(prefix) : -1]
    return None


def _volume_total_bytes(root: str) -> int | None:
    """卷总容量（字节）；拿不到返回 None。"""
    if _kernel32 is None:
        return None
    free = ctypes.c_ulonglong()
    total = ctypes.c_ulonglong()
    free_total = ctypes.c_ulonglong()
    if not _kernel32.GetDiskFreeSpaceExW(root, ctypes.byref(free), ctypes.byref(total), ctypes.byref(free_total)):
        return None
    return int(total.value)


def _read_bitbucket(guid: str) -> tuple[int | None, bool]:
    """读该卷的回收站配置 ``(MaxCapacity 单位 MB, NukeOnDelete)``；键不存在返回 ``(None, False)``。"""
    if winreg is None:
        return None, False
    for hive in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
        try:
            with winreg.OpenKey(hive, f"{_BITBUCKET_KEY}\\{guid}") as key:
                try:
                    max_mb = int(winreg.QueryValueEx(key, "MaxCapacity")[0])
                except (OSError, ValueError, TypeError):
                    max_mb = None
                try:
                    nuke = bool(int(winreg.QueryValueEx(key, "NukeOnDelete")[0]))
                except (OSError, ValueError, TypeError):
                    nuke = False
                return max_mb, nuke
        except OSError:
            continue
    return None, False


def _recycle_usage(root: str) -> int:
    """该卷回收站当前占用字节数；查询失败按 0（预测偏保守侧，可接受）。"""
    if _shell32 is None:
        return 0
    info = _SHQUERYRBINFO()
    if _shell32.SHQueryRecycleBinW(root, ctypes.byref(info)) != 0:
        return 0
    return int(info.i64Size)


def recycle_paths(
    paths: list[str],
    *,
    executor=None,
    verify=None,
    hwnd: int | None = None,
    suppress_confirm: bool = False,
    progress: Callable[[int, int], None] | None = None,
) -> tuple[list[str], list[str]]:
    """分块执行删除并按存在性复核，返回 (deleted, failed)。

    executor(list[str]) 负责实际删除一个分块，默认按平台选择：
    Windows 走回收站（_recycle_bin_windows），其余平台直接删除（仅测试/非 NTFS 场景可达）。
    verify(path) 复核路径是否仍在磁盘上，默认 os.path.exists——删除失败（占用/权限）
    的路径会留在 failed 里，由调用方决定如何提示。

    hwnd：shell 弹窗（确认框/进度框）的父窗口句柄，GUI 传 Tk 顶层窗口句柄，
    弹窗才会盖在主窗口上而不是落到背后被漏点。
    suppress_confirm：True 时不弹系统确认——调用方必须已用自己的确认框告知过
    "超容量目标将被永久删除"（split_permanent 的结果）；False 保留系统确认作最后防线。
    progress(done, total)：每块执行后回调累计完成数与总数，供状态栏刷新。
    """
    executor = executor or partial(_default_executor, hwnd=hwnd, suppress_confirm=suppress_confirm)
    verify = verify or os.path.exists
    unique = list(dict.fromkeys(paths))
    total = len(unique)
    for start in range(0, total, _CHUNK_SIZE):
        executor(unique[start : start + _CHUNK_SIZE])
        if progress is not None:
            progress(min(start + _CHUNK_SIZE, total), total)
    deleted = [path for path in unique if not verify(path)]
    failed = [path for path in unique if verify(path)]
    return deleted, failed


def prune_empty_dirs(directory: str) -> list[str]:
    """自底向上删除 directory 子树内的空目录，返回被移除的目录路径。

    目录勾选删除后，其下文件若全部成功移入回收站，会残留空目录壳；这里把真正
    变空的目录清掉（含 directory 自身）。任何仍含内容的目录保持原样——
    部分删除（有文件被拒或删除失败）时，残余内容因此自然受保护。
    """
    removed: list[str] = []
    root = Path(directory)
    if not root.is_dir():
        return removed
    for current, _dirnames, _filenames in os.walk(root, topdown=False):
        try:
            # listdir 现场确认而不是信任 walk 快照：子目录可能刚在本次清理中被移除。
            if not os.listdir(current):
                os.rmdir(current)
                removed.append(str(current))
        except OSError:
            continue
    return removed


def _default_executor(chunk: list[str], *, hwnd: int | None = None, suppress_confirm: bool = False) -> None:
    if sys.platform.startswith("win"):
        _recycle_bin_windows(chunk, hwnd=hwnd, suppress_confirm=suppress_confirm)
        return
    for path in chunk:
        try:
            if os.path.isdir(path):
                shutil.rmtree(path, ignore_errors=True)
            else:
                os.remove(path)
        except OSError:
            pass


def _recycle_bin_windows(paths: list[str], *, hwnd: int | None = None, suppress_confirm: bool = False) -> None:
    """SHFileOperationW(FO_DELETE + FOF_ALLOWUNDO)：把一批路径移入回收站。

    - hwnd 父化弹窗：确认框（如"文件太大无法放入回收站，是否永久删除"）与进度框都挂在
      调用方窗口上。此前 hwnd=None + FOF_SILENT + 后台线程阻塞，确认框可能落在主窗口
      背后没人点，表现为"删除卡死半小时"。
    - FOF_SIMPLEPROGRESS 显示系统进度框，长任务不再毫无反馈（替换原 FOF_SILENT）。
    - FOF_NOCONFIRMATION 仅在调用方已用 split_permanent 预告过永久删除时设置；
      默认保留系统确认，避免文件被静默永久删除。
    - FOF_NOERRORUI 抑制占用/权限错误弹窗——这些失败统一交给调用方的存在性复核输出。
    """
    FO_DELETE = 0x0003
    FOF_NOCONFIRMATION = 0x0010
    FOF_ALLOWUNDO = 0x0040
    FOF_SIMPLEPROGRESS = 0x0100
    FOF_NOERRORUI = 0x0400

    class SHFILEOPSTRUCTW(ctypes.Structure):
        _fields_ = [
            ("hwnd", ctypes.c_void_p),
            ("wFunc", ctypes.c_uint),
            ("pFrom", ctypes.c_void_p),
            ("pTo", ctypes.c_void_p),
            ("fFlags", ctypes.c_ushort),
            ("fAnyOperationsAborted", ctypes.c_int),
            ("hNameMappings", ctypes.c_void_p),
            ("lpszProgressTitle", ctypes.c_wchar_p),
        ]

    joined = "\0".join(paths)
    # 列表以 \0 分隔、双 \0 结尾：create_unicode_buffer 预留的 2 个空位补齐结尾两个 \0。
    buffer = ctypes.create_unicode_buffer(joined, len(joined) + 2)
    title = ctypes.create_unicode_buffer("正在移入回收站")
    operation = SHFILEOPSTRUCTW()
    operation.hwnd = _top_level_hwnd(hwnd)
    operation.wFunc = FO_DELETE
    operation.pFrom = ctypes.cast(buffer, ctypes.c_void_p)
    operation.lpszProgressTitle = title
    operation.fFlags = FOF_SIMPLEPROGRESS | FOF_ALLOWUNDO | FOF_NOERRORUI
    if suppress_confirm:
        operation.fFlags |= FOF_NOCONFIRMATION
    # 失败/中止不在此时逐项区分：调用方按存在性复核输出 (deleted, failed)。
    ctypes.windll.shell32.SHFileOperationW(ctypes.byref(operation))


def _top_level_hwnd(hwnd: int | None) -> int | None:
    """Tk 的 winfo_id 返回客户区窗口，shell 弹窗需要顶层窗口句柄；取其父，取不到则原样。"""
    if not hwnd or _user32 is None:
        return None
    parent = _user32.GetParent(int(hwnd))
    return int(parent) if parent else int(hwnd)
