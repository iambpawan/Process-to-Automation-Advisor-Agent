"""Overlay live public prices onto the static pricing assumptions, with a JSON cache and offline fallback."""
import copy
import json
import math
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from . import Fetch, http_get_json
from .azure_prices import SOURCE_LABEL as AZURE_SOURCE
from .azure_prices import fetch_azure_document_prices, fetch_azure_openai_prices
from .fx import fetch_fx_with_source

DEFAULT_CACHE_PATH = Path(__file__).resolve().parent.parent / "data" / "live_cache.json"
FX_CURRENCIES = {"USD", "AED", "EUR", "GBP", "SAR", "INR"}
CACHE_VERSION = 1
CURRENCY_CODE = re.compile(r"[A-Z]{3}")

STATIC_NOTES = [
    "Anthropic (Claude) token prices stay static assumptions: Anthropic publishes no public pricing API.",
    "Power Automate licence and AI Builder credit prices stay static assumptions: Microsoft publishes no public API for them.",
]


def _parse_time(value: Any) -> datetime | None:
    try:
        t = datetime.fromisoformat(str(value))
    except (ValueError, TypeError):
        return None
    return t if t.tzinfo else t.replace(tzinfo=timezone.utc)


def _age_text(then: datetime, now: datetime) -> str:
    hours = max(0.0, (now - then).total_seconds() / 3600)
    return f"{hours * 60:.0f} min" if hours < 1 else f"{hours:.0f} h" if hours < 48 else f"{hours / 24:.0f} days"


def load_cache(path: Path) -> dict[str, Any]:
    """Cache contents, or an empty cache if missing, unreadable or from another version."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, RecursionError):  # RecursionError: absurdly nested JSON
        return {"version": CACHE_VERSION}
    if not isinstance(data, dict) or data.get("version") != CACHE_VERSION:
        return {"version": CACHE_VERSION}
    return data


def save_cache(path: Path, cache: dict[str, Any]) -> str | None:
    """Write the cache atomically; returns an error note instead of raising."""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(json.dumps(cache, indent=2, sort_keys=True), encoding="utf-8")
        tmp.replace(path)
    except OSError as e:
        return f"Could not write the live price cache ({e}); prices will be fetched again next time."
    return None


def _positive(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) and value > 0


def _clean_fx(entry: dict) -> dict | None:
    """Keep only valid currency codes with finite positive rates; None if nothing usable is left."""
    rates = entry.get("rates")
    if not isinstance(rates, dict):
        return None
    good = {c: float(r) for c, r in rates.items() if isinstance(c, str) and CURRENCY_CODE.fullmatch(c) and _positive(r)}
    if not good:
        return None
    source = entry.get("source")
    return {**entry, "rates": good, "source": source if isinstance(source, str) else "cached FX source"}


MAX_FX_DEVIATION = 3.0  # a live rate more than 3x away from the static table is treated as bad data
FX_ABS_RANGE = (1e-6, 1e6)  # USD per unit; bounds for currencies the static table does not know


def _plausible_rate(code: str, rate: Any, static_fx: dict[str, Any]) -> bool:
    """True for a finite positive rate near the static value (or inside absolute bounds if unknown)."""
    if not _positive(rate):
        return False
    ref = static_fx.get(code)
    if _positive(ref):
        return ref / MAX_FX_DEVIATION <= rate <= ref * MAX_FX_DEVIATION
    return FX_ABS_RANGE[0] <= rate <= FX_ABS_RANGE[1]


def _clean_azure(entry: dict) -> dict | None:
    """Keep only model prices with positive numbers and a dict of document prices; None if no models remain."""
    models = entry.get("models")
    if not isinstance(models, dict):
        return None
    good = {
        n: {"input": float(v["input"]), "output": float(v["output"])}
        for n, v in models.items()
        if isinstance(n, str) and isinstance(v, dict) and _positive(v.get("input")) and _positive(v.get("output"))
    }
    if not good:
        return None
    docs = entry.get("document_intelligence")
    docs = {k: float(v) for k, v in docs.items() if isinstance(k, str) and _positive(v)} if isinstance(docs, dict) else {}
    source = entry.get("source")
    return {**entry, "models": good, "document_intelligence": docs, "source": source if isinstance(source, str) else AZURE_SOURCE}


def _section(cache_entry: Any, key: str, now: datetime, max_age: timedelta, clean=None) -> tuple[dict | None, bool]:
    """(entry, is_fresh) for a cached section; (None, False) if absent or malformed."""
    if not isinstance(cache_entry, dict) or not isinstance(cache_entry.get(key), dict) or not cache_entry[key]:
        return None, False
    t = _parse_time(cache_entry.get("fetched_at"))
    if t is None:
        return None, False
    if clean is not None:
        cache_entry = clean(cache_entry)
        if cache_entry is None:
            return None, False
    return cache_entry, (now - t) <= max_age and t <= now + timedelta(minutes=5)


def _get_section(key: str, entry: Any, fetcher, now: datetime, max_age: timedelta,
                 allow_network: bool, clean=None, force: bool = False) -> tuple[dict | None, str, str | None]:
    """Resolve one data section: fresh cache, live fetch, stale cache, or nothing.

    Returns (entry, status, error) where status is 'live' | 'cache' | 'stale' | 'static'.
    """
    cached, fresh = _section(entry, key, now, max_age, clean)
    if cached and fresh and not force:
        return cached, "cache", None
    error = None
    if allow_network:
        try:
            return {**fetcher(), "fetched_at": now.isoformat(timespec="seconds")}, "live", None
        except Exception as e:  # network/shape errors must never break the app
            error = f"live lookup failed: {type(e).__name__}: {e}"
    else:
        error = "network lookups are turned off"
    if cached:
        return cached, "stale", error
    return None, "static", error


def refresh_pricing(pricing: dict[str, Any], *, region: str = "uaenorth", cache_path: Path = DEFAULT_CACHE_PATH,
                    max_age_hours: float = 24, fetch: Fetch | None = None, now: datetime | None = None,
                    allow_network: bool = True, force: bool = False) -> tuple[dict[str, Any], list[str]]:
    """Return (new pricing dict with live data overlaid, human-readable status notes). Never raises for network errors.

    - fx_to_usd: every valid live rate (currencies not in the static table are added).
    - ai.models_per_mtok: extended with "azure-openai-<model> (<region>|global)" entries.
    - pricing["_live"]: {"fetched_at", "sources", "status": "live"|"cache"|"static", ...details}.
    Uses a cache younger than `max_age_hours` (unless `force`, which always fetches, as the Refresh button does); on failure falls back to a stale cache, then to static values.
    """
    fetch = fetch or http_get_json
    now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    max_age = timedelta(hours=max_age_hours)
    region = (region or "uaenorth").strip().lower()
    cache = load_cache(cache_path)
    notes: list[str] = []
    p = copy.deepcopy(pricing)

    def fx_fetcher() -> dict[str, Any]:
        rates, source = fetch_fx_with_source(fetch)
        return {"rates": rates, "source": source}

    def azure_fetcher() -> dict[str, Any]:
        out: dict[str, Any] = {"models": fetch_azure_openai_prices(region, fetch), "source": AZURE_SOURCE}
        try:
            out["document_intelligence"] = fetch_azure_document_prices(region, fetch)
        except Exception:  # optional extra; the model prices are what matter
            out["document_intelligence"] = {}
        return out

    fx_entry, fx_status, fx_err = _get_section(
        "rates", cache.get("fx"), fx_fetcher, now, max_age, allow_network, _clean_fx, force)
    azure_cache = cache.get("azure") if isinstance(cache.get("azure"), dict) else {}
    az_entry, az_status, az_err = _get_section(
        "models", azure_cache.get(region), azure_fetcher, now, max_age, allow_network, _clean_azure, force)

    if fx_status == "live" or az_status == "live":
        if fx_status == "live":
            cache["fx"] = fx_entry
        if az_status == "live":
            if not isinstance(cache.get("azure"), dict):
                cache["azure"] = {}
            cache["azure"][region] = az_entry
        if err := save_cache(cache_path, cache):
            notes.append(err)

    sources: list[str] = []
    used_times: list[datetime] = []

    # FX overlay
    if fx_entry:
        fx = p.setdefault("fx_to_usd", {})
        wanted = set(fx) | FX_CURRENCIES | set(fx_entry["rates"])
        applied, rejected = [], []
        static_fx = dict(fx)
        for c in sorted(wanted):
            rate = fx_entry["rates"].get(c)
            if rate is None or c == "USD":
                continue
            if _plausible_rate(c, rate, static_fx):
                fx[c] = float(rate)
                applied.append(c)
            else:
                rejected.append(c)  # keeps the static rate
        fx["USD"] = 1.0
        sources.append(str(fx_entry.get("source", "FX source")))
        used_times.append(_parse_time(fx_entry["fetched_at"]))
        notes.append(_fx_note(fx_status, fx_entry, applied, now, fx_err))
        if rejected:
            notes.append("Exchange rates ignored as implausible (kept static values): " + ", ".join(rejected) + ".")
    else:
        notes.append(f"Exchange rates: static assumptions ({fx_err}).")

    # Azure OpenAI overlay (stored in the pricing currency, as the static models are)
    doc_prices: dict[str, float] = {}
    if az_entry:
        models = p.setdefault("ai", {}).setdefault("models_per_mtok", {})
        fx = p.get("fx_to_usd", {})
        currency = p.get("currency", "USD")
        factor = 1.0 / fx[currency] if currency in fx and fx[currency] else None
        if factor is None:
            notes.append(f"Azure OpenAI prices skipped: no FX rate for pricing currency '{currency}'.")
        else:
            for name, price in az_entry["models"].items():
                try:
                    models[name] = {"input": float(price["input"]) * factor, "output": float(price["output"]) * factor}
                except (KeyError, TypeError, ValueError):
                    continue  # malformed cache entry
            doc_prices = dict(az_entry.get("document_intelligence") or {})
            sources.append(str(az_entry.get("source", AZURE_SOURCE)))
            used_times.append(_parse_time(az_entry["fetched_at"]))
            notes.append(_azure_note(az_status, az_entry, region, now, az_err))
    else:
        notes.append(f"Azure OpenAI prices ({region}): not available ({az_err}).")

    notes.extend(STATIC_NOTES)

    statuses = {fx_status, az_status}
    status = "live" if "live" in statuses else "cache" if statuses & {"cache", "stale"} else "static"
    p["_live"] = {
        "status": status,
        "fetched_at": min(used_times).isoformat(timespec="seconds") if used_times else None,
        "sources": sources,
        "region": region,
        "fx_status": fx_status,
        "azure_status": az_status,
        "azure_openai_models": sorted(az_entry["models"]) if az_entry else [],
        "document_intelligence_usd_per_1k_pages": doc_prices,
        "static": ["Anthropic token prices", "Power Automate licences", "AI Builder credits", "labour and effort"],
    }
    return p, notes


def _fx_note(status: str, entry: dict, applied: list[str], now: datetime, err: str | None) -> str:
    when = _age_text(_parse_time(entry["fetched_at"]), now)
    what = ", ".join(applied)
    if status == "live":
        return f"Exchange rates: live from {entry['source']} ({what})."
    if status == "cache":
        return f"Exchange rates: cached from {entry['source']}, fetched {when} ago ({what})."
    return f"Exchange rates: using a stale cache fetched {when} ago ({err})."


def _azure_note(status: str, entry: dict, region: str, now: datetime, err: str | None) -> str:
    n = len(entry["models"])
    when = _age_text(_parse_time(entry["fetched_at"]), now)
    base = f"Azure OpenAI list prices for {n} models ({region} regional and global deployments, pay-as-you-go)"
    if status == "live":
        return f"{base}: live from the {entry.get('source', AZURE_SOURCE)}."
    if status == "cache":
        return f"{base}: cached, fetched {when} ago."
    return f"{base}: stale cache fetched {when} ago ({err})."
