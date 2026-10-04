"""Decide whether an LLM can be used, without making network calls."""

from __future__ import annotations

import os

from advisor.config import Settings

OFFLINE_PROVIDERS = {"", "offline", "none"}
CLAUDE_ENV_KEYS = ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN")


def llm_available(settings: Settings) -> tuple[bool, str]:
    """(available, reason). Only checks configuration; a key may still be invalid."""
    provider = (settings.provider or "").strip().lower()
    if provider in OFFLINE_PROVIDERS:
        return False, "Offline mode: no LLM provider configured."
    if provider == "claude":
        if (settings.api_key or "").strip():
            return True, "Claude API key configured."
        for name in CLAUDE_ENV_KEYS:
            if os.getenv(name, "").strip():
                return True, f"Claude credentials found in {name}."
        return False, "No Claude API key: set LLM_API_KEY in .env to enable the LLM agents."
    if provider == "ollama":
        return False, "Ollama provider not implemented yet"
    return False, f"Unknown LLM provider '{settings.provider}'."
