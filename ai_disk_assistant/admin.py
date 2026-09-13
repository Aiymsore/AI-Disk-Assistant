"""管理员权限层：检测当前进程是否具有管理员权限，并以 UAC 提权重启自身。

扫描管线的硬性前置条件是"管理员 + 已配置 AI"——MFT 直读必须管理员，
AI 判读必须已配置密钥；两者缺一，入口层（CLI/GUI）应拒绝执行并给出引导。
"""

from __future__ import annotations

import sys
from pathlib import Path


def is_user_admin() -> bool:
    """当前进程是否以管理员身份运行；非 Windows 平台恒为 False。"""
    try:
        import ctypes

        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except (AttributeError, OSError):
        return False


def relaunch_as_admin(target_file: Path) -> bool:
    """弹出 UAC 提权框，以管理员身份重新启动 target_file（通常是 gui.py 入口）。

    成功弹出时返回 True（调用方应尽快退出当前进程）；用户取消 UAC 或失败返回 False。
    """
    try:
        import ctypes

        project_dir = str(target_file.resolve().parent)
        params = f'"{target_file.resolve()}"'
        result = ctypes.windll.shell32.ShellExecuteW(
            None, "runas", sys.executable, params, project_dir, 1  # SW_SHOWNORMAL
        )
        return int(result) > 32
    except (AttributeError, OSError):
        return False


def analysis_blockers(ai_available: bool) -> list[str]:
    """返回当前环境下阻止执行分析的原因清单；空列表表示可以执行。"""
    blockers: list[str] = []
    if not is_user_admin():
        blockers.append("未以管理员身份运行（MFT 直读需要管理员权限）")
    if not ai_available:
        blockers.append("未配置 AI（点击\"AI 配置\"填写密钥与模型）")
    return blockers
