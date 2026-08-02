from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping

from .privacy import normalize_privacy_mode


API_STYLE_ALIASES = {
    "chat": "chat_completions",
    "chat_completion": "chat_completions",
    "chat_completions": "chat_completions",
    "chat-completions": "chat_completions",
    "completions": "chat_completions",
    "response": "responses",
    "responses": "responses",
}

AI_ENV_KEYS = (
    "AI_API_KEY",
    "AI_BASE_URL",
    "AI_MODEL",
    "AI_API_STYLE",
    "AI_TIMEOUT",
    "AI_BATCH_SIZE",
    "AI_MAX_RETRIES",
    "AI_RETRY_BACKOFF",
    "AI_CACHE_PATH",
    "AI_PRIVACY_MODE",
    "AI_USER_AGENT",
)


def normalize_api_style(value: str | None) -> str:
    normalized = (value or "chat_completions").strip().lower()
    try:
        return API_STYLE_ALIASES[normalized]
    except KeyError as exc:
        allowed = "chat_completions 或 responses"
        raise ValueError(f"AI_API_STYLE 只支持 {allowed}，当前值为：{value!r}") from exc


def application_dir() -> Path:
    """Return the folder used for runtime configuration.

    Source runs use the current project folder. A PyInstaller executable stores
    ``.env`` next to the executable so users can move the release folder as a unit.
    """
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path.cwd()


def default_env_path() -> Path:
    return application_dir() / ".env"


def read_dotenv(path: Path | None = None) -> dict[str, str]:
    """Read a small ``.env`` file without importing a third-party package."""
    env_path = path or default_env_path()
    if not env_path.exists():
        return {}

    values: dict[str, str] = {}
    for raw_line in env_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key:
            values[key] = value
    return values


def load_dotenv(path: Path | None = None, *, override: bool = False) -> None:
    """Load values from ``.env`` into the current process.

    ``override=True`` is used after the GUI saves new settings, so the next AI
    test or scan uses the new values without restarting the application.
    """
    for key, value in read_dotenv(path).items():
        if override or key not in os.environ:
            os.environ[key] = value


def update_dotenv(values: Mapping[str, str], path: Path | None = None) -> Path:
    """Update selected keys while preserving comments and unrelated settings."""
    env_path = path or default_env_path()
    normalized: dict[str, str] = {}
    for key, raw_value in values.items():
        if key not in AI_ENV_KEYS:
            raise ValueError(f"不允许写入未知配置项：{key}")
        value = str(raw_value).strip()
        if "\n" in value or "\r" in value:
            raise ValueError(f"配置项 {key} 不能包含换行符")
        normalized[key] = value

    existing_lines = (
        env_path.read_text(encoding="utf-8").splitlines() if env_path.exists() else []
    )
    output: list[str] = []
    written: set[str] = set()

    for raw_line in existing_lines:
        stripped = raw_line.strip()
        if stripped and not stripped.startswith("#") and "=" in stripped:
            key = stripped.split("=", 1)[0].strip()
            if key in normalized:
                output.append(f"{key}={normalized[key]}")
                written.add(key)
                continue
        output.append(raw_line)

    if output and output[-1].strip():
        output.append("")
    for key in AI_ENV_KEYS:
        if key in normalized and key not in written:
            output.append(f"{key}={normalized[key]}")

    env_path.parent.mkdir(parents=True, exist_ok=True)
    env_path.write_text("\n".join(output).rstrip() + "\n", encoding="utf-8")

    # Keep the already-running GUI in sync with the newly saved file.
    for key, value in normalized.items():
        os.environ[key] = value
    return env_path


def _env_int(name: str, default: int, minimum: int = 0) -> int:
    try:
        return max(int(os.getenv(name, str(default))), minimum)
    except ValueError:
        return default


def _env_float(name: str, default: float, minimum: float = 0.0) -> float:
    try:
        return max(float(os.getenv(name, str(default))), minimum)
    except ValueError:
        return default


@dataclass(frozen=True, slots=True)
class Settings:
    ai_api_key: str | None
    ai_base_url: str
    ai_model: str
    ai_timeout: float
    ai_batch_size: int = 12
    ai_max_retries: int = 3
    ai_retry_backoff: float = 1.0
    ai_cache_path: str = ".cache/ai_advice.sqlite3"
    ai_privacy_mode: str = "balanced"
    ai_api_style: str = "chat_completions"
    ai_user_agent: str = "AI-Disk-Assistant/1.3"

    @classmethod
    def from_env(cls) -> "Settings":
        load_dotenv()
        return cls(
            ai_api_key=os.getenv("AI_API_KEY") or os.getenv("OPENAI_API_KEY"),
            ai_base_url=os.getenv("AI_BASE_URL", "https://api.openai.com/v1").rstrip("/"),
            ai_model=os.getenv("AI_MODEL", ""),
            ai_timeout=_env_float("AI_TIMEOUT", 30.0, 1.0),
            ai_api_style=normalize_api_style(os.getenv("AI_API_STYLE", "chat_completions")),
            ai_batch_size=_env_int("AI_BATCH_SIZE", 12, 1),
            ai_max_retries=_env_int("AI_MAX_RETRIES", 3, 0),
            ai_retry_backoff=_env_float("AI_RETRY_BACKOFF", 1.0, 0.0),
            ai_cache_path=os.getenv("AI_CACHE_PATH", ".cache/ai_advice.sqlite3"),
            ai_privacy_mode=normalize_privacy_mode(os.getenv("AI_PRIVACY_MODE", "balanced")),
            ai_user_agent=os.getenv("AI_USER_AGENT", "AI-Disk-Assistant/1.3").strip()
            or "AI-Disk-Assistant/1.3",
        )
