"""安全规则层：全项目的安全底线，不依赖任何网络与 AI。

- 常量区：受保护目录 + 全部后缀集合（唯一定义处，scanner 打分也从这里导入）。
- local_safety_guard：本地规则判定，凡返回 source="local-guard" 的结论 AI 无权推翻。
- is_auto_delete_eligible / can_move_to_trash：自动删除白名单与移入回收站的前置检查。
"""

from __future__ import annotations

import os
from pathlib import Path

from .models import Advice, FileMetadata


PROTECTED_DIR_NAMES = {
    "windows",
    "program files",
    "program files (x86)",
    "programdata",
    "system32",
    "syswow64",
    "drivers",
    "boot",
    "windowsapps",
    "system volume information",
    "$recycle.bin",
}

EXECUTABLE_OR_CONFIG_SUFFIXES = {
    ".sys",
    ".dll",
    ".exe",
    ".msi",
    ".msix",
    ".bat",
    ".cmd",
    ".ps1",
    ".reg",
    ".ini",
    ".db",
    ".sqlite",
    ".dat",
}

USER_CONTENT_SUFFIXES = {
    ".doc",
    ".docx",
    ".xls",
    ".xlsx",
    ".ppt",
    ".pptx",
    ".pdf",
    ".txt",
    ".md",
    ".jpg",
    ".jpeg",
    ".png",
    ".gif",
    ".webp",
    ".mp3",
    ".wav",
    ".mp4",
    ".mov",
    ".mkv",
    ".zip",
    ".rar",
    ".7z",
    ".py",
    ".js",
    ".ts",
    ".java",
    ".cpp",
    ".c",
    ".ipynb",
}


# ── 后缀常量（全项目唯一定义处，scanner.py 等模块从这里导入）─────────────
# 打分信号集合（SIGNAL_*）与安全守卫集合的取值差异是"有意设计"：
# 打分只决定一个文件是否值得作为候选展示；安全守卫决定能否自动进回收站。
# 因此允许打分侧覆盖更多后缀，但自动删除白名单必须保持最保守。
CODE_SUFFIXES = {".py", ".js", ".ts", ".java", ".cpp", ".c", ".ipynb"}
MEDIA_SUFFIXES = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".mp3", ".wav", ".mp4", ".mov", ".mkv"}
# 用户内容里的压缩包子类，仅用于把用途细分为"存档或备份文件"。
USER_ARCHIVE_SUFFIXES = {".zip", ".rar", ".7z"}

# 自动删除白名单：满足"明显垃圾"标准、允许不经 AI 复核直接建议删除的后缀。
# .bak 有意不在此列——它可能是用户手动备份，一律走人工确认。
AUTO_DELETE_JUNK_SUFFIXES = {".tmp", ".temp", ".log", ".dmp", ".old"}
# 扫描打分用的垃圾信号：在白名单基础上放宽（含 .bak），只影响候选排序与展示。
SIGNAL_JUNK_SUFFIXES = AUTO_DELETE_JUNK_SUFFIXES | {".bak"}
# 扫描打分用的安装包/压缩包信号（比用户内容分类覆盖更广，仅影响候选评分）。
INSTALLER_SUFFIXES = {".exe", ".msi", ".msix", ".apk"}
SIGNAL_ARCHIVE_SUFFIXES = {".zip", ".rar", ".7z", ".tar", ".gz", ".iso"}

SAFE_CONTEXT_NAMES = {"temp", "tmp", "cache", "caches", "logs", "log", "crashdumps", "crash"}


def normalized_parts(path: str | Path) -> list[str]:
    text = str(path).replace("\\", "/")
    return [part.casefold() for part in text.split("/") if part]


# ── 路径判定 ──────────────────────────────────────────────────────────────
def is_protected_path(path: str | Path) -> bool:
    parts = set(normalized_parts(path))
    return bool(parts & PROTECTED_DIR_NAMES)


# ── 本地规则判定：从最严到最宽依次匹配，返回 None 表示交给 AI ────────────
def local_safety_guard(metadata: FileMetadata) -> Advice | None:
    path = Path(metadata.path)
    suffix = metadata.suffix.casefold()
    parts = set(normalized_parts(path))

    if is_protected_path(path):
        return Advice(
            recommend_delete=False,
            purpose="系统文件",
            advice_level="不建议删除",
            reason="文件位于系统或程序关键目录，误删可能导致系统或软件异常。",
            source="local-guard",
        )

    if suffix in EXECUTABLE_OR_CONFIG_SUFFIXES:
        return Advice(
            recommend_delete=False,
            purpose="程序配置文件",
            advice_level="人工确认",
            reason="该类型可能影响程序安装、配置或运行，不能自动建议删除。",
            source="local-guard",
        )

    if suffix in USER_CONTENT_SUFFIXES:
        if suffix in CODE_SUFFIXES:
            purpose = "代码或项目文件"
        elif suffix in MEDIA_SUFFIXES:
            purpose = "媒体文件"
        elif suffix in USER_ARCHIVE_SUFFIXES:
            purpose = "存档或备份文件"
        else:
            purpose = "用户文档"
        return Advice(
            recommend_delete=False,
            purpose=purpose,
            advice_level="人工确认",
            reason="该文件可能属于个人资料、媒体、压缩包或项目内容，必须人工确认。",
            source="local-guard",
        )

    if suffix in AUTO_DELETE_JUNK_SUFFIXES and parts & SAFE_CONTEXT_NAMES:
        return Advice(
            recommend_delete=True,
            purpose="临时文件" if suffix in {".tmp", ".temp"} else "日志文件",
            advice_level="建议删除",
            reason="文件位于缓存、日志或临时目录，且后缀符合常见清理对象。",
            source="local-rule",
        )

    if parts & SAFE_CONTEXT_NAMES:
        return Advice(
            recommend_delete=False,
            purpose="缓存文件",
            advice_level="谨慎删除",
            reason="文件位于缓存或临时目录，但仅凭元数据不足以安全自动删除。",
            source="local-rule",
        )

    return None


# ── 删除前置检查 ─────────────────────────────────────────────────────────
def is_auto_delete_eligible(metadata: FileMetadata) -> bool:
    """Only obvious junk in an explicit cache/log/temp context may be auto-selected."""
    parts = set(normalized_parts(metadata.path))
    suffix = metadata.suffix.casefold()
    return bool(parts & SAFE_CONTEXT_NAMES) and suffix in AUTO_DELETE_JUNK_SUFFIXES


def can_move_to_trash(path: Path) -> tuple[bool, str]:
    if not path.exists():
        return False, "文件不存在"
    if path.is_dir():
        return False, "发布版不支持直接删除整个文件夹"
    if path.is_symlink():
        return False, "为避免路径指向风险，不处理符号链接"
    if is_protected_path(path):
        return False, "文件位于受保护目录"
    if os.path.abspath(path) == os.path.abspath(Path.home()):
        return False, "不能处理用户主目录"
    return True, ""
