from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

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


def normalize_api_style(value: str | None) -> str:
    normalized = (value or "chat_completions").strip().lower()
    try:
        return API_STYLE_ALIASES[normalized]
    except KeyError as exc:
        allowed = "chat_completions 或 responses"
        raise ValueError(f"AI_API_STYLE 只支持 {allowed}，当前值为：{value!r}") from exc


def load_dotenv(path: Path | None = None) -> None:
    """Load a minimal .env file without adding a third-party dependency."""
    env_path = path or Path.cwd() / ".env"
    if not env_path.exists():
        return

    for raw_line in env_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key:
            os.environ.setdefault(key, value)


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
    ai_user_agent: str = "AI-Disk-Assistant/1.2"

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
            ai_user_agent=os.getenv("AI_USER_AGENT", "AI-Disk-Assistant/1.2").strip()
            or "AI-Disk-Assistant/1.2",
        )
