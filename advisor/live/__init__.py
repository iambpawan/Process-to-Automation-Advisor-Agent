"""Keyless real-time public data (FX rates, Azure retail prices) with caching and offline fallback.

Every fetcher takes a `fetch(url) -> parsed JSON` callable so tests never touch the network.
"""
import json
import urllib.error
import urllib.request
from collections.abc import Callable
from typing import Any

USER_AGENT = "ProcessToAutomationAdvisor/1.0 (+keyless public pricing lookup)"
DEFAULT_TIMEOUT = 10.0

Fetch = Callable[[str], Any]


class LiveDataError(Exception):
    """A public data source could not be reached or returned something unexpected."""


def http_get_json(url: str, timeout: float = DEFAULT_TIMEOUT) -> Any:
    """GET `url` and parse JSON. Every failure is raised as LiveDataError."""
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, TimeoutError, OSError, ValueError) as e:
        raise LiveDataError(f"{url.split('?')[0]}: {e}") from e


# Submodules import the names above, so they are imported last.
from .azure_prices import fetch_azure_document_prices, fetch_azure_openai_prices  # noqa: E402
from .fx import fetch_fx_to_usd  # noqa: E402
from .refresh import DEFAULT_CACHE_PATH, refresh_pricing  # noqa: E402

__all__ = [
    "DEFAULT_CACHE_PATH",
    "Fetch",
    "LiveDataError",
    "fetch_azure_document_prices",
    "fetch_azure_openai_prices",
    "fetch_fx_to_usd",
    "http_get_json",
    "refresh_pricing",
]
