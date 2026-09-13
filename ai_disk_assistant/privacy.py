"""隐私保护层：决定发送给 AI 的字段范围（strict/balanced/full 三档）。

被 config.py（Settings 校验）与 ai_advisor.py（请求前裁剪）使用；
safety.py 提供路径分词工具，本层不做任何安全判定。
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Literal

from .models import FileMetadata, Unit
from .safety import SAFE_CONTEXT_NAMES, normalized_parts

PrivacyMode = Literal["strict", "balanced", "full"]
VALID_PRIVACY_MODES = {"strict", "balanced", "full"}


def normalize_privacy_mode(mode: str) -> PrivacyMode:
    normalized = mode.strip().casefold()
    if normalized not in VALID_PRIVACY_MODES:
        raise ValueError(f"不支持的隐私模式：{mode}")
    return normalized  # type: ignore[return-value]


def _replace_case_insensitive(text: str, old: str, new: str) -> str:
    if not old:
        return text
    lower_text = text.casefold()
    lower_old = old.casefold()
    index = lower_text.find(lower_old)
    if index == -1:
        return text
    return text[:index] + new + text[index + len(old) :]


def anonymize_path(path: str) -> str:
    """Remove common user-identifying prefixes while preserving useful context."""
    result = path
    home = str(Path.home())
    result = _replace_case_insensitive(result, home, "%USERPROFILE%")

    username = os.getenv("USERNAME") or os.getenv("USER")
    if username:
        result = result.replace(f"\\Users\\{username}", r"\Users\<USER>")
        result = result.replace(f"/home/{username}", "/home/<USER>")

    # Covers Windows paths processed on non-Windows CI runners.
    parts = result.replace("\\", "/").split("/")
    for index, part in enumerate(parts[:-1]):
        if part.casefold() == "users" and index + 1 < len(parts):
            parts[index + 1] = "<USER>"
            break
    separator = "\\" if "\\" in result else "/"
    return separator.join(parts)


def metadata_for_ai(metadata: FileMetadata, mode: str = "balanced") -> dict[str, Any]:
    """逐文件 AI 载荷。有意不含修改/访问时间：mtime 在 Windows 下不可靠，
    不能作为判断依据；时间只随报告导出供人工参考。"""
    privacy_mode = normalize_privacy_mode(mode)
    common: dict[str, Any] = {
        "name": metadata.name,
        "suffix": metadata.suffix,
        "size_bytes": metadata.size_bytes,
    }

    if privacy_mode == "strict":
        parts = set(normalized_parts(metadata.path))
        common["directory_context"] = sorted(parts & SAFE_CONTEXT_NAMES)
        common["path_depth"] = len(normalized_parts(metadata.path))
        return common

    if privacy_mode == "balanced":
        common["path"] = anonymize_path(metadata.path)
        common["parent_folder"] = anonymize_path(metadata.parent_folder)
        return common

    common["path"] = metadata.path
    common["parent_folder"] = metadata.parent_folder
    return common


def unit_payload_for_ai(unit: Unit, mode: str = "balanced") -> dict[str, Any]:
    """判定单元的 AI 载荷：统计字段对所有隐私档位可见，目录模式按档位裁剪。

    有意不含年龄字段：mtime 在 Windows 下不可靠，不作为判断依据。
    evidence 校验依赖"AI 原样引用输入字段"，因此这里的键名与取值即校验白名单。
    """
    privacy_mode = normalize_privacy_mode(mode)
    payload: dict[str, Any] = {
        "suffix": unit.suffix,
        "file_count": unit.file_count,
        "total_size_bytes": unit.total_size,
        "sample_names": [member.name for member in unit.members[:3]],
    }

    if privacy_mode == "strict":
        payload["directory_context"] = sorted(set(normalized_parts(unit.parent_folder)) & SAFE_CONTEXT_NAMES)
        return payload

    if privacy_mode == "balanced":
        payload["path_pattern"] = anonymize_path(unit.parent_folder)
        return payload

    payload["path_pattern"] = unit.parent_folder
    return payload
