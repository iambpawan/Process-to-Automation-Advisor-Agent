# Process-to-Automation Advisor

**Describe a manual business process in plain language. Get back a costed, compliance-checked
automation recommendation — and the design document to hand to a delivery team.**

Built with Python, Streamlit and the Claude API. **Every figure is computed in Python; the language
model never does arithmetic.**

![The assessment](docs/images/03-assessment.jpg)

---

## The problem

Deciding whether to automate a process usually means a consultant, a spreadsheet and two weeks. The
answer is often wrong in a specific way: AI gets recommended because it is interesting, not because
it pays for itself, and nobody checks whether the data is even allowed to leave the country.

This tool does that assessment in a conversation, and shows its working.

It answers four questions:

1. **Should this be automated at all?** A 0–100 fit score from rules clarity, volume, stability, data
   quality and ROI.
2. **Does it need AI, or will rules do?** Rules-only, Hybrid or AI-first — with AI recommended only
   where it earns its place.
3. **What will it cost, and when does it pay back?** Rules-only against Rules + AI, including
   licences, tokens, build effort and ongoing support.
4. **Is it allowed?** PHI/PII detection and UAE data-residency rules, which can block the AI option
   outright.

---

## What makes it interesting

| | |
|---|---|
| **The maths is never left to the model** | Scores, costs, payback and effort all live in `advisor/analysis/` as plain, unit-tested Python. The model writes narrative with `{{PLACEHOLDER}}` tokens, and Python substitutes the computed tables. A hallucinated number cannot reach the output. |
| **It works with no API key at all** | With no key, a rule-based interviewer and a template writer take over. Analysis, imports, live pricing and the design document are identical. Add a key later and the AI agents switch on by themselves. |
| **Swappable model provider** | Every call goes through one interface (`advisor/llm/base.py`). Claude is implemented; an Ollama provider can be added without touching a line of agent logic. |
| **Compliance that changes the answer** | UAE PDPL and health-data localisation are modelled, not mentioned. If data must stay in the UAE and the model is hosted in the US, the AI recommendation is withdrawn and the reason is stated. Choose a UAE-hosted model and it comes back. |
| **Real public data, no account needed** | Live FX rates and Azure list prices from keyless public APIs, cached locally, fetched only when asked. |
| **Degrades gracefully** | If the API key is wrong or the network drops mid-interview, it says so, falls back to the offline agents and carries on without losing the conversation. |
| **571 tests** | Analysis maths, the agent loop against a fake provider, every importer, and the Streamlit UI driven end to end through `AppTest`. No test touches the network. |

---

## See it work

```powershell
python -m venv .venv
.venv\Scripts\python -m pip install -r requirements.txt
copy .env.example .env          # leave LLM_API_KEY empty — it runs without one
.venv\Scripts\streamlit run app.py
```

Click **Load sample** for a finished healthcare claims-intake assessment, or just start typing.

---

## How it works

You talk; it asks only for what is still missing.

![A conversation](docs/images/02-conversation.jpg)

You can also hand it what you already have — an SOP, a BPMN or Visio diagram, or a voice note
(transcribed locally, never uploaded). Everything extracted is previewed before it is merged.

The results stay hidden until every fact is in, then appear by themselves.

<table>
<tr>
<td width="50%"><img src="docs/images/04-cost-roi.jpg" alt="Cost comparison"></td>
<td width="50%"><img src="docs/images/05-compliance.jpg" alt="Compliance check"></td>
</tr>
<tr>
<td><b>Two costed scenarios</b> — rules-only against rules + AI, with payback and three-year value.</td>
<td><b>Compliance</b> — PHI/PII, UAE residency, and whether data may reach the model at all.</td>
</tr>
</table>

**[Read the full user manual →](docs/USER_MANUAL.md)**

---

## Architecture

```
app.py (Streamlit)
  ├─ Interview ── GuidedInterviewer (no key) │ InterviewerAgent (Claude) ──► ProcessProfile
  ├─ Import ───── text · bpmn · vsdx · voice ──► ImportResult ──► preview ──► merge
  ├─ Live data ── keyless public APIs (FX, Azure retail prices) ──► cache
  ├─ Tenant ───── Power Automate exports · licence CSV
  └─ Orchestrator.analyze(profile, flows, inventory)
       ├─ Compliance    PHI/PII, UAE residency vs the model's region   (Python)
       ├─ AI-Need       AI signals ──► Rules-only │ Hybrid │ AI-first  (Python)
       ├─ Cost          effort, licences, tokens, payback, ROI         (Python)
       ├─ Analyst       fit score, tools, human-in-the-loop, risk      (Python)
       ├─ Reuse         existing flows that overlap these steps        (Python)
       └─ Writer        narrative only ──► report.py fills the tables
advisor/llm/
  base.py     LLMProvider.chat(messages, tools, system) — the only interface agents see
  claude.py   streaming, adaptive thinking, prompt caching, refusal fallback
  ollama.py   placeholder: implement chat() and set LLM_PROVIDER=ollama
```

The specialist agents are deterministic Python wrappers over `advisor/analysis/`. Only the
interviewer and the writer are language models, and neither is allowed near a number.

---

## Configuration

Everything is set in `.env` (see `.env.example`):

| Variable | Meaning |
|---|---|
| `LLM_PROVIDER` | `claude`, `ollama` (placeholder) or `offline` |
| `LLM_MODEL` | e.g. `claude-sonnet-5-5` |
| `LLM_API_KEY` | Leave empty to run keyless. `ANTHROPIC_API_KEY` also works. |
| `LLM_EFFORT` | Claude thinking effort: `low` … `max` |
| `LLM_DATA_REGION` | Where the model processes data — drives the compliance check |
| `CURRENCY` | Default currency (needs an FX rate) |
| `LIVE_PRICE_REGION` | Azure region for live list prices (default `uaenorth`) |

Every price, licence cost, token estimate and effort figure is editable in
`advisor/data/pricing_assumptions.json`.

> **The default prices are planning placeholders.** Azure figures are public list prices, not
> negotiated rates. Validate them before putting a number in front of anyone.

---

## Tests

```powershell
.venv\Scripts\python -m pytest -q
```

571 tests: the analysis maths, the agent loop against a fake provider, the guided interviewer, the
text/BPMN/Visio/voice importers, live pricing with fake fetchers, the tenant inventory, the template
writer, and the Streamlit app driven end to end in offline mode — a full interview through to the
design document. None of them touch the network.

---

## Not built yet

The Ollama provider; expanding BPMN sub-processes and Visio group shapes; reading flows directly
from a tenant (you upload exports instead); live Power Automate and Anthropic prices, which have no
public price API.

---

## Licence

MIT — see [LICENSE](LICENSE).
