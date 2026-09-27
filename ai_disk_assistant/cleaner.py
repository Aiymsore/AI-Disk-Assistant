"""手动删除执行层：把用户显式勾选并二次确认的目标移入回收站（Windows）。

与 safety.py 的守卫哲学同源：
- 自动流程（扫描 / 评审 / 报告 / 综述）永不触碰文件系统；只有 GUI 收集到的勾选路径
  经 `plan_deletion` 归类展开与守卫后，才会交给 `recycle_paths` 执行。
- 删除一律走回收站（SHFileOperationW + FOF_ALLOWUNDO，可在回收站还原），不做永久删除；
  受保护目录（safety.PROTECTED_DIR_NAMES）中的目标无条件拒绝，快照外的路径不执行。
- 本模块不维护快照库：执行成功后由调用方按磁盘事实回删快照行并刷新目录聚合。
"""

from __future__ import annotations

import ctypes
import os
import shutil
import sys
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path

from .inventory import FileRow, Inventory
from .safety import is_protected_path

# 一次 SHFileOperationW 携带的路径数上限：路径列表以 \0 连接成缓冲区，
# 分块让单次调用远小于系统的缓冲区上限，也便于逐块执行后统一按存在性复核。
_CHUNK_SIZE = 500

_REFUSED_PROTECTED = "位于受保护目录（系统/程序关键目录），不允许删除"
_REFUSED_UNKNOWN = "快照中不存在（可能已被移动、删除或来自旧扫描）"


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


def recycle_paths(
    paths: list[str],
    *,
    executor=None,
    verify=None,
) -> tuple[list[str], list[str]]:
    """分块执行删除并按存在性复核，返回 (deleted, failed)。

    executor(list[str]) 负责实际删除一个分块，默认按平台选择：
    Windows 走回收站（_recycle_bin_windows），其余平台直接删除（仅测试/非 NTFS 场景可达）。
    verify(path) 复核路径是否仍在磁盘上，默认 os.path.exists——删除失败（占用/权限）
    的路径会留在 failed 里，由调用方决定如何提示。
    """
    executor = executor or _default_executor
    verify = verify or os.path.exists
    unique = list(dict.fromkeys(paths))
    for start in range(0, len(unique), _CHUNK_SIZE):
        executor(unique[start : start + _CHUNK_SIZE])
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


def _default_executor(chunk: list[str]) -> None:
    if sys.platform.startswith("win"):
        _recycle_bin_windows(chunk)
        return
    for path in chunk:
        try:
            if os.path.isdir(path):
                shutil.rmtree(path, ignore_errors=True)
            else:
                os.remove(path)
        except OSError:
            pass


def _recycle_bin_windows(paths: list[str]) -> None:
    """SHFileOperationW(FO_DELETE + FOF_ALLOWUNDO)：把一批路径移入回收站。

    故意不设 FOF_NOCONFIRMATION：个别文件过大放不进回收站时系统会弹确认，
    避免被静默永久删除；FOF_SILENT 关闭逐块进度弹窗，FOF_NOERRORUI 抑制
    占用/权限错误弹窗——这些失败统一交给调用方的存在性复核给出最终结果。
    """
    FO_DELETE = 0x0003
    FOF_SILENT = 0x0004
    FOF_ALLOWUNDO = 0x0040
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
            ("lpszProgressTitle", ctypes.c_void_p),
        ]

    joined = "\0".join(paths)
    # 列表以 \0 分隔、双 \0 结尾：create_unicode_buffer 预留的 2 个空位补齐结尾两个 \0。
    buffer = ctypes.create_unicode_buffer(joined, len(joined) + 2)
    operation = SHFILEOPSTRUCTW()
    operation.hwnd = None
    operation.wFunc = FO_DELETE
    operation.pFrom = ctypes.cast(buffer, ctypes.c_void_p)
    operation.fFlags = FOF_SILENT | FOF_ALLOWUNDO | FOF_NOERRORUI
    # 失败/中止不在此时逐项区分：调用方按存在性复核输出 (deleted, failed)。
    ctypes.windll.shell32.SHFileOperationW(ctypes.byref(operation))
