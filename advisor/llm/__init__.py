from advisor.config import Settings
from advisor.llm.availability import llm_available
from advisor.llm.base import LLMProvider


def get_provider(settings: Settings) -> LLMProvider:
    """Build the provider named by LLM_PROVIDER in .env."""
    if settings.provider == "claude":
        from advisor.llm.claude import ClaudeProvider

        return ClaudeProvider(settings)
    if settings.provider == "ollama":
        from advisor.llm.ollama import OllamaProvider

        return OllamaProvider(settings)
    if settings.provider in {"", "offline", "none"}:
        raise ValueError("LLM_PROVIDER is 'offline': no LLM is used.")
    raise ValueError(f"Unknown LLM_PROVIDER '{settings.provider}'. Use 'claude', 'ollama' or 'offline'.")


def get_provider_or_none(settings: Settings) -> tuple[LLMProvider | None, str]:
    """(provider, reason): None when no LLM is configured, so callers fall back to the offline agents."""
    ok, reason = llm_available(settings)
    if not ok:
        return None, reason
    try:
        return get_provider(settings), reason
    except (ValueError, NotImplementedError) as e:
        return None, str(e)
