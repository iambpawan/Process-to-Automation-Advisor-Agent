"""Keyless FX rates. Output matches pricing["fx_to_usd"]: USD value of one unit of each currency."""
import math
from typing import Any

from . import Fetch, LiveDataError, http_get_json

ER_API_URL = "https://open.er-api.com/v6/latest/USD"
FRANKFURTER_URL = "https://api.frankfurter.app/latest?from=USD"
# (url, attribution) in the order tried. Frankfurter (ECB data) has no AED/SAR, so it is only a fallback.
SOURCES = [
    (ER_API_URL, "open.er-api.com (Rates By Exchange Rate API)"),
    (FRANKFURTER_URL, "api.frankfurter.app (ECB reference rates)"),
]


def _rates_to_usd(data: Any) -> dict[str, float]:
    """Validate a 'per 1 USD' response and invert it to 'USD per 1 unit'."""
    if not isinstance(data, dict):
        raise LiveDataError("FX response is not a JSON object")
    if data.get("result", "success") != "success":
        raise LiveDataError(f"FX source reported result={data.get('result')!r}")
    base = data.get("base_code", data.get("base"))
    if base != "USD":
        raise LiveDataError(f"FX base is {base!r}, expected 'USD'")
    rates = data.get("rates")
    if not isinstance(rates, dict) or not rates:
        raise LiveDataError("FX response has no 'rates' object")
    out = {"USD": 1.0}
    for code, rate in rates.items():
        if not (isinstance(code, str) and len(code) == 3 and code.isalpha()):
            continue
        if isinstance(rate, bool) or not isinstance(rate, (int, float)) or not math.isfinite(rate) or rate <= 0:
            continue
        out[code.upper()] = 1.0 / float(rate)
    out["USD"] = 1.0
    if len(out) < 2:
        raise LiveDataError("FX response has no usable rates")
    return out


def fetch_fx_with_source(fetch: Fetch = http_get_json) -> tuple[dict[str, float], str]:
    """Try each source in turn; return (fx_to_usd, source label). Raises LiveDataError if all fail."""
    errors = []
    for url, label in SOURCES:
        try:
            return _rates_to_usd(fetch(url)), label
        except (LiveDataError, OSError, ValueError) as e:
            errors.append(str(e))
    raise LiveDataError("All FX sources failed: " + "; ".join(errors))


def fetch_fx_to_usd(fetch: Fetch = http_get_json) -> dict[str, float]:
    """{currency: USD per 1 unit}, e.g. {"USD": 1.0, "AED": 0.2723, ...}."""
    return fetch_fx_with_source(fetch)[0]
