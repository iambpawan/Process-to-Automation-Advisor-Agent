"""Azure OpenAI and Document Intelligence list prices from the public, keyless Azure Retail Prices API.

Findings from the live API (checked Oct 2026):
- Azure OpenAI meters sit under serviceName 'Foundry Models' (formerly 'Cognitive Services'), with
  productName 'Azure OpenAI', 'Azure OpenAI GPT5', 'Azure OpenAI Reasoning', 'Azure OpenAI Media', ...
- skuName encodes model, version, direction and deployment, inconsistently:
  'gpt 4o 1120 Inp regnl', 'gpt-4o-mini-0718-Outp-glbl', 'GPT 5 Mini Inpt Glbl', 'o3 0416 Inp glbl'.
- unitOfMeasure is '1K' (older models) or '1M' (newer); prices are USD.
- Pages hold up to 1000 items; NextPageLink continues.

Deliberate choices: only standard pay-as-you-go text tokens are kept. Any extra qualifier in the
sku (cached, batch, realtime, audio, transcribe, fine-tune, priority, flex, codex, pro, ...) makes
the model name differ from the requested list, so those variants are skipped. Data-zone meters are
skipped. Regional deployments (data processed in the region) are keyed '(<region>)'; global
deployments (data may be processed in any Azure region) are keyed '(global)'. When several dated
versions exist, the newest complete input+output pair (by effectiveStartDate) wins.
"""
import re
import urllib.parse
from collections.abc import Iterable
from typing import Any

from . import Fetch, LiveDataError, http_get_json

PRICES_URL = "https://prices.azure.com/api/retail/prices"
SOURCE_LABEL = "Azure Retail Prices API (prices.azure.com)"
MAX_PAGES = 10

DEFAULT_MODELS = (
    "gpt-4o", "gpt-4o-mini",
    "gpt-4.1", "gpt-4.1-mini", "gpt-4.1-nano",
    "o3", "o3-mini", "o4-mini",
    "gpt-5", "gpt-5-mini", "gpt-5-nano",
)

_DIRECTION = {
    "inp": "input", "input": "input", "inpt": "input", "in": "input",
    "outp": "output", "output": "output", "outpt": "output", "out": "output", "opt": "output",
}
_DEPLOYMENT = {
    "glbl": "global", "global": "global", "gl": "global",
    "regnl": "regional", "regional": "regional", "rgnl": "regional",
    "dz": "datazone", "dzone": "datazone", "datazone": "datazone",
}
_UNIT_TO_PER_M = {"1k": 1000.0, "1m": 1.0}
_DOC_METERS = {"s0 read pages": "read", "s0 pre-built pages": "prebuilt", "s0 custom pages": "custom"}


def _check_region(region: str) -> str:
    region = region.strip().lower()
    if not re.fullmatch(r"[a-z0-9]+", region):
        raise LiveDataError(f"Invalid Azure region name {region!r}")
    return region


def _is_azure_prices_url(url: str) -> bool:
    """True only for https URLs whose host is exactly prices.azure.com (no look-alike hosts)."""
    try:
        parts = urllib.parse.urlsplit(url)
        return parts.scheme == "https" and parts.hostname == "prices.azure.com" and parts.username is None
    except ValueError:
        return False


def fetch_items(odata_filter: str, fetch: Fetch = http_get_json, max_pages: int = MAX_PAGES) -> list[dict[str, Any]]:
    """All price items matching an OData filter, following NextPageLink up to `max_pages`."""
    url: str | None = f"{PRICES_URL}?$filter={urllib.parse.quote(odata_filter)}"
    items: list[dict[str, Any]] = []
    for _ in range(max_pages):
        if not url:
            break
        page = fetch(url)
        if not isinstance(page, dict) or not isinstance(page.get("Items"), list):
            raise LiveDataError("Azure price response has no 'Items' list")
        items += [i for i in page["Items"] if isinstance(i, dict)]
        nxt = page.get("NextPageLink")
        url = nxt if isinstance(nxt, str) and _is_azure_prices_url(nxt) else None
    return items


def parse_sku(sku: str) -> tuple[str, str, str, str] | None:
    """'gpt 4o 1120 Inp regnl' -> ('gpt-4o', '1120', 'input', 'regional'); None if not a token meter."""
    s = sku.lower().replace("-", " ").replace("_", " ")
    s = re.sub(r"\bgpt(?=\d)", "gpt ", s)          # gpt4o -> gpt 4o
    s = re.sub(r"\b(\d(?:\.\d)?o)(mini)\b", r"\1 \2", s)  # 4omini -> 4o mini
    tokens = s.split()
    idx = next((i for i, t in enumerate(tokens) if t in _DIRECTION), None)
    if idx is None or idx == 0:
        return None
    direction = _DIRECTION[tokens[idx]]
    model, version, deployment = [], "", None
    for i, t in enumerate(tokens):
        if i == idx:
            continue
        if re.fullmatch(r"\d{4}", t):
            version = t
        elif t in _DEPLOYMENT:
            deployment = _DEPLOYMENT[t]
        elif i < idx:
            model.append(t)
        else:
            return None  # unknown qualifier after the direction: not a plain token meter
    if not model or deployment is None:
        return None
    return "-".join(model), version, direction, deployment


def _per_mtok(item: dict[str, Any]) -> float | None:
    if item.get("type") != "Consumption" or item.get("tierMinimumUnits", 0) not in (0, 0.0):
        return None
    if item.get("currencyCode", "USD") != "USD" or "token" not in str(item.get("meterName", "")).lower():
        return None
    factor = _UNIT_TO_PER_M.get(str(item.get("unitOfMeasure", "")).strip().lower())
    price = item.get("retailPrice")
    if factor is None or isinstance(price, bool) or not isinstance(price, (int, float)) or price <= 0:
        return None
    return float(price) * factor


def normalise_openai_items(items: Iterable[dict[str, Any]], region: str,
                           models: Iterable[str] = DEFAULT_MODELS) -> dict[str, dict[str, float]]:
    """Reduce raw price items to {"azure-openai-<model> (<region>|global)": {"input", "output"}} USD/1M tokens."""
    wanted = set(models)
    # (model, deployment, version) -> direction -> (effectiveStartDate, price)
    groups: dict[tuple[str, str, str], dict[str, tuple[str, float]]] = {}
    for item in items:
        parsed = parse_sku(str(item.get("skuName", "")))
        price = _per_mtok(item)
        if parsed is None or price is None:
            continue
        model, version, direction, deployment = parsed
        if model not in wanted or deployment == "datazone":
            continue
        start = str(item.get("effectiveStartDate", ""))
        slot = groups.setdefault((model, deployment, version), {})
        if direction not in slot or start > slot[direction][0]:
            slot[direction] = (start, price)

    best: dict[tuple[str, str], tuple[tuple[str, str], dict[str, float]]] = {}
    for (model, deployment, version), slot in groups.items():
        if "input" not in slot or "output" not in slot:
            continue
        rank = (max(slot["input"][0], slot["output"][0]), version)
        if (model, deployment) not in best or rank > best[(model, deployment)][0]:
            best[(model, deployment)] = (rank, {"input": slot["input"][1], "output": slot["output"][1]})

    out = {}
    for (model, deployment), (_, prices) in sorted(best.items()):
        where = region if deployment == "regional" else "global"
        out[f"azure-openai-{model} ({where})"] = {k: round(v, 6) for k, v in prices.items()}
    return out


def fetch_azure_openai_prices(region: str = "uaenorth", fetch: Fetch = http_get_json,
                              models: Iterable[str] = DEFAULT_MODELS) -> dict[str, dict[str, float]]:
    """Azure OpenAI pay-as-you-go text-token prices in USD per 1M tokens, for `region`."""
    region = _check_region(region)
    flt = (
        "(serviceName eq 'Foundry Models' or serviceName eq 'Cognitive Services') "
        f"and armRegionName eq '{region}' and contains(productName, 'OpenAI')"
    )
    prices = normalise_openai_items(fetch_items(flt, fetch), region, models)
    if not prices:
        raise LiveDataError(f"No Azure OpenAI token prices found for region '{region}'")
    return prices


def fetch_azure_document_prices(region: str = "uaenorth", fetch: Fetch = http_get_json) -> dict[str, float]:
    """Azure AI Document Intelligence S0 prices in USD per 1,000 pages: {"read", "prebuilt", "custom"}.

    Uses the first (tier 0) price; volume discounts above 1M pages/month are ignored.
    """
    region = _check_region(region)
    flt = f"armRegionName eq '{region}' and productName eq 'Azure Document Intelligence' and skuName eq 'S0'"
    out: dict[str, float] = {}
    for item in fetch_items(flt, fetch):
        key = _DOC_METERS.get(str(item.get("meterName", "")).strip().lower())
        price = item.get("retailPrice")
        if (key and item.get("type") == "Consumption" and item.get("tierMinimumUnits", 0) in (0, 0.0)
                and str(item.get("unitOfMeasure", "")).lower() == "1k" and isinstance(price, (int, float))
                and not isinstance(price, bool) and price >= 0):
            out[key] = float(price)
    if not out:
        raise LiveDataError(f"No Document Intelligence prices found for region '{region}'")
    return out
