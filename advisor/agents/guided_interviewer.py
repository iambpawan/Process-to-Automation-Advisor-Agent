"""Guided Interviewer: a deterministic, keyless stand-in for the LLM InterviewerAgent.

It has the same surface (`profile`, `complete`, `summary`, `run_turn`) so the app can
swap it for InterviewerAgent once an API key is configured. Facts are extracted with
the heuristics in advisor.importers.text; nothing here calls a model.
"""

from __future__ import annotations

import difflib
import json
import re
from typing import Any, Iterator

from advisor.agents.base import AgentEvent, Notice, ToolFinished, ToolStarted
from advisor.analysis import load_pricing
from advisor.importers.text import (
    find_systems,
    input_type_of,
    parse_currency,
    parse_hourly_cost,
    parse_minutes,
    parse_number,
    parse_process_text,
    parse_step,
    parse_volume,
    system_facts,
)
from advisor.importers.text import _g
from advisor.llm.base import Message, TextDelta
from advisor.models import STEP_FIELDS, DataProfile, ProcessProfile, ProfileUpdate, Step, System, missing_fields

# Conservative defaults used when the user skips a question. Each is reported to the user.
STEP_DEFAULTS: dict[str, Any] = {
    "minutes_per_run": 5.0,
    "system": "Unknown system",
    "input_type": "semi_structured",
    "requires_judgment": True,
    "rules_documented": False,
    "exception_rate_pct": 10.0,
}
SYSTEM_DEFAULTS: dict[str, Any] = {"has_api_or_connector": False, "ui_change_frequency": "occasional"}
DATA_DEFAULTS: dict[str, Any] = {"contains_phi": True, "contains_pii": True, "uae_data_residency_required": True}
DEFAULT_VOLUME = 100.0
DEFAULT_HOURLY_USD = 25.0
DEFAULT_STABILITY = "changes_quarterly"
DATA_FIELDS = ("contains_phi", "contains_pii", "uae_data_residency_required")
SYSTEM_FIELDS = ("has_api_or_connector", "ui_change_frequency", "is_legacy_desktop")
FX_TO_USD = dict(load_pricing()["fx_to_usd"])
MAX_CHAT_CHARS = 2_000  # a chat answer is short; longer pastes are truncated so regex scans stay fast

FIELD_LABELS = {
    "minutes_per_run": "minutes per item",
    "system": "system",
    "input_type": "input type",
    "requires_judgment": "needs judgment",
    "rules_documented": "rules written down",
    "exception_rate_pct": "exception rate",
    "has_api_or_connector": "API/connector",
    "ui_change_frequency": "how often screens change",
    "is_legacy_desktop": "legacy desktop app",
    "contains_phi": "health/patient data (PHI)",
    "contains_pii": "personal data (PII)",
    "uae_data_residency_required": "must stay in the UAE",
}

SKIP_RE = re.compile(
    r"^\s*(?:skip|pass|next|don'?t know|do not know|dont know|not sure|no idea|unknown|unsure|idk|n/?a|no clue|"
    r"no clue|can'?t say|dunno|default|use (?:the )?default)\b",
    re.I,
)
YES_RE = re.compile(
    r"^\s*(?:yes|y|yeah|yep|yup|ya|true|correct|sure|definitely|absolutely|of course|it does|they do|we do|"
    r"right|ok(?:ay)?|confirm(?:ed)?|looks good|that'?s right|affirmative|always|mostly)\b",
    re.I,
)
NO_RE = re.compile(r"^\s*(?:no|n|nope|nah|false|never|not really|none|doesn'?t|does not|don'?t|negative|rarely)\b", re.I)
CORRECTION_RE = re.compile(
    r"\b(?:actually|correction|correct that|should be|instead|wrong|not .{1,30} but|rename|change .{1,40} to)\b", re.I
)
# "volume is 5000 a month", "staff cost: 40 per hour": a fact stated at any point, even mid step questions
FACT_STATEMENT_RE = re.compile(
    r"^\s*(?:the\s+|our\s+)?(?:volume|monthly volume|number of (?:items|transactions)|transactions?|items?|"
    r"staff cost|hourly (?:cost|rate)|cost per hour|process name)\s*(?:is|are|=|:|should be|to|of)\b",
    re.I,
)
# "the manager approval happens in Teams": names a step and says where it is done
STEP_SYSTEM_STATEMENT_RE = re.compile(r"\b(?:happens?|is done|are done|done|is performed|takes? place|moves?|runs?|is|are)\s+(?:now\s+)?(?:in|on|using|via|through)\b")
MANUAL_SYSTEM = "Manual (no system)"
MANUAL_RE = re.compile(r"^(?:paper|manual\w*|none|no system|by hand|n/?a|nothing|pen and paper|offline)$", re.I)

STABILITY_SYNONYMS = [
    ("stable", r"stable|rarely|never|hardly|seldom|same|fixed|for years|not often|doesn'?t change|no change"),
    ("changes_quarterly", r"quarter|few times a year|occasional|sometimes|every few months|twice a year|yearly|annual|once a year"),
    ("changes_monthly", r"month|often|frequent|weekly|constantly|all the time|always|every week|a lot"),
]
UI_SYNONYMS = [
    ("rare", r"rare|never|hardly|seldom|stable|for years|not often|almost never|same"),
    ("occasional", r"occasional|sometimes|few times a year|quarter|yearly|annual|every few months|twice a year|once a year|now and then"),
    ("frequent", r"frequent|often|month|weekly|constantly|all the time|a lot|every week|regularly"),
]
INPUT_SYNONYMS = [
    ("semi_structured", r"semi[\s_-]?structured|semi|invoices?|templates?|receipts?|mixed"),
    ("scanned_document", r"scan\w*|pdfs?|fax\w*|handwrit\w*|paper|images?|photos?|scanned[\s_]document"),
    ("voice", r"voice\w*|phone|calls?|audio|recording"),
    ("email", r"e-?mails?|inbox|mailbox|outlook message"),
    ("unstructured", r"unstructured|free[\s_-]?text|notes|letters?|narrative|comments|chat|word documents?"),
    ("structured", r"structured|forms?|spreadsheets?|csv|database|system fields?|fields|excel|tables?|records?|drop-?downs?|typed"),
]


class GuidedInterviewer:
    """Rule-based interviewer: asks for the most important missing fact and parses the answer."""

    def __init__(self, profile: ProcessProfile):
        self.profile = profile
        self.complete = False
        self.summary: str | None = None
        self.assumptions: list[str] = []  # defaults applied after "skip", shown in the summary
        self._started = False
        self._awaiting_confirm = False
        self._pending: tuple[str, str | None] | None = None
        if profile.name or profile.steps:
            # Resuming on a populated profile (sample loaded, LLM interviewer failed): the first message is an
            # answer or a correction, not a fresh description, so earlier values must be overwritable.
            self._started = True
            self._ask_next()  # sets the pending question, or the confirm stage if nothing is missing

    # ---------- public surface ----------

    def greeting(self) -> str:
        return (
            "Hi! I'll ask a few questions about the manual process you want to automate.\n\n"
            "To start, **describe the process in your own words or paste your SOP**: the steps in order, "
            "the systems used, how long each step takes, and how many items you handle a month. "
            "For example: `1. Read the claim email in Outlook (3 min)  2. Key the data into SAP (5 min) ... "
            "about 4,000 claims a month, staff cost AED 90/hour`.\n\n"
            "Please don't paste real patient or customer records — a description is enough. "
            "At any point you can type **skip** if you don't know, **summary** to see progress, "
            "**add step <name>** or **remove step <name>**."
        )

    def resume(self) -> str:
        """Next question after facts were merged from outside the chat (e.g. an imported SOP or diagram).

        UI-only text: it is not added to history, which must stay user/assistant pairs.
        """
        self._started = True
        self.complete = False
        return self._ask_next()

    def run_turn(self, history: list[Message], user_text: str) -> Iterator[AgentEvent]:
        user_text = user_text or ""
        truncated = len(user_text) > MAX_CHAT_CHARS
        if truncated:
            user_text = user_text[:MAX_CHAT_CHARS]
        history.append(Message(role="user", content=user_text))
        out: list[str] = []
        if truncated:
            yield Notice(f"Message truncated to {MAX_CHAT_CHARS:,} characters.")
            out.append(f"(Your message was longer than {MAX_CHAT_CHARS:,} characters, so I only read the first part. "
                       "For a long SOP use the **Paste text** import.)")
        yield from self._handle(user_text, out)
        reply = "\n\n".join(part for part in out if part).strip()
        for line in reply.splitlines(keepends=True):
            yield TextDelta(line)
        history.append(Message(role="assistant", content=reply))

    # ---------- turn handling ----------

    def _handle(self, text: str, out: list[str]) -> Iterator[AgentEvent]:
        t = text.strip()
        low = t.lower()

        if not t:
            out.append(self._ask_next())
            return
        if re.fullmatch(r"(?:show |the )?(?:summary|status|progress|where are we\??|what'?s missing\??)", low):
            out.append(self._summary_text())
            missing = missing_fields(self.profile)
            if missing:
                out.append(f"Still needed: {len(missing)} fact(s).")
            out.append(self._ask_next())
            return
        m = re.match(r"^(?:add|new|insert)\s+(?:a\s+)?step\s*[:\-]?\s*(.+)$", t, re.I | re.S)
        if m:
            yield from self._add_step(m.group(1), out)
            out.append(self._ask_next())
            return
        m = re.match(r"^(?:remove|delete|drop)\s+(?:the\s+)?step\s*[:\-]?\s*(.+)$", t, re.I | re.S)
        if m:
            yield from self._remove_step(m.group(1), out)
            out.append(self._ask_next())
            return
        m = re.match(r"^rename\s+(?:the\s+)?step\s*[:\-]?\s*(.+?)\s+(?:to|as|into)\s+['\"]?(.+?)['\"]?\s*$", t, re.I | re.S)
        if m:
            self._rename_step(m.group(1), m.group(2), out)
            out.append(self._ask_next())
            return

        if not self._started:
            self._started = True
            yield from self._first_message(t, out)
            out.append(self._ask_next())
            return

        if self._awaiting_confirm:
            if YES_RE.match(low) and not re.search(r"\d|but|except|change", low):
                self._finish(out)
                return
            rest = re.sub(r"^\s*(?:no|nope|not quite|not really|wrong)\b[\s,.:;-]*", "", t, flags=re.I)
            if not rest:
                out.append(
                    "No problem — what should I change? For example: `volume is 5000 a month`, "
                    "`step Register claim takes 4 min`, `Outlook has no API`, `no PHI`, `remove step X`, "
                    "or `add step Archive the file in SharePoint, 1 min`."
                )
                return
            changed = yield from self._apply_corrections(rest, out)
            if not changed:
                out.append("I couldn't find a change in that. Try e.g. `volume is 5000 a month` or `step <name> takes 4 min`.")
            out.append(self._ask_next())
            return

        if self.complete:
            changed = yield from self._apply_corrections(t, out)
            if not changed:
                out.append("The interview is complete. Tell me any correction, e.g. `volume is 5000 a month`.")
                return
            out.append(self._ask_next())
            return

        pending = self._pending or self._next_question()
        if pending and pending[0] != "cost" and (cost := parse_hourly_cost(t)):
            # "we pay the clerks $30 an hour" is a staff cost, not an answer to the current question
            yield from self._record(ProfileUpdate(hourly_labor_cost=cost[0], currency=cost[1]), out)
            out.append(f"Recorded staff cost: {cost[0]:g} {cost[1] or self.profile.currency or '(app default currency)'} per hour.")
            t = re.sub(r"[^,;.\n]*\b(?:an|a|per|each|/)\s*hour\b[^,;.\n]*", " ", t, flags=re.I).strip(" ,;.\n")
            low = t.lower()
            if not re.search(r"\w", t):
                out.append(self._ask_next())
                return
        if SKIP_RE.match(low) and pending:
            yield from self._skip(pending, out)
            out.append(self._ask_next())
            return

        mentioned = self._step_mentioned(t)
        if (
            CORRECTION_RE.search(low)
            or FACT_STATEMENT_RE.match(t)
            or (re.match(r"^\s*(?:step|change|set)\s", low) and mentioned)
            or (mentioned and STEP_SYSTEM_STATEMENT_RE.search(low) and find_systems(t))
        ):
            changed = yield from self._apply_corrections(t, out)
            if changed:
                out.append(self._ask_next())
                return

        recorded = False
        if pending:
            recorded = yield from self._answer(pending, t, out)
        extra = False
        if len(t.split()) >= 10 or "\n" in t:
            extra = yield from self._merge_text(t, out, override=False, quiet=recorded)
        if not recorded and not extra:
            yield Notice("Couldn't read an answer from that message.")
            out.append("Sorry, I couldn't pick an answer out of that. Type **skip** to use a default.")
        out.append(self._ask_next())

    def _first_message(self, text: str, out: list[str]) -> Iterator[AgentEvent]:
        result = parse_process_text(text)
        update = result.update
        if update.name is None and self.profile.name is None and not update.steps and _looks_like_name(text):
            update.name = _clean_name(text)
            result.notes.insert(0, f"Process name: '{update.name}'.")
        if not update.model_dump(exclude_none=True):
            out.append("Thanks. I couldn't pick out specific facts yet, so I'll ask for them one by one.")
            return
        yield from self._record(self._only_new(update), out)
        out.append("Thanks — here's what I picked up:\n" + "\n".join(f"- {n}" for n in result.notes))

    def _finish(self, out: list[str]) -> None:
        missing = missing_fields(self.profile)
        if missing:
            self._awaiting_confirm = False
            out.append(self._ask_next())
            return
        p = self.profile
        systems = ", ".join(s.name for s in p.systems) or "no named systems"
        sensitive = [
            label for label, flag in (("PHI", p.data.contains_phi), ("PII", p.data.contains_pii)) if flag
        ]
        self.summary = (
            f"{p.name}: {len(p.steps)} steps taking about {p.total_minutes():g} minutes per item, "
            f"{_g(p.transactions_per_month)} items a month at {p.hourly_labor_cost:g} {p.currency or ''} per staff hour. "
            f"Systems: {systems}. Data: {', '.join(sensitive) or 'no PHI/PII'}"
            + ("; must stay in the UAE." if p.data.uae_data_residency_required else ".")
        ).replace("  ", " ")
        self.complete = True
        self._awaiting_confirm = False
        self._pending = None
        out.append("Great — the interview is complete. You can now run the analysis.")

    # ---------- questions ----------

    def _repair_systems(self) -> None:
        """Give every system named by a step an entry, so its API/screen questions get asked (e.g. after an LLM update)."""
        p = self.profile
        known = {s.name.lower() for s in p.systems}
        for step in p.steps:
            if step.system and step.system.lower() not in known:
                if step.system == MANUAL_SYSTEM:
                    p.systems.append(System(name=MANUAL_SYSTEM, has_api_or_connector=False, ui_change_frequency="rare", is_legacy_desktop=False))
                else:
                    p.systems.append(system_facts(step.system))
                known.add(step.system.lower())

    def _next_question(self) -> tuple[str, str | None] | None:
        p = self.profile
        self._repair_systems()
        if p.name is None:
            return ("name", None)
        if len(p.steps) < 2:
            return ("steps", None)
        if p.transactions_per_month is None:
            return ("volume", None)
        if p.hourly_labor_cost is None:
            return ("cost", None)
        for s in p.steps:
            if any(getattr(s, f) is None for f in STEP_FIELDS):
                return ("step", s.name)
        for sys in p.systems:
            if sys.has_api_or_connector is None or sys.ui_change_frequency is None:
                return ("system", sys.name)
        if any(getattr(p.data, f) is None for f in DATA_FIELDS):
            return ("data", None)
        if p.process_stability is None:
            return ("stability", None)
        return None

    def _ask_next(self) -> str:
        self._pending = self._next_question()
        if self._pending is None:
            if self.complete:
                return ""
            missing = missing_fields(self.profile)
            if missing:  # never ask for confirmation while facts are still missing
                self._awaiting_confirm = False
                return "I still need: " + "; ".join(missing) + ". Tell me, or use `add step <name>` / **skip**."
            self._awaiting_confirm = True
            return (
                "That's everything I need. Here's the summary:\n\n" + self._summary_text() + "\n\n"
                "**Is this right?** Reply **yes** to finish, or tell me what to change "
                "(e.g. `volume is 5000 a month`, `step Register claim takes 4 min`, `remove step X`)."
            )
        self._awaiting_confirm = False
        self.complete = False
        return self._question_text(self._pending)

    def _question_text(self, q: tuple[str, str | None]) -> str:
        kind, target = q
        p = self.profile
        if kind == "name":
            return "**What do you call this process?** (e.g. `Invoice matching`, `Claims intake`)"
        if kind == "steps":
            have = f" I have {len(p.steps)} so far ({', '.join(s.name for s in p.steps)})." if p.steps else ""
            return (
                f"**What are the main steps, in order?**{have} One per line, numbered, or `A, then B, then C`. "
                "Add the time and system if you know them, e.g.\n"
                "`1. Read the claim email in Outlook (3 min)`\n`2. Key the claim into SAP (5 min)`\n"
                "`3. Supervisor approves high-value claims (2 min)`"
            )
        if kind == "volume":
            return (
                "**How many items does this process handle per month?** Rough is fine — e.g. `4000 a month`, "
                "`200 a day`, `50 per week`, `4k`."
            )
        if kind == "cost":
            return (
                "**What's the fully loaded hourly cost of the staff doing this?** Include the currency — "
                "e.g. `AED 90 per hour`, `$35/hour`, `40 EUR`."
            )
        if kind == "stability":
            return (
                "**How often do the process rules or forms change?** Options: **stable** (rarely), "
                "**changes quarterly** (a few times a year), **changes monthly** (often)."
            )
        if kind == "data":
            items = [f for f in DATA_FIELDS if getattr(p.data, f) is None]
            lines = {
                "contains_phi": "Does it include **health or patient data (PHI)** — diagnoses, treatments, medical records? (yes/no)",
                "contains_pii": "Does it include **personal data (PII)** — names, Emirates ID, passport, phone, address? (yes/no)",
                "uae_data_residency_required": "Must the data **stay in the UAE** (data residency)? (yes/no)",
            }
            body = "\n".join(f"{i}. {lines[f]}" for i, f in enumerate(items, 1))
            example = ", ".join(["yes"] * len(items))
            return f"A few questions about the data:\n{body}\n\nAnswer in order, e.g. `{example}`."
        if kind == "system":
            sys = p.system_by_name(target)
            items = [f for f in SYSTEM_FIELDS if sys is not None and getattr(sys, f) is None]
            lines = {
                "has_api_or_connector": "Does it have an **API or Power Automate connector**? (yes/no)",
                "ui_change_frequency": "How often do its **screens change**? (rare / occasional / frequent)",
                "is_legacy_desktop": "Is it a **legacy desktop app** used only through its screens (e.g. Citrix, green screen)? (yes/no)",
            }
            examples = {"has_api_or_connector": "no", "ui_change_frequency": "rare", "is_legacy_desktop": "yes"}
            body = "\n".join(f"- {lines[f]}" for f in items)
            return f"About the system **{target}**:\n{body}\n\nE.g. `{', '.join(examples[f] for f in items)}`."
        if kind == "step":
            step = self._step(target)
            items = [f for f in STEP_FIELDS if step is not None and getattr(step, f) is None]
            idx = p.steps.index(step) + 1 if step else 0
            lines = {
                "minutes_per_run": "how many **minutes** it takes per item (e.g. `4 min`)",
                "system": "which **system** it's done in (e.g. Outlook, SAP, `legacy claims system`, `paper`)",
                "input_type": "what the **input** is: structured (form/system fields), semi-structured (invoices), "
                "unstructured (free text), scanned document (PDF/fax), email, or voice",
                "requires_judgment": "does it need **human judgment**? (yes/no)",
                "rules_documented": "are the **rules written down**? (yes/no)",
                "exception_rate_pct": "what **% of items hit an exception** (e.g. `5%`)",
            }
            examples = {
                "minutes_per_run": "4 min", "system": "SAP", "input_type": "structured",
                "requires_judgment": "no", "rules_documented": "yes", "exception_rate_pct": "5%",
            }
            body = "\n".join(f"- {lines[f]}" for f in items)
            return (
                f"Step {idx} of {len(p.steps)}: **{target}**. Please tell me:\n{body}\n\n"
                f"One line is fine, e.g. `{', '.join(examples[f] for f in items)}`."
            )
        return ""

    # ---------- answers ----------

    def _answer(self, q: tuple[str, str | None], text: str, out: list[str]) -> Iterator[AgentEvent]:
        kind, target = q
        low = text.lower()
        update = ProfileUpdate()
        if kind == "name":
            if len(text.split()) > 12 or "\n" in text:
                changed = yield from self._merge_text(text, out, override=False)
                if self.profile.name is None:
                    yield from self._record(ProfileUpdate(name=_clean_name(text)), out)
                return True if changed or self.profile.name else False
            vol, cost = parse_volume(text), parse_hourly_cost(text)
            if vol or cost:
                # a volume or cost, not a name: keep the fact and ask for the name again
                if vol:
                    update.transactions_per_month = vol[0]
                    out.append(f"Recorded volume: {_g(vol[0])} per month. I still need a name for the process.")
                if cost:
                    update.hourly_labor_cost = cost[0]
                    update.currency = cost[1]
                    out.append(f"Recorded staff cost: {cost[0]:g} {cost[1] or '(app default currency)'} per hour. I still need a name for the process.")
                yield from self._record(update, out)
                return True
            if re.search(r"\d\s*%", text) or len(re.findall(r"\b(?:yes|no)\b", low)) >= 2:
                return False  # step facts, not a name
            update.name = _clean_name(text)
            out.append(f"Recorded process name: {update.name}.")
        elif kind == "steps":
            return (yield from self._answer_steps(text, out))
        elif kind == "volume":
            vol = parse_volume(text)
            if vol:
                update.transactions_per_month = vol[0]
                how = "" if vol[1] == f"{_g(vol[0])} per month" else f" ({vol[1]})"
                out.append(f"Recorded volume: {_g(vol[0])} per month{how}.")
            else:
                n = parse_number(text)
                if n is None:
                    return False
                update.transactions_per_month = n
                out.append(f"Recorded volume: {_g(n)} per month.")
        elif kind == "cost":
            cost = parse_hourly_cost(text)
            amount, cur = cost if cost else (parse_number(text), parse_currency(text))
            if amount is None:
                return False
            update.hourly_labor_cost = amount
            if cur:
                update.currency = cur
            cur_label = cur or self.profile.currency or "(app default currency)"
            out.append(f"Recorded staff cost: {amount:g} {cur_label} per hour.")
        elif kind == "stability":
            value = _match(low, STABILITY_SYNONYMS)
            if value is None:
                return False
            update.process_stability = value
            out.append(f"Recorded: process {value.replace('_', ' ')}.")
        elif kind == "data":
            queue = [f for f in DATA_FIELDS if getattr(self.profile.data, f) is None]
            values, defaults = _parse_data_answer(text, queue)
            if not values:
                return False
            update.data = DataProfile(**values)
            self._note_defaults(defaults, "data", out)
        elif kind == "system":
            sys = self.profile.system_by_name(target)
            if sys is None:
                return False
            queue = [f for f in SYSTEM_FIELDS if getattr(sys, f) is None]
            values, defaults = _parse_system_answer(text, queue)
            if not values:
                return False
            update.systems = [System(name=sys.name, **values)]
            self._note_defaults(defaults, f"system '{sys.name}'", out)
        elif kind == "step":
            step = self._step(target)
            if step is None:
                return False
            queue = [f for f in STEP_FIELDS if getattr(step, f) is None]
            values, defaults = self._parse_step_answer(text, queue)
            if not values:
                return False
            update.steps = [Step(name=step.name, **values)]
            self._note_defaults(defaults, f"step '{step.name}'", out)
        if kind in ("data", "system", "step"):
            out.append("Recorded: " + _describe(update) + ".")
        yield from self._record(update, out)
        return True

    def _answer_steps(self, text: str, out: list[str]) -> Iterator[AgentEvent]:
        result = parse_process_text(text)
        steps = result.update.steps or []
        if len(steps) + len(self.profile.steps) < 2:
            parts = [x for x in re.split(r"\n|;|,|\s+->\s+|\s*→\s*|\s+>\s+", text) if len(x.strip()) > 1]
            if len(parts) >= 2:
                steps = [parse_step(x.strip(), i) for i, x in enumerate(parts, 1)]
        if not steps:
            return False
        update = result.update
        update.steps = steps
        yield from self._record(self._only_new(update), out)
        out.append(f"Recorded {len(steps)} step(s): " + ", ".join(s.name for s in steps) + ".")
        return True

    def _parse_step_answer(self, text: str, queue: list[str], positional: bool = True) -> tuple[dict, list[str]]:
        """Pull step facts out of a (possibly comma-separated) answer. Ambiguous parts fill `queue` in order."""
        values: dict[str, Any] = {}
        defaults: list[str] = []
        queue = list(queue)
        if positional and SKIP_RE.match(text) and len(re.split(r"[,;\n]", text)) == 1:
            for f in queue:
                values[f] = STEP_DEFAULTS[f]
                defaults.append(f)
            return values, defaults

        def take(field: str, value: Any) -> None:
            if field in queue and field not in values:
                values[field] = value
                queue.remove(field)

        for part in [x.strip() for x in re.split(r"[,;\n]|\s+/\s+", text) if x.strip()]:
            low = part.lower()
            found = False
            if positional and SKIP_RE.match(low):
                if queue:
                    f = queue[0]
                    take(f, STEP_DEFAULTS[f])
                    defaults.append(f)
                continue
            pct = re.search(r"(\d+(?:\.\d+)?)\s*(?:%|percent)", low)
            if pct:
                take("exception_rate_pct", min(float(pct.group(1)), 100.0))
                found = True
            elif re.search(r"exception|reject|fail", low):
                n = parse_number(low)
                if n is not None:
                    take("exception_rate_pct", min(n, 100.0))
                    found = True
                elif re.search(r"\bno\b|\bnone\b|never|zero", low):
                    take("exception_rate_pct", 0.0)
                    found = True
            minutes = parse_minutes(low)
            if minutes is not None:
                take("minutes_per_run", minutes)
                found = True
            flag = _flag(low, r"judg\w*|discretion|decision|decide|subjective")
            if flag is not None:
                take("requires_judgment", flag)
                found = True
            if re.search(r"undocumented|unwritten|not (?:written|documented)|no (?:written |documented )?rules", low):
                take("rules_documented", False)
                found = True
            else:
                flag = _flag(low, r"rules?|documented|written|sop|checklist|procedure")
                if flag is not None:
                    take("rules_documented", flag)
                    found = True
            if "system" in queue and MANUAL_RE.match(low):
                take("system", MANUAL_SYSTEM)
                continue
            # "it's in Teams not Outlook": the system before "not" is the new one
            systems = find_systems(re.split(r"\b(?:not|instead of|rather than|no longer)\b", part, maxsplit=1)[0]) or find_systems(part)
            if systems and "system" in queue:
                take("system", self._canonical_system(systems[0]))
                found = True
            input_type = _match(low, INPUT_SYNONYMS) if "input_type" in queue else None
            if input_type and not (systems and re.fullmatch(r"(?:ms |microsoft )?excel", low)):
                take("input_type", input_type)
                found = True
            if found or not positional:
                continue
            # Ambiguous part: fill the next compatible field in the order asked.
            yn = True if YES_RE.match(low) else False if NO_RE.match(low) else None
            number = parse_number(low)
            only_exception = "exception_rate_pct" in queue and not any(f in queue for f in ("requires_judgment", "rules_documented"))
            if only_exception and (level := _exception_level(low)) is not None:
                take("exception_rate_pct", level)  # "none" / "rare" / "often" to "what % hit an exception?"
            elif yn is not None:
                f = next((f for f in queue if f in ("requires_judgment", "rules_documented")), None)
                if f:
                    take(f, yn)
                elif "exception_rate_pct" in queue and yn is False:
                    take("exception_rate_pct", 0.0)  # "no, yes, none": the third answer is the exception rate
            elif number is not None and re.fullmatch(r"[~≈]?\s*(?:about|around|roughly|approx\.?)?\s*[\d.,]+\s*k?", low):
                f = next((f for f in queue if f in ("minutes_per_run", "exception_rate_pct")), None)
                if f:
                    take(f, min(number, 100.0) if f == "exception_rate_pct" else number)
            elif "system" in queue and len(part.split()) <= 5 and _match(low, INPUT_SYNONYMS) is None:
                name = re.sub(r"^(?:in|on|using|via|through|it'?s|its|done in|the)\s+", "", part, flags=re.I).strip(" .")
                name = re.sub(r"^(?:the|our)\s+", "", name, flags=re.I)
                if name:
                    take("system", self._canonical_system(_title(name)))
        return values, defaults

    # ---------- skip / commands / corrections ----------

    def _skip(self, q: tuple[str, str | None], out: list[str]) -> Iterator[AgentEvent]:
        kind, target = q
        p = self.profile
        update = ProfileUpdate()
        if kind == "name":
            update.name = "Unnamed process"
            self._assume("Process name: 'Unnamed process'", out)
        elif kind == "steps":
            out.append("I need at least two steps to analyse the process — even rough ones like `receive request, then update the system`.")
            return
        elif kind == "volume":
            update.transactions_per_month = DEFAULT_VOLUME
            self._assume(f"Volume: {DEFAULT_VOLUME:g} items per month (low estimate, so savings aren't overstated)", out)
        elif kind == "cost":
            cur = p.currency or "USD"
            amount = round(DEFAULT_HOURLY_USD / FX_TO_USD.get(cur, 1.0), 2)
            update.hourly_labor_cost = amount
            update.currency = cur
            self._assume(f"Staff cost: {amount:g} {cur} per hour (modest estimate, so savings aren't overstated)", out)
        elif kind == "stability":
            update.process_stability = DEFAULT_STABILITY
            self._assume("Process changes quarterly (middle option)", out)
        elif kind == "data":
            values = {f: DATA_DEFAULTS[f] for f in DATA_FIELDS if getattr(p.data, f) is None}
            update.data = DataProfile(**values)
            self._note_defaults(list(values), "data", out)
        elif kind == "system":
            sys = p.system_by_name(target)
            values = {f: SYSTEM_DEFAULTS[f] for f in SYSTEM_DEFAULTS if sys and getattr(sys, f) is None}
            update.systems = [System(name=target, **values)]
            self._note_defaults(list(values), f"system '{target}'", out)
        elif kind == "step":
            step = self._step(target)
            values = {f: STEP_DEFAULTS[f] for f in STEP_FIELDS if step and getattr(step, f) is None}
            update.steps = [Step(name=target, **values)]
            self._note_defaults(list(values), f"step '{target}'", out)
        yield Notice("Used a default value for a skipped question.")
        yield from self._record(update, out)

    def _add_step(self, text: str, out: list[str]) -> Iterator[AgentEvent]:
        step = parse_step(text.strip(), len(self.profile.steps) + 1)
        if self._step(step.name):
            out.append(f"There's already a step called '{step.name}' — I updated it instead.")
        yield from self._record(ProfileUpdate(steps=[step]), out)
        out.append(f"Added step '{step.name}'.")

    def _remove_step(self, text: str, out: list[str]) -> Iterator[AgentEvent]:
        step = self._find_step(text.strip())
        if step is None:
            out.append(f"I couldn't find a step called '{text.strip()}'. Steps: " + ", ".join(s.name for s in self.profile.steps))
            return
        yield from self._record(ProfileUpdate(remove_steps=[step.name]), out)
        out.append(f"Removed step '{step.name}'.")

    def _rename_step(self, old: str, new: str, out: list[str]) -> None:
        step = self._find_step(old.strip())
        if step is None:
            out.append(f"I couldn't find a step called '{old.strip()}'. Steps: " + ", ".join(s.name for s in self.profile.steps))
            return
        new_name = _clean_name(new)
        clash = self._step(new_name)
        if clash is not None and clash is not step:
            out.append(f"There's already a step called '{new_name}', so I kept '{step.name}'.")
            return
        old_name, step.name = step.name, new_name
        out.append(f"Renamed step '{old_name}' to '{new_name}'.")

    def _apply_corrections(self, text: str, out: list[str]) -> Iterator[AgentEvent]:
        """Explicit corrections may overwrite earlier answers."""
        low = text.lower()
        update = ProfileUpdate()
        step = self._step_mentioned(text)
        if step:
            rest = re.sub(re.escape(step.name), " ", text, flags=re.I)
            values, _ = self._parse_step_answer(rest, list(STEP_FIELDS), positional=False)
            if values:
                update.steps = [Step(name=step.name, **values)]
        m = re.search(r"(?:process\s+)?(?:name\s+(?:is|should be|to|=|:)|rename(?:\s+it)?\s+to|call it)\s+['\"]?(.+?)['\"]?\s*$", text, re.I)
        if m and not step:
            update.name = _clean_name(m.group(1))
        vol = parse_volume(text)
        if vol is None and re.search(r"volume|per month|a month|items|transactions", low) and not step:
            n = parse_number(re.sub(r"\d+(?:\.\d+)?\s*%", "", low))
            vol = (n, "") if n is not None else None
        if vol:
            update.transactions_per_month = vol[0]
        cost = parse_hourly_cost(text)
        if cost is None and re.search(r"\b(?:cost|hourly|salary|per hour|/hour|labou?r rate|staff rate)\b", low) and not step:
            n = parse_number(low)
            cost = (n, parse_currency(low)) if n is not None else None
        if cost:
            update.hourly_labor_cost = cost[0]
            if cost[1]:
                update.currency = cost[1]
        if re.search(r"change|stable|rules|process", low) and not step:
            value = _match(low, STABILITY_SYNONYMS) if re.search(r"stable|quarter|monthly|every month|rarely change", low) else None
            if value:
                update.process_stability = value
        if re.search(r"phi|pii|patient|health|medical|personal data|uae|residen|stay in", low):
            data, _ = _parse_data_answer(text, list(DATA_FIELDS), positional=False)
            if data:
                update.data = DataProfile(**data)
        if not step:
            parsed = parse_process_text(text).update
            for f in ("error_rate_pct", "process_stability", "department", "industry", "frequency"):
                if getattr(parsed, f) is not None and getattr(update, f) is None:
                    setattr(update, f, getattr(parsed, f))
        systems = []
        mentioned = find_systems(text) + [s.name for s in self.profile.systems if s.name.lower() in low]
        for name in mentioned:
            sys = self.profile.system_by_name(self._canonical_system(name))
            if sys:
                values, _ = _parse_system_answer(text, list(SYSTEM_FIELDS), positional=False)
                if values:
                    systems.append(System(name=sys.name, **values))
        if systems:
            update.systems = systems
        if not update.model_dump(exclude_none=True):
            return (yield from self._merge_text(text, out, override=False))
        yield from self._record(update, out)
        out.append("Updated: " + _describe(update) + ".")
        return True

    def _merge_text(self, text: str, out: list[str], override: bool, quiet: bool = False) -> Iterator[AgentEvent]:
        """Merge facts found anywhere in free text without overwriting earlier answers."""
        result = parse_process_text(text)
        result.update.description = None  # the description comes from the first message only
        update = result.update if override else self._only_new(result.update)
        # A system no step uses would be dropped again as an orphan, so don't report it as noted.
        used = {s.system.lower() for s in self.profile.steps if s.system} | {s.system.lower() for s in update.steps or [] if s.system}
        if update.systems:
            update.systems = [s for s in update.systems if s.name.lower() in used or self.profile.system_by_name(s.name)] or None
        if not update.model_dump(exclude_none=True):
            return False
        yield from self._record(update, out)
        if not quiet:
            out.append("Also noted: " + _describe(update) + ".")
        return True

    # ---------- recording ----------

    def _record(self, update: ProfileUpdate, out: list[str]) -> Iterator[AgentEvent]:
        """Merge an update (plus stub entries for newly mentioned systems) and report it as a tool call."""
        known = {s.name.lower() for s in self.profile.systems} | {s.name.lower() for s in update.systems or []}
        stubs: list[System] = []
        for step in update.steps or []:
            if step.system:
                step.system = self._canonical_system(step.system)
                if step.system.lower() not in known:
                    if step.system == MANUAL_SYSTEM:
                        stubs.append(System(name=MANUAL_SYSTEM, has_api_or_connector=False, ui_change_frequency="rare", is_legacy_desktop=False))
                    elif step.system == STEP_DEFAULTS["system"]:
                        stubs.append(System(name=step.system, **SYSTEM_DEFAULTS))
                    else:
                        stubs.append(system_facts(step.system))
                    known.add(step.system.lower())
        if stubs:
            update.systems = (update.systems or []) + stubs
        if not update.model_dump(exclude_none=True):
            return
        args = update.model_dump(exclude_none=True)
        yield ToolStarted("update_profile", args)
        # ProcessProfile.merge doesn't expect remove_steps among its scalar fields, so apply it here.
        removed = {n.lower() for n in update.remove_steps or []}
        update.remove_steps = None
        self.profile.merge(update)
        if removed:
            self.profile.steps = [s for s in self.profile.steps if s.name.lower() not in removed]
        self._drop_orphan_systems()
        missing = missing_fields(self.profile)
        yield ToolFinished(
            "update_profile",
            json.dumps({"recorded": True, "still_missing": missing, "ready_to_finish": not missing}),
            False,
        )

    def _only_new(self, u: ProfileUpdate) -> ProfileUpdate:
        """Keep only facts the profile doesn't have yet (earlier answers win)."""
        p = self.profile
        out = ProfileUpdate()
        for f in ProfileUpdate.model_fields:
            if f in ("steps", "systems", "data", "remove_steps"):
                continue
            value = getattr(u, f)
            if value is not None and getattr(p, f) is None:
                setattr(out, f, value)
        if u.data:
            d = {k: v for k, v in u.data.model_dump(exclude_none=True).items() if getattr(p.data, k) is None}
            if d:
                out.data = DataProfile(**d)
        steps = []
        for s in u.steps or []:
            existing = self._step(s.name)
            if existing is None:
                steps.append(s)
                continue
            d = {k: v for k, v in s.model_dump(exclude_none=True).items() if k != "name" and getattr(existing, k) is None}
            if d:
                steps.append(Step(name=existing.name, **d))
        out.steps = steps or None
        systems = []
        for s in u.systems or []:
            existing = p.system_by_name(self._canonical_system(s.name))
            if existing is None:
                systems.append(s)
                continue
            d = {k: v for k, v in s.model_dump(exclude_none=True).items() if k != "name" and getattr(existing, k) is None}
            if d:
                systems.append(System(name=existing.name, **d))
        out.systems = systems or None
        return out

    def _drop_orphan_systems(self) -> None:
        """Remove placeholder systems no step uses any more (e.g. after a step was removed or corrected)."""
        used = {s.system.lower() for s in self.profile.steps if s.system}
        self.profile.systems = [
            s for s in self.profile.systems
            if s.name.lower() in used or s.has_api_or_connector is not None and s.ui_change_frequency is not None
        ]

    # ---------- helpers ----------

    def _step(self, name: str | None) -> Step | None:
        if not name:
            return None
        return next((s for s in self.profile.steps if s.name.lower() == name.lower()), None)

    def _find_step(self, text: str) -> Step | None:
        low = text.lower().strip(" '\"")
        steps = self.profile.steps
        exact = self._step(low)
        if exact:
            return exact
        contains = [s for s in steps if low in s.name.lower() or s.name.lower() in low]
        if contains:
            return max(contains, key=lambda s: len(s.name))
        names = [s.name.lower() for s in steps]
        close = difflib.get_close_matches(low, names, n=1, cutoff=0.6)
        if close:
            return steps[names.index(close[0])]
        if re.fullmatch(r"\d+", low) and 1 <= int(low) <= len(steps):
            return steps[int(low) - 1]
        return None

    def _step_mentioned(self, text: str) -> Step | None:
        low = text.lower()
        hits = [s for s in self.profile.steps if s.name.lower() in low]
        if hits:
            return max(hits, key=lambda s: len(s.name))
        m = re.search(r"\bstep\s+(\d+|['\"]?[\w -]{3,40}?['\"]?)(?=\s+(?:takes|is|should|needs|has|uses|in|on|:|-)|$)", text, re.I)
        found = self._find_step(m.group(1)) if m else None
        if found:
            return found
        # Loose wording: "the manager approval" matches the step "Manager approves" (same word stems).
        text_stems = _stems(low)
        loose = [s for s in self.profile.steps if (st := _stems(s.name.lower())) and len(st) >= 2 and st <= text_stems]
        return max(loose, key=lambda s: len(s.name)) if loose else None

    def _canonical_system(self, name: str) -> str:
        if MANUAL_RE.match(name.strip()):
            return MANUAL_SYSTEM
        existing = self.profile.system_by_name(name)
        if existing:
            return existing.name
        found = find_systems(name)
        if found:
            existing = self.profile.system_by_name(found[0])
            return existing.name if existing else found[0]
        return name

    def _assume(self, text: str, out: list[str]) -> None:
        self.assumptions.append(text)
        out.append(f"OK — I'll assume: {text}. You can correct it later.")

    def _note_defaults(self, fields: list[str], where: str, out: list[str]) -> None:
        if not fields:
            return
        source = {**STEP_DEFAULTS, **SYSTEM_DEFAULTS, **DATA_DEFAULTS}
        parts = [f"{FIELD_LABELS[f]} = {_fmt(source[f])}" for f in fields]
        self._assume(f"for {where}: " + ", ".join(parts) + " (conservative default)", out)

    def _summary_text(self) -> str:
        p = self.profile
        cur = p.currency or "(default currency)"
        lines = [
            f"**{p.name or 'Unnamed process'}**"
            + (f" — {_g(p.transactions_per_month)} items/month" if p.transactions_per_month is not None else "")
            + (f", {p.hourly_labor_cost:g} {cur}/hour" if p.hourly_labor_cost is not None else "")
            + (f", process {p.process_stability.replace('_', ' ')}" if p.process_stability else ""),
        ]
        if p.steps:
            lines.append("Steps:")
            for i, s in enumerate(p.steps, 1):
                bits = [
                    f"{s.minutes_per_run:g} min" if s.minutes_per_run is not None else "? min",
                    s.system or "? system",
                    (s.input_type or "? input").replace("_", " "),
                    f"judgment: {_fmt(s.requires_judgment)}",
                    f"rules written: {_fmt(s.rules_documented)}",
                    f"exceptions {s.exception_rate_pct:g}%" if s.exception_rate_pct is not None else "exceptions ?",
                ]
                if s.is_approval:
                    bits.append("approval")
                lines.append(f"{i}. {s.name} — " + ", ".join(bits))
        if p.systems:
            lines.append(
                "Systems: "
                + "; ".join(
                    f"{s.name} (API/connector: {_fmt(s.has_api_or_connector)}, screens change: {s.ui_change_frequency or '?'}"
                    + (", legacy desktop" if s.is_legacy_desktop else "")
                    + ")"
                    for s in p.systems
                )
            )
        d = p.data
        lines.append(
            f"Data: PHI {_fmt(d.contains_phi)}, PII {_fmt(d.contains_pii)}, must stay in UAE {_fmt(d.uae_data_residency_required)}"
        )
        if self.assumptions:
            lines.append("Assumed defaults: " + "; ".join(self.assumptions))
        return "\n".join(lines)


# ---------- answer parsers (module level so they're easy to test) ----------

def _parse_data_answer(text: str, queue: list[str], positional: bool = True) -> tuple[dict, list[str]]:
    values: dict[str, Any] = {}
    defaults: list[str] = []
    queue = list(queue)
    low = text.lower().strip()
    if positional and re.match(r"^(?:all )?(?:yes|yep|yeah)(?: to all| all| for all| to everything)?\.?$|^all (?:of them|three)$", low) and len(queue) > 1 and re.search(r"all|every", low):
        return {f: True for f in queue}, []
    if positional and re.match(r"^(?:no to all|all no|none(?: of (?:them|these|the above))?|no,? none)\.?$", low):
        return {f: False for f in queue}, []
    if positional and SKIP_RE.match(low) and len(re.split(r"[,;\n]", low)) == 1:
        return {f: DATA_DEFAULTS[f] for f in queue}, list(queue)

    def take(field: str, value: bool) -> None:
        if field in queue and field not in values:
            values[field] = value
            queue.remove(field)

    keywords = {
        "contains_phi": r"phi|health\w*|patients?|medical|clinical|diagnos\w*",
        "contains_pii": r"pii|personal|names?|emirates id|passports?|ids?\b|phone|address\w*|identif\w*",
        "uae_data_residency_required": r"uae|residen\w*|stay|country|onshore|local\w*|abroad|outside|leave",
    }
    for part in [x.strip() for x in re.split(r"[,;\n]|\band\b|\d\s*[.)]\s+", low) if x.strip()]:
        found = False
        for field, pattern in keywords.items():
            flag = _flag(part, pattern)
            if flag is None:
                continue
            if field == "uae_data_residency_required" and re.search(r"abroad|outside|leave", part):
                flag = not flag if re.search(r"\b(?:can'?t|cannot|must not|mustn'?t|not allowed)\b", part) is None else True
            take(field, flag)
            found = True
        if found or not positional:
            continue
        if SKIP_RE.match(part) and queue:
            f = queue[0]
            take(f, DATA_DEFAULTS[f])
            defaults.append(f)
        elif YES_RE.match(part) and queue:
            take(queue[0], True)
        elif NO_RE.match(part) and queue:
            take(queue[0], False)
    return values, defaults


def _parse_system_answer(text: str, queue: list[str], positional: bool = True) -> tuple[dict, list[str]]:
    values: dict[str, Any] = {}
    defaults: list[str] = []
    queue = list(queue)
    if positional and SKIP_RE.match(text) and len(re.split(r"[,;\n]", text)) == 1:
        values = {f: SYSTEM_DEFAULTS[f] for f in queue if f in SYSTEM_DEFAULTS}
        return values, list(values)

    def take(field: str, value: Any) -> None:
        if field in queue and field not in values:
            values[field] = value
            queue.remove(field)

    for part in [x.strip() for x in re.split(r"[,;\n]|\band\b", text.lower()) if x.strip()]:
        found = False
        flag = _flag(part, r"api|connector|integration|web service")
        if flag is not None:
            take("has_api_or_connector", flag)
            found = True
        flag = _flag(part, r"desktop|legacy|citrix|green[\s-]screen|mainframe|thick client")
        if flag is not None:
            take("is_legacy_desktop", flag)
            found = True
        if "ui_change_frequency" in queue and (re.search(r"screen|\bui\b|interface|change", part) or (positional and not found)):
            ui = _match(part, UI_SYNONYMS)
            if ui:
                take("ui_change_frequency", ui)
                found = True
        if found or not positional:
            continue
        if SKIP_RE.match(part) and queue:
            f = queue[0]
            if f in SYSTEM_DEFAULTS:
                take(f, SYSTEM_DEFAULTS[f])
                defaults.append(f)
            else:
                queue.pop(0)
        elif YES_RE.match(part) or NO_RE.match(part):
            f = next((f for f in queue if f in ("has_api_or_connector", "is_legacy_desktop")), None)
            if f:
                take(f, bool(YES_RE.match(part)))
    return values, defaults


def _flag(text: str, pattern: str) -> bool | None:
    """Yes/no about a keyword: None if absent; False if 'no'/'not'/negated; True otherwise."""
    m = re.search(rf"\b(?:{pattern})", text)
    if not m:
        return None
    if re.search(r"^\s*(?:yes|yeah|yep|y)\b|\b(?:yes|yeah|yep)\s*$|[:=-]\s*(?:yes|y)\b", text):
        return True
    if re.search(
        r"^\s*(?:no|nope|n)\b|\b(?:no|nope)\s*$|[:=-]\s*(?:no|n)\b|\b(?:not|no|non|without|never|none|isn'?t|doesn'?t|don'?t|aren'?t|lacks?)\b|n't\b|\bun(?=documented|written)",
        text,
    ):
        return False
    return True


def _match(text: str, synonyms: list[tuple[str, str]]) -> str | None:
    """Fuzzy enum match: exact option name first, then synonyms in priority order."""
    low = text.lower().strip()
    for value, _ in synonyms:
        if re.search(rf"\b{value.replace('_', '[ _-]?')}\b", low):
            return value
    for value, pattern in synonyms:
        if re.search(rf"\b(?:{pattern})", low):
            return value
    guess = input_type_of(low) if synonyms is INPUT_SYNONYMS else None
    return guess


_NOT_A_NAME_START = {
    "i", "i'm", "im", "i'd", "we", "you", "can", "could", "would", "will", "please", "how", "what", "why", "when",
    "where", "who", "which", "hi", "hello", "hey", "hiya", "yo", "thanks", "thank", "help", "ok", "okay", "yes", "no",
    "test", "testing", "my", "our", "let", "let's", "lets", "it", "it's", "this", "that", "there", "to", "need",
    "want", "start", "begin", "good", "greetings", "sure", "so", "well",
}
_SENTENCE_VERB_RE = re.compile(
    r"\b(?:want|need|wish|like to|trying|try|going to|help|automate|automating|handle|handles|receive|receives|get|gets|"
    r"do|does|is|are|was|were|have|has|process(?:es)? \w+ \w+)\b",
    re.I,
)


def _looks_like_name(text: str) -> bool:
    """A short noun phrase ('Vendor onboarding') can be the process name; greetings, questions and sentences cannot."""
    t = text.strip()
    words = t.split()
    if not 1 <= len(words) <= 8 or re.search(r"[?!]|\d", t):
        return False
    if words[0].lower().strip(",.") in _NOT_A_NAME_START:
        return False
    return not _SENTENCE_VERB_RE.search(t)


# Words for "what % of items hit an exception?" -> a rate, echoed back in the "Recorded:" line
EXCEPTION_LEVELS = [
    (r"never|none|zero|no exceptions?|nothing", 0.0),
    (r"rare(?:ly)?|hardly|seldom|almost never|very few|few", 2.0),
    (r"sometimes|occasional(?:ly)?|now and then|some", 10.0),
    (r"often|frequent(?:ly)?|a lot|many|regularly|common", 25.0),
]


def _exception_level(text: str) -> float | None:
    for pattern, value in EXCEPTION_LEVELS:
        if re.fullmatch(rf"\s*(?:{pattern})[\s.!]*", text):
            return value
    return None


def _stems(text: str) -> set[str]:
    """First five letters of every word of 4+ letters: 'approval' and 'approves' both give 'appro'."""
    return {w[:5] for w in re.findall(r"[a-z]+", text) if len(w) >= 4}


def _clean_name(text: str) -> str:
    t = re.sub(r"^\s*(?:it'?s|it is|we call it|the process is|this is|process name is|name is|called|the|our)\s+", "", text.strip(), flags=re.I)
    t = re.sub(r"^\s*(?:called|the|our)\s+", "", t, flags=re.I)
    t = re.split(r"[.\n!?]", t)[0].strip(" '\"`*:-")
    words = t.split()
    t = " ".join(words[:8])
    return (t[:1].upper() + t[1:])[:80] or "Unnamed process"


def _title(s: str) -> str:
    return " ".join(w if w[:1].isupper() or w.isupper() else w.capitalize() for w in s.split())


def _fmt(value: Any) -> str:
    if value is True:
        return "yes"
    if value is False:
        return "no"
    if value is None:
        return "?"
    if isinstance(value, float):
        return f"{value:g}"
    return str(value).replace("_", " ")


def _describe(u: ProfileUpdate) -> str:
    bits = []
    for key, value in u.model_dump(exclude_none=True, exclude={"steps", "systems", "data", "remove_steps", "description"}).items():
        bits.append(f"{key.replace('_', ' ')} = {_fmt(value)}")
    if u.data:
        bits += [f"{FIELD_LABELS[k]} = {_fmt(v)}" for k, v in u.data.model_dump(exclude_none=True).items() if k in FIELD_LABELS]
    for s in u.steps or []:
        facts = [f"{FIELD_LABELS.get(k, k)} {_fmt(v)}" for k, v in s.model_dump(exclude_none=True).items() if k in FIELD_LABELS]
        bits.append(f"step '{s.name}'" + (f" ({', '.join(facts)})" if facts else ""))
    for s in u.systems or []:
        facts = [f"{FIELD_LABELS[k]} {_fmt(v)}" for k, v in s.model_dump(exclude_none=True).items() if k in FIELD_LABELS]
        bits.append(f"system '{s.name}'" + (f" ({', '.join(facts)})" if facts else ""))
    return "; ".join(bits) or "nothing new"
