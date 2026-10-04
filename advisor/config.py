"""Settings loaded from the .env file (or the process environment)."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parent.parent


@dataclass(frozen=True)
class Settings:
    provider: str
    model: str
    api_key: str | None
    effort: str
    llm_data_region: str
    ollama_base_url: str
    currency: str
    live_region: str = "uaenorth"  # Azure region for live list prices
    live_cache_path: str = ""  # empty = advisor/data/live_cache.json


def load_settings(env_file: Path | None = None) -> Settings:
    load_dotenv(env_file or PROJECT_ROOT / ".env", override=False)
    return Settings(
        # "offline" (or no API key) runs the keyless guided interviewer and template writer.
        provider=os.getenv("LLM_PROVIDER", "claude").strip().lower(),
        model=os.getenv("LLM_MODEL", "claude-opus-5-5").strip(),
        # Empty means "let the provider SDK resolve credentials itself".
        api_key=(os.getenv("LLM_API_KEY") or os.getenv("ANTHROPIC_API_KEY") or "").strip() or None,
        effort=os.getenv("LLM_EFFORT", "high").strip().lower(),
        llm_data_region=os.getenv("LLM_DATA_REGION", "US").strip().upper(),
        ollama_base_url=os.getenv("OLLAMA_BASE_URL", "http://localhost:11434").strip(),
        currency=os.getenv("CURRENCY", "USD").strip().upper(),
        live_region=os.getenv("LIVE_PRICE_REGION", "uaenorth").strip().lower() or "uaenorth",
        live_cache_path=os.getenv("LIVE_CACHE_PATH", "").strip(),
    )
