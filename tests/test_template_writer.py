"""Offline TemplateWriter and LLM availability checks (no API calls)."""

import json
from dataclasses import replace
from pathlib import Path

import pytest

from advisor.agents.template_writer import TemplateWriter
from advisor.config import Settings
from advisor.llm.availability import llm_available
from advisor.llm.base import Message, TextDelta
from advisor.models import ProcessProfile
from advisor.orchestrator import Orchestrator
from advisor.report import PLACEHOLDERS, fill, to_docx

SAMPLE = Path(__file__).resolve().parent.parent / "advisor" / "samples" / "claims_intake.json"
SETTINGS = Settings("claude", "fake", None, "high", "US", "", "USD")
HEADINGS = [
    "# Solution Design: ",
    "## 1. Executive summary",
    "## 2. Process summary",
    "## 3. Automation fit",
    "## 4. AI decision",
    "## 5. Target architecture",
    "## 6. Risks and compliance",
    "## 7. Cost and ROI",
    "## 8. Next steps",
]


def sample_data() -> dict:
    return json.loads(SAMPLE.read_text(encoding="utf-8"))


def structured(data: dict) -> dict:
    for s in data["steps"]:
        s.update(input_type="structured", requires_judgment=False, needs_language_understanding=False)
    return data


def non_sensitive(data: dict) -> dict:
    data["data"] = {"contains_phi": False, "contains_pii": False, "uae_data_residency_required": False,
                    "classification": "internal"}
    return data


def hybrid(data: dict) -> dict:
    next(s for s in data["steps"] if s.get("name") == "Register claim")["minutes_per_run"] = 20
    return data


def assess(data: dict, region: str = "US"):
    return Orchestrator(replace(SETTINGS, llm_data_region=region)).analyze(ProcessProfile.model_validate(data))


def run(a) -> tuple[list, list[Message], str]:
    history: list[Message] = []
    events = list(TemplateWriter().write(a, history))
    return events, history, history[-1].content


CASES = {
    "sample_us": (sample_data, "US", "Rules-only"),
    "sample_uae": (sample_data, "UAE", "AI-first"),
    "all_structured": (lambda: structured(sample_data()), "US", "Rules-only"),
    "non_sensitive_us": (lambda: non_sensitive(sample_data()), "US", "AI-first"),
    "hybrid_uae": (lambda: hybrid(sample_data()), "UAE", "Hybrid"),
    "structured_non_sensitive": (lambda: non_sensitive(structured(sample_data())), "US", "Rules-only"),
}


@pytest.fixture(params=list(CASES))
def case(request):
    make, region, approach = CASES[request.param]
    a = assess(make(), region)
    assert a.ai_decision.approach == approach
    return a


def test_outline_and_placeholders(case):
    events, history, narrative = run(case)
    assert all(isinstance(e, TextDelta) for e in events) and len(events) > 1
    assert "".join(e.text for e in events).rstrip() == narrative.rstrip()
    assert history[-1].role == "assistant"
    positions = [narrative.index(h) for h in HEADINGS]
    assert positions == sorted(positions)
    assert narrative.startswith(f"# Solution Design: {case.profile.name}")
    lines = narrative.splitlines()
    for key in PLACEHOLDERS:
        token = "{{" + key + "}}"
        assert narrative.count(token) == 1
        assert token in lines  # on its own line
    # placeholders sit in the sections the Writer outline defines
    for key, section in [("PROCESS_TABLE", "## 2."), ("FIT_SCORE", "## 3."), ("AI_DECISION", "## 4."),
                         ("DELIVERY", "## 5."), ("COMPLIANCE", "## 6."), ("COST_TABLE", "## 7.")]:
        start = narrative.index(section)
        nxt = narrative.find("\n## ", start + 1)
        assert "{{" + key + "}}" in narrative[start: nxt if nxt != -1 else None]


def test_fill_and_docx(case):
    _, _, narrative = run(case)
    doc = fill(narrative, case)
    assert "{{" not in doc and "}}" not in doc
    assert "Appendix" not in doc  # every placeholder was used, nothing appended
    assert case.ai_decision.approach in doc
    assert to_docx(doc)[:2] == b"PK"


def test_orchestrator_style_fill_reads_history(case):
    history: list[Message] = [Message(role="user", content="earlier")]
    list(TemplateWriter().write(case, history))
    narrative = next(m.content for m in reversed(history) if m.role == "assistant" and m.content)
    assert len(history) == 2
    assert "{{" not in fill(narrative, case)


def test_render_matches_write():
    a = assess(sample_data(), "UAE")
    assert TemplateWriter().render(a) == run(a)[2]


def test_deterministic():
    a = assess(sample_data(), "UAE")
    assert run(a)[2] == run(a)[2]


def test_us_sample_explains_residency_block():
    a = assess(sample_data(), "US")
    text = run(a)[2]
    assert "Rules-only" in text
    assert "Data residency" in text
    assert "remain manual" in text
    assert "Alternative" in text
    assert "Assess the alternative" in text
    assert "Build an AI evaluation set" not in text


def test_uae_sample_covers_ai_risks_and_hitl():
    a = assess(sample_data(), "UAE")
    text = run(a)[2]
    assert "AI-first" in text
    assert "AI accuracy" in text and "AI drift" in text
    assert "Build an AI evaluation set" in text
    assert "Triage claim email" in text and "Extract claim form data" in text
    assert "Human review:" in text
    assert "Make UI automation resilient" in text  # legacy claims system runs on a desktop flow
    assert "Compliance sign-off" in text


def test_no_ai_signals_narrative():
    a = assess(non_sensitive(structured(sample_data())), "US")
    text = run(a)[2]
    assert "No step shows a need for AI" in text
    assert "AI accuracy" not in text
    assert "Data classification" in text
    assert "Compliance sign-off" not in text


def test_fit_names_strongest_and_weakest():
    a = assess(sample_data(), "UAE")
    comps = sorted(a.fit.components, key=lambda c: c.points / c.max_points)
    text = run(a)[2]
    fit_section = text[text.index("## 3."): text.index("## 4.")]
    assert f"strongest factor is **{comps[-1].name}**" in fit_section
    assert f"weakest is **{comps[0].name}**" in fit_section


def test_architecture_lists_every_step_in_order():
    a = assess(sample_data(), "UAE")
    text = run(a)[2]
    arch = text[text.index("## 5."): text.index("## 6.")]
    idx = [arch.index(f"{i}. **{sp.step}** — {sp.tool}") for i, sp in enumerate(a.step_plans, 1)]
    assert idx == sorted(idx)


def test_next_steps_start_with_pricing():
    text = run(assess(sample_data(), "US"))[2]
    nxt = text[text.index("## 8."):]
    assert "1. **Validate the pricing assumptions**" in nxt


# -- availability -----------------------------------------------------------

@pytest.fixture
def clean_env(monkeypatch):
    for k in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN"):
        monkeypatch.delenv(k, raising=False)
    return monkeypatch


@pytest.mark.parametrize("provider", ["offline", "none", "", "OFFLINE"])
def test_offline_providers(clean_env, provider):
    ok, reason = llm_available(replace(SETTINGS, provider=provider, api_key="sk-x"))
    assert not ok and reason


def test_claude_with_settings_key(clean_env):
    assert llm_available(replace(SETTINGS, api_key="sk-ant-test"))[0]


def test_claude_without_key(clean_env):
    ok, reason = llm_available(replace(SETTINGS, api_key=None))
    assert not ok and "LLM_API_KEY" in reason
    assert not llm_available(replace(SETTINGS, api_key="   "))[0]


@pytest.mark.parametrize("var", ["ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN"])
def test_claude_with_env_key(clean_env, var):
    clean_env.setenv(var, "token")
    ok, reason = llm_available(replace(SETTINGS, api_key=None))
    assert ok and var in reason


def test_claude_blank_env_key(clean_env):
    clean_env.setenv("ANTHROPIC_API_KEY", "")
    assert not llm_available(replace(SETTINGS, api_key=None))[0]


def test_ollama_not_implemented(clean_env):
    assert llm_available(replace(SETTINGS, provider="ollama")) == (False, "Ollama provider not implemented yet")


def test_unknown_provider(clean_env):
    ok, reason = llm_available(replace(SETTINGS, provider="gpt"))
    assert not ok and "gpt" in reason
