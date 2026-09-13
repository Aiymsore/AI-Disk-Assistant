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


CODE_SUFFIXES = {".py", ".js", ".ts", ".java", ".cpp", ".c", ".ipynb"}
MEDIA_SUFFIXES = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".mp3", ".wav", ".mp4", ".mov", ".mkv"}
ARCHIVE_SUFFIXES = {".zip", ".rar", ".7z"}

SAFE_CONTEXT_NAMES = {"temp", "tmp", "cache", "caches", "logs", "log", "crashdumps", "crash"}
JUNK_SUFFIXES = {".tmp", ".temp", ".log", ".dmp", ".old"}


def normalized_parts(path: str | Path) -> list[str]:
    text = str(path).replace("\\", "/")
    return [part.casefold() for part in text.split("/") if part]


def is_protected_path(path: str | Path) -> bool:
    parts = set(normalized_parts(path))
    return bool(parts & PROTECTED_DIR_NAMES)


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
        elif suffix in ARCHIVE_SUFFIXES:
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

    if suffix in JUNK_SUFFIXES and parts & SAFE_CONTEXT_NAMES:
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


def is_auto_delete_eligible(metadata: FileMetadata) -> bool:
    """Only obvious junk in an explicit cache/log/temp context may be auto-selected."""
    parts = set(normalized_parts(metadata.path))
    suffix = metadata.suffix.casefold()
    return bool(parts & SAFE_CONTEXT_NAMES) and suffix in JUNK_SUFFIXES


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
