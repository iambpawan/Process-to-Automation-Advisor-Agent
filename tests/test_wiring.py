"""Integration wiring: tenant inventory through the Orchestrator, runtime data region, offline writer,
and the app falling back to offline agents when the LLM fails at runtime (no API calls)."""

import urllib.request
from pathlib import Path

import pytest
import streamlit as st
from streamlit.testing.v1 import AppTest

import advisor.llm
from advisor.config import Settings, load_settings
from advisor.integrations import load_flows, load_license_csv
from advisor.llm import get_provider_or_none
from advisor.llm.base import LLMProvider, ProviderError
from advisor.models import ProcessProfile, ProfileUpdate, Step
from advisor.orchestrator import Orchestrator, runtime_data_region
from advisor.report import PLACEHOLDERS, RENDERERS, existing_automation

ROOT = Path(__file__).resolve().parent.parent
SAMPLE = ROOT / "advisor" / "samples" / "claims_intake.json"
FIXTURES = ROOT / "tests" / "fixtures"
APP = str(ROOT / "app.py")
SETTINGS = Settings("claude", "fake", None, "high", "US", "", "USD")


def sample() -> ProcessProfile:
    return ProcessProfile.model_validate_json(SAMPLE.read_text(encoding="utf-8"))


def flows():
    path = FIXTURES / "flows" / "claim_acknowledgement.json"
    found, _ = load_flows([(path.name, path.read_bytes())])
    return found


def test_placeholders_and_renderers_match():
    assert set(PLACEHOLDERS) == set(RENDERERS)
    assert "EXISTING_AUTOMATION" in PLACEHOLDERS


def test_analyze_without_inventory_is_unchanged():
    a = Orchestrator(SETTINGS).analyze(sample())
    assert a.flows == [] and a.overlaps == [] and a.inventory is None
    assert a.costs.with_ai.license_offset_monthly == 0
    text = existing_automation(a)
    assert "No existing flows provided" in text and "No licence inventory provided" in text


def test_analyze_with_flows_and_inventory():
    inventory, _ = load_license_csv((FIXTURES / "licenses.csv").read_bytes())
    base = Orchestrator(SETTINGS).analyze(sample())
    a = Orchestrator(SETTINGS).analyze(sample(), flows=flows(), inventory=inventory)
    assert a.inventory is inventory
    assert any(o.flow == "Send claim acknowledgement email" for o in a.overlaps)
    assert a.costs.rules_only.license_offset_monthly > 0
    assert a.costs.rules_only.pa_license_monthly < base.costs.rules_only.pa_license_monthly
    text = existing_automation(a)
    assert "Send claim acknowledgement email" in text and "| Process step |" in text
    assert "Spare licences in tenant" in text and "Licence cost avoided" in text


def test_flows_without_overlap():
    p = sample()
    p.steps = [s.model_copy(update={"name": f"Zzz step {i}"}) for i, s in enumerate(p.steps)]
    a = Orchestrator(SETTINGS).analyze(p, flows=flows())
    if not a.overlaps:
        assert "No existing flow appears to cover" in existing_automation(a)


@pytest.mark.parametrize(
    "model, region",
    [
        ("claude-opus-5-5", "US"),
        ("azure-openai-gpt-4o (uaenorth)", "UAE"),
        ("azure-openai-gpt-4o (uaecentral)", "UAE"),
        ("azure-openai-gpt-5 (global)", "GLOBAL (any Azure region)"),
        ("azure-openai-gpt-4o (swedencentral)", "Azure swedencentral"),
        (None, "US"),
    ],
)
def test_runtime_data_region(model, region):
    assert runtime_data_region(model, "US") == region


def test_uae_runtime_model_unblocks_ai_for_resident_phi():
    pricing = Orchestrator(SETTINGS).pricing
    pricing["ai"]["models_per_mtok"]["azure-openai-gpt-4o (uaenorth)"] = {"input": 3.0, "output": 12.0}
    pricing["ai"]["models_per_mtok"]["azure-openai-gpt-4o (global)"] = {"input": 2.5, "output": 10.0}
    us = Orchestrator(SETTINGS).analyze(sample())
    assert not us.compliance.can_send_to_llm and us.ai_decision.approach == "Rules-only"
    pricing["ai"]["runtime_model"] = "azure-openai-gpt-4o (uaenorth)"
    uae = Orchestrator(SETTINGS, pricing).analyze(sample())
    assert uae.compliance.can_send_to_llm and uae.ai_decision.ai_needed
    pricing["ai"]["runtime_model"] = "azure-openai-gpt-4o (global)"
    glob = Orchestrator(SETTINGS, pricing).analyze(sample())
    assert not glob.compliance.can_send_to_llm


def test_offline_document_has_every_section_filled():
    orch = Orchestrator(SETTINGS)
    a = orch.analyze(sample(), flows=flows())
    doc = list(orch.write_document(a))[-1]
    assert isinstance(doc, str) and "{{" not in doc and "Appendix" not in doc
    assert "Send claim acknowledgement email" in doc


def test_merge_remove_steps():
    p = sample()
    first = p.steps[0].name
    p.merge(ProfileUpdate(remove_steps=[first], steps=[Step(name="New step")]))
    assert first not in [s.name for s in p.steps] and p.steps[-1].name == "New step"


def test_provider_or_none(monkeypatch):
    for key in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN"):
        monkeypatch.delenv(key, raising=False)
    assert get_provider_or_none(Settings("offline", "m", "k", "high", "US", "", "USD"))[0] is None
    prov, reason = get_provider_or_none(SETTINGS)
    assert prov is None and "LLM_API_KEY" in reason


def test_load_settings_reads_live_options(monkeypatch, tmp_path):
    monkeypatch.setenv("LIVE_PRICE_REGION", " UAECentral ")
    monkeypatch.setenv("LIVE_CACHE_PATH", str(tmp_path / "c.json"))
    monkeypatch.setenv("LLM_API_KEY", "   ")
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    s = load_settings(tmp_path / "missing.env")
    assert s.live_region == "uaecentral" and s.live_cache_path.endswith("c.json") and s.api_key is None


# ---------- app: the LLM fails at runtime -> offline fallback ----------


class FailingProvider(LLMProvider):
    name = "failing"
    model = "failing"

    def chat(self, messages, tools=None, system=None):
        raise ProviderError("Claude API key is missing or invalid.")
        yield  # pragma: no cover


@pytest.fixture
def bad_key(monkeypatch, tmp_path):
    monkeypatch.setenv("LLM_PROVIDER", "claude")
    monkeypatch.setenv("LLM_API_KEY", "sk-invalid")
    monkeypatch.setenv("LIVE_CACHE_PATH", str(tmp_path / "live_cache.json"))
    monkeypatch.setattr(advisor.llm, "get_provider", lambda settings: FailingProvider())

    def no_network(*args, **kwargs):
        raise AssertionError("the app tried to use the network")

    monkeypatch.setattr(urllib.request, "urlopen", no_network)
    st.cache_resource.clear()
    yield
    st.cache_resource.clear()


def test_app_falls_back_to_offline_when_llm_fails(bad_key):
    at = AppTest.from_file(APP, default_timeout=30).run()
    assert not at.exception
    assert not any("Offline mode" in i.value for i in at.info)  # key configured: no offline banner
    at.chat_input[0].set_value("Invoice matching").run()
    assert not at.exception
    state = at.session_state
    assert "missing or invalid" in state.llm_error
    assert type(state.interviewer).__name__ == "GuidedInterviewer"
    assert state.profile.name == "Invoice matching"
    assert [m.role for m in state.history] == ["user", "assistant"]
    assert any("Switched to offline mode" in c.value for c in at.chat_message[-1].caption)
    assert any("The LLM failed" in w.value for w in at.warning)
    assert any(b.label == "Retry AI agents" for b in at.sidebar.button)


def test_app_writer_falls_back_to_template(bad_key):
    at = AppTest.from_file(APP, default_timeout=30).run()
    next(b for b in at.sidebar.button if b.label == "Load sample").click().run()
    next(b for b in at.button if b.label == "Generate design document").click().run()
    assert not at.exception
    assert at.session_state.document.startswith("# Solution Design")
    assert at.session_state.document_by == "template writer (offline)"
    assert at.session_state.llm_error
