"""Live public-data layer. No test touches the network: every fetch is a fake."""
import json
from datetime import datetime, timedelta, timezone
from urllib.parse import unquote

import pytest

from advisor.analysis import load_pricing, pricing_in
from advisor.analysis.cost import ai_cost_per_step
from advisor.live import LiveDataError, fetch_azure_document_prices, fetch_azure_openai_prices, fetch_fx_to_usd
from advisor.live import refresh_pricing
from advisor.live.azure_prices import normalise_openai_items, parse_sku
from advisor.live.fx import ER_API_URL, FRANKFURTER_URL

NOW = datetime(2026, 10, 3, 12, 0, tzinfo=timezone.utc)

ER_API = {
    "result": "success", "base_code": "USD",
    "rates": {"USD": 1, "AED": 3.6725, "EUR": 0.8, "GBP": 0.75, "SAR": 3.75, "INR": 100.0, "JPY": 150.0},
}
FRANKFURTER = {"amount": 1.0, "base": "USD", "date": "2026-10-02", "rates": {"EUR": 0.9, "GBP": 0.8, "INR": 90.0}}


def item(sku, price, unit="1K", start="2025-01-01T00:00:00Z", meter=None, **kw):
    base = {
        "currencyCode": "USD", "tierMinimumUnits": 0.0, "retailPrice": price, "unitPrice": price,
        "armRegionName": "uaenorth", "effectiveStartDate": start, "skuName": sku,
        "meterName": meter or f"{sku} {'1M ' if unit == '1M' else ''}Tokens", "productName": "Azure OpenAI",
        "serviceName": "Foundry Models", "unitOfMeasure": unit, "type": "Consumption",
    }
    return base | kw


OPENAI_ITEMS = [
    # gpt-4o: three dated versions; 1120 is newest and also has a regional deployment
    item("gpt 4o 0513 Input global", 0.005, start="2024-07-01T00:00:00Z"),
    item("gpt 4o 0513 Output global", 0.015, start="2024-07-01T00:00:00Z"),
    item("gpt-4o-0806-Inp-glbl", 0.0025, start="2024-08-01T00:00:00Z"),
    item("gpt-4o-0806-Outp-glbl", 0.01, start="2024-08-01T00:00:00Z"),
    item("gpt 4o 1120 Inp glbl", 0.0025, start="2024-12-01T00:00:00Z"),
    item("gpt 4o 1120 Outp glbl", 0.01, start="2024-12-01T00:00:00Z"),
    item("gpt 4o 1120 Inp regnl", 0.003025, start="2025-12-01T00:00:00Z"),
    item("gpt 4o 1120 Outp regnl", 0.0121, start="2025-12-01T00:00:00Z"),
    # variants that must be ignored
    item("gpt 4o 1120 cached Inp glbl", 0.00125),
    item("gpt 4o 1120 Batch Inp glbl", 0.00125),
    item("gpt-4o-rt-txt-1217 Inp glbl", 0.005),
    item("gpt-4o-aud-0603 Inp glbl", 0.04),
    item("gpt-4o-transcribe-txt-inp-glbl", 0.0025),
    item("gpt 4.1 Inp dz", 0.0022),
    item("gpt 4.1 Outp dz", 0.0088),
    item("gpt 4.1 Inp glbl", 0.002, tierMinimumUnits=1000.0),  # volume tier
    item("gpt 4.1 Outp glbl", 9.9, type="Reservation"),
    # gpt-4.1 plain, 1K units
    item("gpt 4.1 Inp glbl", 0.002),
    item("gpt 4.1 Outp glbl", 0.008),
    # newer naming, 1M units
    item("GPT 5 Mini Inpt Glbl", 0.25, unit="1M", productName="Azure OpenAI GPT5"),
    item("GPT 5 Mini outpt Glbl", 2.0, unit="1M", productName="Azure OpenAI GPT5"),
    item("GPT 5 Mini cchd Inpt Glbl", 0.025, unit="1M", productName="Azure OpenAI GPT5"),
    item("gpt-4o-mini-0718-Inp-glbl", 0.00015),
    item("gpt-4o-mini-0718-Outp-glbl", 0.0006),
    item("o4-mini 0416 Inp glbl", 0.0011, productName="Azure OpenAI Reasoning"),
    item("o4-mini 0416 Outp glbl", 0.0044, productName="Azure OpenAI Reasoning"),
    # input only: incomplete pair, dropped
    item("o3 0416 Inp glbl", 0.002),
    # non-token meter
    item("Provisioned Managed Regional", 2.36, unit="1/Hour", meter="Provisioned Managed Regional Unit"),
]

DOC_ITEMS = [
    {"meterName": "S0 Read Pages", "unitOfMeasure": "1K", "retailPrice": 1.5, "tierMinimumUnits": 0.0, "type": "Consumption"},
    {"meterName": "S0 Read Pages", "unitOfMeasure": "1K", "retailPrice": 0.6, "tierMinimumUnits": 1000.0, "type": "Consumption"},
    {"meterName": "S0 Pre-built Pages", "unitOfMeasure": "1K", "retailPrice": 10.0, "tierMinimumUnits": 0.0, "type": "Consumption"},
    {"meterName": "S0 Custom Pages", "unitOfMeasure": "1K", "retailPrice": 30.0, "tierMinimumUnits": 0.0, "type": "Consumption"},
    {"meterName": "S0 Batch Read Pages", "unitOfMeasure": "1K", "retailPrice": 1.5, "tierMinimumUnits": 0.0, "type": "Consumption"},
    {"meterName": "S0 Training", "unitOfMeasure": "1 Hour", "retailPrice": 3.0, "tierMinimumUnits": 0.0, "type": "Consumption"},
]


class FakeFetch:
    """Routes URLs to canned JSON; records calls. A value that is an Exception is raised."""

    def __init__(self, fx=ER_API, frankfurter=FRANKFURTER, openai=OPENAI_ITEMS, docs=DOC_ITEMS, openai_pages=None):
        self.fx, self.frankfurter, self.openai, self.docs = fx, frankfurter, openai, docs
        self.openai_pages = openai_pages
        self.calls: list[str] = []

    def _ret(self, value):
        if isinstance(value, Exception):
            raise value
        return value

    def __call__(self, url):
        self.calls.append(url)
        if url == ER_API_URL:
            return self._ret(self.fx)
        if url == FRANKFURTER_URL:
            return self._ret(self.frankfurter)
        if url.startswith("https://prices.azure.com"):
            text = unquote(url)
            if "Document Intelligence" in text:
                return self._ret({"Items": self.docs, "NextPageLink": None})
            if self.openai_pages is not None:
                return self._ret(self.openai_pages[url])
            return self._ret({"Items": self.openai, "NextPageLink": None})
        raise AssertionError(f"unexpected url {url}")


def offline(url):
    raise LiveDataError("network down")


# ---------- fx ----------

def test_fx_inverts_rates_per_usd():
    fx = fetch_fx_to_usd(fetch=FakeFetch())
    assert fx["USD"] == 1.0
    assert fx["AED"] == pytest.approx(1 / 3.6725)
    assert fx["INR"] == pytest.approx(0.01)
    assert "JPY" in fx


def test_fx_falls_back_to_frankfurter():
    fake = FakeFetch(fx=LiveDataError("down"))
    fx = fetch_fx_to_usd(fetch=fake)
    assert fx["EUR"] == pytest.approx(1 / 0.9)
    assert fake.calls == [ER_API_URL, FRANKFURTER_URL]


@pytest.mark.parametrize("bad", [
    [], {"result": "error", "error-type": "quota"}, {"result": "success", "base_code": "EUR", "rates": {"USD": 1.1}},
    {"result": "success", "base_code": "USD"}, {"result": "success", "base_code": "USD", "rates": {"AED": -1, "x": 2}},
])
def test_fx_rejects_bad_shapes(bad):
    with pytest.raises(LiveDataError):
        fetch_fx_to_usd(fetch=FakeFetch(fx=bad, frankfurter=bad))


def test_fx_skips_invalid_individual_rates():
    data = {"result": "success", "base_code": "USD", "rates": {"AED": 3.6725, "BAD": 0, "EUR": "x", "GBP": True}}
    fx = fetch_fx_to_usd(fetch=FakeFetch(fx=data))
    assert set(fx) == {"USD", "AED"}


# ---------- azure prices ----------

@pytest.mark.parametrize("sku,expected", [
    ("gpt 4o 1120 Inp regnl", ("gpt-4o", "1120", "input", "regional")),
    ("gpt-4o-mini-0718-Outp-glbl", ("gpt-4o-mini", "0718", "output", "global")),
    ("GPT 5 Mini Inpt Glbl", ("gpt-5-mini", "", "input", "global")),
    ("o3 mini 0131 output glbl", ("o3-mini", "0131", "output", "global")),
    ("gpt 4o 0513 Input global", ("gpt-4o", "0513", "input", "global")),
    ("gpt4o mini 0718 Inp glbl", ("gpt-4o-mini", "0718", "input", "global")),
    ("gpt 4.1 Inp dz", ("gpt-4.1", "", "input", "datazone")),
    ("gpt 4o 1120 cached Inp glbl", ("gpt-4o-cached", "1120", "input", "global")),
    ("Provisioned Managed Regional", None),
    ("gpt 4o Inp", None),  # no deployment: skipped, not guessed
    ("gpt4o mn trscb txt in gl 1215", ("gpt-4o-mn-trscb-txt", "1215", "input", "global")),
])
def test_parse_sku(sku, expected):
    assert parse_sku(sku) == expected


def test_normalise_picks_standard_newest_pairs_per_mtok():
    out = normalise_openai_items(OPENAI_ITEMS, "uaenorth")
    assert out["azure-openai-gpt-4o (global)"] == {"input": 2.5, "output": 10.0}
    assert out["azure-openai-gpt-4o (uaenorth)"] == {"input": 3.025, "output": 12.1}
    assert out["azure-openai-gpt-4.1 (global)"] == {"input": 2.0, "output": 8.0}
    assert out["azure-openai-gpt-5-mini (global)"] == {"input": 0.25, "output": 2.0}
    assert out["azure-openai-gpt-4o-mini (global)"] == {"input": 0.15, "output": 0.6}
    assert out["azure-openai-o4-mini (global)"] == {"input": 1.1, "output": 4.4}
    assert not any("o3" in k for k in out)  # input without output
    assert not any("cached" in k or "batch" in k for k in out)
    assert len(out) == 6


def test_normalise_respects_model_list():
    out = normalise_openai_items(OPENAI_ITEMS, "uaenorth", models=["gpt-4.1"])
    assert list(out) == ["azure-openai-gpt-4.1 (global)"]


def test_fetch_openai_prices_builds_filter_and_follows_pages():
    fake = FakeFetch()
    first = None

    def fetch(url):
        nonlocal first
        if first is None:
            first = url
            return {"Items": OPENAI_ITEMS[:8], "NextPageLink": "https://prices.azure.com/api/retail/prices?$skip=1000"}
        return {"Items": OPENAI_ITEMS[8:], "NextPageLink": None}

    out = fetch_azure_openai_prices("UAENorth", fetch=fetch)
    flt = unquote(first)
    assert "armRegionName eq 'uaenorth'" in flt and "contains(productName, 'OpenAI')" in flt
    assert "serviceName eq 'Foundry Models'" in flt
    assert "azure-openai-gpt-5-mini (global)" in out and "azure-openai-gpt-4o (uaenorth)" in out
    assert fake.calls == []


def test_page_cap_and_foreign_next_link():
    calls = []

    def loop(url):
        calls.append(url)
        return {"Items": OPENAI_ITEMS, "NextPageLink": "https://prices.azure.com/next"}

    fetch_azure_openai_prices(fetch=loop)
    assert len(calls) == 10

    calls.clear()

    def evil(url):
        calls.append(url)
        return {"Items": OPENAI_ITEMS, "NextPageLink": "https://evil.example/steal"}

    fetch_azure_openai_prices(fetch=evil)
    assert len(calls) == 1


def test_openai_prices_errors():
    with pytest.raises(LiveDataError):
        fetch_azure_openai_prices("uae north'; drop", fetch=FakeFetch())
    with pytest.raises(LiveDataError):
        fetch_azure_openai_prices(fetch=lambda url: {"Items": []})
    with pytest.raises(LiveDataError):
        fetch_azure_openai_prices(fetch=lambda url: {"error": "bad"})


def test_document_prices():
    out = fetch_azure_document_prices(fetch=FakeFetch())
    assert out == {"read": 1.5, "prebuilt": 10.0, "custom": 30.0}
    with pytest.raises(LiveDataError):
        fetch_azure_document_prices(fetch=FakeFetch(docs=[]))


# ---------- refresh ----------

@pytest.fixture
def pricing():
    return load_pricing()


def test_refresh_live_overlays_and_writes_cache(pricing, tmp_path):
    cache = tmp_path / "live_cache.json"
    before = json.dumps(pricing, sort_keys=True)
    p, notes = refresh_pricing(pricing, cache_path=cache, fetch=FakeFetch(), now=NOW)

    assert json.dumps(pricing, sort_keys=True) == before  # input untouched
    assert p is not pricing
    assert p["fx_to_usd"]["AED"] == pytest.approx(1 / 3.6725)
    assert p["fx_to_usd"]["EUR"] == pytest.approx(1.25)
    assert p["fx_to_usd"]["USD"] == 1.0
    assert p["fx_to_usd"]["JPY"] == pytest.approx(1 / 150.0)  # every valid live rate is overlaid
    models = p["ai"]["models_per_mtok"]
    assert models["claude-opus-5-5"] == pricing["ai"]["models_per_mtok"]["claude-opus-5-5"]  # static kept
    assert models["azure-openai-gpt-4o (uaenorth)"] == {"input": 3.025, "output": 12.1}
    live = p["_live"]
    assert live["status"] == "live" and live["fx_status"] == "live" and live["azure_status"] == "live"
    assert live["fetched_at"] == NOW.isoformat(timespec="seconds")
    assert any("er-api" in s for s in live["sources"]) and any("Azure" in s for s in live["sources"])
    assert live["document_intelligence_usd_per_1k_pages"]["read"] == 1.5
    text = " ".join(notes)
    assert "Anthropic" in text and "no public pricing API" in text
    assert "Power Automate" in text and "AI Builder" in text
    stored = json.loads(cache.read_text(encoding="utf-8"))
    assert stored["fx"]["rates"]["AED"] == pytest.approx(1 / 3.6725)
    assert "azure-openai-gpt-4o (uaenorth)" in stored["azure"]["uaenorth"]["models"]


def test_refresh_uses_fresh_cache_without_network(pricing, tmp_path):
    cache = tmp_path / "c.json"
    refresh_pricing(pricing, cache_path=cache, fetch=FakeFetch(), now=NOW)
    fake = FakeFetch()
    p, notes = refresh_pricing(pricing, cache_path=cache, fetch=fake, now=NOW + timedelta(hours=23))
    assert fake.calls == []
    assert p["_live"]["status"] == "cache"
    assert p["_live"]["fetched_at"] == NOW.isoformat(timespec="seconds")
    assert "azure-openai-gpt-4o (uaenorth)" in p["ai"]["models_per_mtok"]
    assert any("cached" in n and "23 h ago" in n for n in notes)


def test_refresh_refetches_when_cache_is_old(pricing, tmp_path):
    cache = tmp_path / "c.json"
    refresh_pricing(pricing, cache_path=cache, fetch=FakeFetch(), now=NOW)
    newer = dict(ER_API, rates=dict(ER_API["rates"], AED=4.0))
    fake = FakeFetch(fx=newer)
    later = NOW + timedelta(hours=25)
    p, _ = refresh_pricing(pricing, cache_path=cache, fetch=fake, now=later)
    assert fake.calls
    assert p["_live"]["status"] == "live"
    assert p["fx_to_usd"]["AED"] == pytest.approx(0.25)
    assert json.loads(cache.read_text())["fx"]["fetched_at"] == later.isoformat(timespec="seconds")


def test_refresh_stale_cache_when_offline(pricing, tmp_path):
    cache = tmp_path / "c.json"
    refresh_pricing(pricing, cache_path=cache, fetch=FakeFetch(), now=NOW)
    p, notes = refresh_pricing(pricing, cache_path=cache, fetch=offline, now=NOW + timedelta(days=3))
    assert p["_live"]["status"] == "cache"
    assert p["_live"]["fx_status"] == "stale" and p["_live"]["azure_status"] == "stale"
    assert p["fx_to_usd"]["AED"] == pytest.approx(1 / 3.6725)
    assert any("stale cache" in n and "3 days ago" in n and "network down" in n for n in notes)


def test_refresh_static_when_offline_and_no_cache(pricing, tmp_path):
    cache = tmp_path / "missing" / "c.json"
    p, notes = refresh_pricing(pricing, cache_path=cache, fetch=offline, now=NOW)
    assert p["_live"]["status"] == "static"
    assert p["_live"]["fetched_at"] is None and p["_live"]["sources"] == []
    assert p["fx_to_usd"] == pricing["fx_to_usd"]
    assert p["ai"]["models_per_mtok"] == pricing["ai"]["models_per_mtok"]
    assert any("static assumptions" in n and "network down" in n for n in notes)
    assert not cache.exists()


def test_refresh_never_raises_on_unexpected_errors(pricing, tmp_path):
    def broken(url):
        raise RuntimeError("boom")

    p, notes = refresh_pricing(pricing, cache_path=tmp_path / "c.json", fetch=broken, now=NOW)
    assert p["_live"]["status"] == "static"
    assert any("boom" in n for n in notes)


def test_refresh_partial_failure(pricing, tmp_path):
    fake = FakeFetch(openai=[], docs=[])
    p, notes = refresh_pricing(pricing, cache_path=tmp_path / "c.json", fetch=fake, now=NOW)
    assert p["_live"]["status"] == "live"
    assert p["_live"]["fx_status"] == "live" and p["_live"]["azure_status"] == "static"
    assert not any(k.startswith("azure-openai") for k in p["ai"]["models_per_mtok"])
    assert any("Azure OpenAI prices (uaenorth): not available" in n for n in notes)


def test_refresh_doc_prices_optional(pricing, tmp_path):
    p, _ = refresh_pricing(pricing, cache_path=tmp_path / "c.json", fetch=FakeFetch(docs=[]), now=NOW)
    assert p["_live"]["azure_status"] == "live"
    assert p["_live"]["document_intelligence_usd_per_1k_pages"] == {}


def test_refresh_corrupt_cache_is_ignored(pricing, tmp_path):
    cache = tmp_path / "c.json"
    cache.write_text("{not json", encoding="utf-8")
    p, _ = refresh_pricing(pricing, cache_path=cache, fetch=offline, now=NOW)
    assert p["_live"]["status"] == "static"
    cache.write_text(json.dumps({"version": 1, "fx": {"fetched_at": "nope", "rates": {"AED": 1}},
                                 "azure": {"uaenorth": {"fetched_at": NOW.isoformat(), "models": "x"}}}))
    p, _ = refresh_pricing(pricing, cache_path=cache, fetch=offline, now=NOW)
    assert p["_live"]["status"] == "static"


def test_refresh_cache_is_per_region(pricing, tmp_path):
    cache = tmp_path / "c.json"
    refresh_pricing(pricing, cache_path=cache, fetch=FakeFetch(), now=NOW)
    fake = FakeFetch()
    p, _ = refresh_pricing(pricing, region="westeurope", cache_path=cache, fetch=fake, now=NOW)
    assert any("westeurope" in unquote(u) for u in fake.calls)
    assert ER_API_URL not in fake.calls  # fx still fresh in cache
    assert "azure-openai-gpt-4o (westeurope)" in p["ai"]["models_per_mtok"]
    stored = json.loads(cache.read_text())
    assert set(stored["azure"]) == {"uaenorth", "westeurope"}


def test_refresh_allow_network_false(pricing, tmp_path):
    cache = tmp_path / "c.json"
    refresh_pricing(pricing, cache_path=cache, fetch=FakeFetch(), now=NOW)
    fake = FakeFetch()
    p, notes = refresh_pricing(pricing, cache_path=cache, fetch=fake, now=NOW + timedelta(days=10),
                               allow_network=False)
    assert fake.calls == []
    assert p["_live"]["status"] == "cache"
    assert any("turned off" in n for n in notes)


def test_refresh_unwritable_cache_adds_note(pricing, tmp_path):
    blocker = tmp_path / "file"
    blocker.write_text("x")
    p, notes = refresh_pricing(pricing, cache_path=blocker / "c.json", fetch=FakeFetch(), now=NOW)
    assert p["_live"]["status"] == "live"
    assert any("Could not write" in n for n in notes)


def test_refresh_converts_azure_prices_into_pricing_currency(pricing, tmp_path):
    aed_pricing = pricing_in(pricing, "AED")
    p, _ = refresh_pricing(aed_pricing, cache_path=tmp_path / "c.json", fetch=FakeFetch(), now=NOW)
    gpt = p["ai"]["models_per_mtok"]["azure-openai-gpt-4.1 (global)"]
    assert gpt["input"] == pytest.approx(2.0 * 3.6725)


def test_pricing_in_and_cost_work_on_refreshed_dict(pricing, tmp_path):
    from advisor.models import ProcessProfile
    from pathlib import Path

    p, _ = refresh_pricing(pricing, cache_path=tmp_path / "c.json", fetch=FakeFetch(), now=NOW)
    aed = pricing_in(p, "AED")
    assert aed["currency"] == "AED"
    assert aed["ai"]["models_per_mtok"]["azure-openai-gpt-4o (uaenorth)"]["input"] == pytest.approx(3.025 * 3.6725)
    assert aed["labor"]["build_day_rate"] == pytest.approx(600 * 3.6725)
    assert aed["_live"]["status"] == "live"

    sample = Path(__file__).resolve().parent.parent / "advisor" / "samples" / "claims_intake.json"
    profile = ProcessProfile.model_validate_json(sample.read_text(encoding="utf-8"))
    step = profile.steps[0]
    aed["ai"]["runtime_model"] = "azure-openai-gpt-4o (uaenorth)"
    tokens = aed["ai"]["tokens_per_step_by_input_type"][step.input_type or "unstructured"]
    expected = (tokens["input"] * 3.025 + tokens["output"] * 12.1) * 3.6725 / 1_000_000
    assert ai_cost_per_step(step, aed) == pytest.approx(expected)
