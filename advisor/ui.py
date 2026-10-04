"""Presentation helpers for the Streamlit app: styling, readiness summary and control help text.

Importing this module has no side effects — nothing calls into Streamlit here. `app.py`
injects `CSS` once, renders `readiness(profile)` in the sidebar and passes `FIELD_HELP`
entries as `help=` on its controls.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from advisor.models import STEP_FIELDS, ProcessProfile

# ---------------------------------------------------------------- styling

# One <style> block injected with st.markdown(CSS, unsafe_allow_html=True).
# Colour, type and radius all live in .streamlit/config.toml, which is the supported way to theme
# a Streamlit app. This block is deliberately limited to the few things config.toml cannot express:
# page width, the two-sided chat layout, and small spacing fixes. Streamlit exposes no theme CSS
# variables, so nothing here hard-codes a colour that config.toml owns; a selector that stops
# matching just leaves Streamlit's own styling in place.
CSS = """
<style>
/* Chrome we do not need. The Deploy button is already gone via client.toolbarMode. */
footer, #MainMenu, [data-testid="stDecoration"] { display: none; }

/* Use the whole window (layout="wide") with gutters that shrink on small screens. */
[data-testid="stMainBlockContainer"] {
  max-width: none;
  padding-top: 2rem;
  padding-bottom: 6rem;
  padding-left: clamp(1rem, 4vw, 4rem);
  padding-right: clamp(1rem, 4vw, 4rem);
}
[data-testid="stMainBlockContainer"] h1 { letter-spacing: -0.02em; line-height: 1.15; }
[data-testid="stMainBlockContainer"] p, [data-testid="stMainBlockContainer"] li { line-height: 1.6; }

/* A two-sided conversation: the person on the left, the advisor on the right.
   Each turn is capped so long answers stay readable on a wide screen. */
[data-testid="stChatMessage"] {
  background: transparent;
  padding: 0.2rem 0 0.7rem 0;
  gap: 0.75rem;
  width: fit-content;
  max-width: min(46rem, 82%);
}
/* app.py wraps each turn in st.container(key="turn-<role>-N"), which Streamlit turns into an
   st-key-turn-<role>-N class. A custom avatar collapses Streamlit's own user/assistant testids
   into one, so the key is what tells the two sides apart. */
[class*="st-key-turn-user"] [data-testid="stChatMessage"] {
  margin-right: auto;   /* you: left */
}
[class*="st-key-turn-assistant"] [data-testid="stChatMessage"] {
  flex-direction: row-reverse;   /* the advisor: right, avatar on the outside */
  margin-left: auto;
  text-align: left;
}
/* The bubble itself is the message content, so both sides read as cards. */
[data-testid="stChatMessage"] [data-testid="stChatMessageContent"] {
  border-radius: 1rem;
  padding: 0.7rem 1rem;
}
[class*="st-key-turn-user"] [data-testid="stChatMessageContent"] {
  background: rgba(127, 127, 127, 0.11);
}
[class*="st-key-turn-assistant"] [data-testid="stChatMessageContent"] {
  background: rgba(127, 127, 127, 0.05);
}
[data-testid="stChatMessage"] [data-testid="stChatMessageContent"] > div > :last-child { margin-bottom: 0; }

/* The composer draws its own bordered box, so this only sets the outer shell to nothing
   (otherwise the two borders stack and read as a box inside a box). */
[data-testid="stChatInput"] { border: none; background: transparent; box-shadow: none; padding: 0; }

/* Metric cards: a little air inside the border st.metric(border=True) draws. */
[data-testid="stMetric"] { padding: 0.85rem 1rem; }
[data-testid="stMetricLabel"] { letter-spacing: 0.03em; font-size: 0.78rem; opacity: 0.85; }

/* Sidebar: quieter section headings so the content leads. */
[data-testid="stSidebar"] h2, [data-testid="stSidebar"] h3 {
  font-size: 0.8rem;
  text-transform: uppercase;
  letter-spacing: 0.07em;
  opacity: 0.7;
  margin-top: 1.3rem;
  margin-bottom: 0.3rem;
}
[data-testid="stSidebar"] [data-testid="stCaptionContainer"] { line-height: 1.5; }

/* Stack the metric row instead of squeezing four cards onto a phone. */
@media (max-width: 640px) {
  [data-testid="stChatMessage"] { max-width: 100%; }
  [data-testid="stMainBlockContainer"] { padding-bottom: 7rem; }
}
</style>
"""

# ---------------------------------------------------------------- readiness


@dataclass
class ReadinessGroup:
    """One themed slice of the interview: how much is answered and what is still open."""

    title: str
    done: int
    total: int
    missing: list[str] = field(default_factory=list)
    help: str = ""

    @property
    def complete(self) -> bool:
        return self.done >= self.total


@dataclass
class Readiness:
    """Human-readable view of `missing_fields()`, grouped for the sidebar."""

    groups: list[ReadinessGroup]
    done: int
    total: int

    @property
    def complete(self) -> bool:
        return self.done >= self.total

    @property
    def fraction(self) -> float:
        """0.0-1.0, safe to hand straight to st.progress."""
        return 1.0 if self.total == 0 else self.done / self.total

    @property
    def percent(self) -> int:
        return round(self.fraction * 100)


BASIC_LABELS = {
    "name": "What the process is called",
    "transactions_per_month": "How many times it runs each month",
    "hourly_labor_cost": "Fully loaded cost of one staff hour",
    "process_stability": "How often the process itself changes",
}

STEP_FIELD_LABELS = {
    "minutes_per_run": "how many minutes it takes",
    "system": "which system it is done in",
    "input_type": "what kind of input it works from",
    "requires_judgment": "whether it needs human judgement",
    "rules_documented": "whether its decision rules are written down",
    "exception_rate_pct": "how often it hits an exception",
}

SYSTEM_FIELD_LABELS = {
    "has_api_or_connector": "does it have an API or a connector?",
    "ui_change_frequency": "how often do its screens change?",
}

DATA_LABELS = {
    "contains_phi": "Does it involve patient health data?",
    "contains_pii": "Does it involve personal data about individuals?",
    "uae_data_residency_required": "Must the data stay inside the UAE?",
}

GROUP_HELP = {
    "Process basics": (
        "Name the process, say roughly how many times it runs each month, what one staff hour costs "
        "fully loaded, and how often the process changes. Volume and hourly cost drive every saving "
        "and payback figure."
    ),
    "Steps": (
        "Walk through the process step by step — at least two — and for each one say how long it takes, "
        "where it is done, what it works from, and how predictable it is. The steps decide whether rules "
        "alone will do or AI is needed."
    ),
    "Systems": (
        "For every system a step touches, say whether it offers an API or a ready connector and how often "
        "its screens change. That is what separates a reliable integration from brittle screen automation."
    ),
    "Data": (
        "Tell us whether the process handles patient health data or personal data, and whether the data must "
        "stay inside the UAE. These three answers set the compliance guardrails on the recommendation."
    ),
}


def _basics(p: ProcessProfile) -> ReadinessGroup:
    missing = [BASIC_LABELS[f] for f in BASIC_LABELS if getattr(p, f) is None]
    total = len(BASIC_LABELS)
    return ReadinessGroup("Process basics", total - len(missing), total, missing, GROUP_HELP["Process basics"])


def _steps(p: ProcessProfile) -> ReadinessGroup:
    missing: list[str] = []
    total = 1  # the "at least two steps" requirement itself
    if len(p.steps) < 2:
        missing.append("At least two steps in the process")
    for step in p.steps:
        total += len(STEP_FIELDS)
        missing += [
            f"{step.name} — {STEP_FIELD_LABELS[f]}" for f in STEP_FIELDS if getattr(step, f) is None
        ]
    return ReadinessGroup("Steps", total - len(missing), total, missing, GROUP_HELP["Steps"])


def _systems(p: ProcessProfile) -> ReadinessGroup:
    missing: list[str] = []
    total = 0
    # A step may name a system we know nothing about yet; that gap counts here too.
    for step in p.steps:
        if step.system and p.system_by_name(step.system) is None:
            total += 1
            missing.append(f"{step.system} — we still need the details of this system")
    for system in p.systems:
        total += len(SYSTEM_FIELD_LABELS)
        missing += [
            f"{system.name} — {SYSTEM_FIELD_LABELS[f]}"
            for f in SYSTEM_FIELD_LABELS
            if getattr(system, f) is None
        ]
    return ReadinessGroup("Systems", total - len(missing), total, missing, GROUP_HELP["Systems"])


def _data(p: ProcessProfile) -> ReadinessGroup:
    missing = [DATA_LABELS[f] for f in DATA_LABELS if getattr(p.data, f) is None]
    total = len(DATA_LABELS)
    return ReadinessGroup("Data", total - len(missing), total, missing, GROUP_HELP["Data"])


def readiness(profile: ProcessProfile) -> Readiness:
    """Group what the interview still needs, in the user's words.

    Every open item here matches one entry of `missing_fields(profile)`, so
    `readiness(p).complete` is always `not missing_fields(p)`.
    """
    groups = [_basics(profile), _steps(profile), _systems(profile), _data(profile)]
    return Readiness(groups, sum(g.done for g in groups), sum(g.total for g in groups))


# ---------------------------------------------------------------- control help text

# Passed as help= on the matching sidebar control, uploader or tab in app.py.
FIELD_HELP = {
    "flow_files": (
        "Power Automate flow exports (.zip from 'Export > Package', or a flow definition .json). "
        "Used to spot automation you already own. Nothing is sent to Microsoft."
    ),
    "license_file": (
        "A CSV of your Power Platform licences — columns for Premium, Process and AI Builder seats or "
        "credits, assigned and available. Export it from the Microsoft 365 admin centre."
    ),
    "runtime_model": (
        "Which AI model the cost estimate should price, and the data region that comes with it. "
        "It changes the figures only — the interview itself uses the model configured in .env."
    ),
    "live_refresh": (
        "Fetches public FX rates and Azure pay-as-you-go list prices now. Without it the app uses the "
        "static planning assumptions. List prices are not your negotiated rates."
    ),
    "live_data": (
        "Shows whether prices came from a live fetch or the static assumptions, and when they were "
        "fetched. Figures older than a day fall back to the assumptions."
    ),
    "import_text": (
        "Paste an SOP, work instruction or a plain description. We pull out the steps for you to check "
        "before anything is merged into the profile."
    ),
    "import_diagram": (
        "A process diagram exported as BPMN (.bpmn / .xml) or Visio (.vsdx). Task and activity shapes "
        "become draft steps you can review."
    ),
    "import_voice": (
        "Record or upload a spoken walkthrough. It is transcribed on this machine, then read like pasted "
        "text. Describe the process rather than reading out real records."
    ),
    "profile_json": (
        "Everything collected so far, exactly as the analysis will see it. Handy for checking a figure or "
        "copying the profile elsewhere."
    ),
    "new_assessment": "Clears the profile and the conversation and starts a fresh process from scratch.",
    "load_sample": (
        "Fills the profile with a worked claims-intake example so you can see a finished analysis "
        "straight away."
    ),
    "readiness": (
        "What the analysis still needs from you. Answer the questions in the chat — the analysis unlocks "
        "once every item here is covered."
    ),
}
