from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Literal

from .models import FileMetadata
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
    privacy_mode = normalize_privacy_mode(mode)
    common: dict[str, Any] = {
        "name": metadata.name,
        "suffix": metadata.suffix,
        "size_bytes": metadata.size_bytes,
        "modified_time": metadata.modified_time,
        "accessed_time": metadata.accessed_time,
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
