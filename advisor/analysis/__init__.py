import copy
import json
from pathlib import Path
from typing import Any

PRICING_FILE = Path(__file__).resolve().parent.parent / "data" / "pricing_assumptions.json"


def load_pricing(path: Path = PRICING_FILE) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def pricing_in(pricing: dict[str, Any], currency: str) -> dict[str, Any]:
    """Copy of `pricing` with every money amount converted into `currency`."""
    fx = pricing["fx_to_usd"]
    currency = str(currency).strip().upper()  # 'aed' is the same currency as 'AED'
    source = pricing["currency"]
    if currency not in fx or source not in fx:
        raise ValueError(
            f"No exchange rate for currency '{currency}'. Use a 3-letter code such as AED, USD or EUR, "
            f"or add the rate to fx_to_usd in {PRICING_FILE.name}."
        )
    factor = fx[source] / fx[currency]
    p = copy.deepcopy(pricing)
    p["currency"] = currency
    p["labor"]["build_day_rate"] *= factor
    p["power_automate"]["premium_per_user_month"] *= factor
    p["power_automate"]["process_license_per_bot_month"] *= factor
    p["ai"]["ai_builder"]["price_per_credit"] *= factor
    for prices in p["ai"]["models_per_mtok"].values():
        prices["input"] *= factor
        prices["output"] *= factor
    return p
