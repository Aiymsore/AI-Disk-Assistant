from __future__ import annotations

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from ai_disk_assistant.ai_advisor import HybridAdvisor
from ai_disk_assistant.config import Settings


def main() -> None:
    settings = Settings.from_env()
    print(f"Base URL: {settings.ai_base_url}")
    print(f"API style: {settings.ai_api_style}")
    print(f"Model: {settings.ai_model}")
    print(f"API key loaded: {bool(settings.ai_api_key)}")

    advisor = HybridAdvisor(settings, enable_ai=True)
    advice = advisor.probe()
    print("Connection: OK")
    print(f"Structured result: {advice.purpose} / {advice.advice_level}")
    print(f"Reason: {advice.reason}")
    print(f"Requests: {advisor.stats.api_calls}")


if __name__ == "__main__":
    main()
