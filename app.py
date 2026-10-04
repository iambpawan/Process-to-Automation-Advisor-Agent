"""Streamlit web UI for the Process-to-Automation Advisor.

Works without an LLM key (guided interviewer + template writer) and switches to the LLM
Interviewer and Writer automatically once LLM_API_KEY is set in .env.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path

import streamlit as st

from advisor import ui
from advisor.agents.base import Notice, ToolFinished, ToolStarted
from advisor.agents.guided_interviewer import GuidedInterviewer
from advisor.agents.interviewer import InterviewerAgent
from advisor.analysis import load_pricing
from advisor.config import load_settings
from advisor.importers import ImportResult, voice
from advisor.importers.bpmn import parse_bpmn
from advisor.importers.text import MAX_TEXT_CHARS, parse_process_text
from advisor.importers.vsdx import parse_vsdx
from advisor.integrations import load_flows, load_license_csv
from advisor.live import DEFAULT_CACHE_PATH, refresh_pricing
from advisor.llm import get_provider
from advisor.llm.availability import llm_available
from advisor.llm.base import ProviderError, TextDelta
from advisor.models import ProcessProfile, missing_fields
from advisor.orchestrator import Orchestrator, runtime_data_region
from advisor.report import (
    ai_decision,
    compliance_section,
    cost_table,
    delivery_section,
    existing_automation,
    fit_score,
    process_table,
    to_docx,
)

SAMPLE = Path(__file__).parent / "advisor" / "samples" / "claims_intake.json"
LLM_GREETING = (
    "Hi! I'll ask a few questions about the manual process you want to automate — what happens, how often, "
    "which systems and data are involved, and what it costs today. Please describe the process rather than "
    "pasting real patient or customer records. What process should we look at?"
)
OFFLINE_BANNER = "Offline mode — add LLM_API_KEY to .env to enable the AI interviewer and writer"
LIST_PRICE_NOTE = (
    "Azure prices are public pay-as-you-go list prices, not negotiated or EA rates. "
    "All other prices are the static planning assumptions in pricing_assumptions.json."
)
TAGLINE = (
    "Talk through a manual process. We score how well it automates, decide whether rules, AI or both are "
    "needed, compare the cost and write the design document. Every figure is computed in Python."
)
CHAT_PLACEHOLDER = "Describe the process, answer the question, or attach an SOP or diagram"
AVATARS = {"user": ":material/person:", "assistant": ":material/auto_awesome:"}
# Only the formats the importers actually understand: SOP text, BPMN/Visio diagrams, audio.
DOC_FILE_TYPES = ["txt", "md", "bpmn", "xml", "vsdx"]
AUDIO_FILE_TYPES = ["wav", "mp3", "m4a", "ogg", "webm"]
AUDIO_SUFFIXES = {f".{t}" for t in AUDIO_FILE_TYPES}
METRIC_HELP = {
    "fit": "How well this process suits automation, out of 100: volume, time saved, rule clarity, system access and stability.",
    "approach": "Rules-only, Hybrid or AI-first, from the judgement, input type and exception signals in your steps.",
    "payback": "How long the cheaper scenario takes to pay back its build and licence cost out of the labour it saves.",
    "effort": "Build size and working days for the recommended scenario, from the per-step effort assumptions.",
}

st.set_page_config(page_title="Automation Advisor", page_icon=":material/account_tree:", layout="wide")
st.logo(str(Path(__file__).parent / "advisor" / "assets" / "logo.svg"), size="large")
st.markdown(ui.CSS, unsafe_allow_html=True)  # layout only; colour and type come from .streamlit/config.toml
settings = load_settings()
LLM_OK, LLM_REASON = llm_available(settings)
CACHE_PATH = Path(settings.live_cache_path) if settings.live_cache_path else DEFAULT_CACHE_PATH


@st.cache_resource
def provider():
    return get_provider(settings)


state = st.session_state


def use_llm() -> bool:
    """LLM agents are used when a key is configured and the LLM hasn't failed this session."""
    return LLM_OK and not state.get("llm_error")


def greeting() -> str:
    return LLM_GREETING if use_llm() else GuidedInterviewer(ProcessProfile()).greeting()


def clear_results() -> None:
    """Drop the analysis and document: they only ever describe one version of the profile."""
    state.assessment = None
    state.document = None
    state.analysis_error = None


def reset(profile: ProcessProfile | None = None) -> None:
    state.profile = profile or ProcessProfile()
    state.history = []
    state.log = [{"role": "assistant", "text": greeting(), "tools": []}]
    state.interviewer = None
    state.document_by = None
    state.pending_import = None
    state.import_context = None
    state.import_error = None
    state.import_note = None
    clear_results()


def startup_pricing() -> tuple[dict, list[str]]:
    """Static assumptions, overlaid with the live-data cache when it is fresh. Never touches the network."""
    base = load_pricing()
    if not CACHE_PATH.exists():
        return base, ["No live data fetched yet: using the static assumptions."]
    try:
        pricing, notes = refresh_pricing(base, region=settings.live_region, cache_path=CACHE_PATH, allow_network=False)
    except Exception as e:  # a damaged cache file must never stop the app from starting
        return base, [f"The live data cache could not be used ({type(e).__name__}): using the static assumptions."]
    live = pricing.get("_live", {})
    if "cache" in (live.get("fx_status"), live.get("azure_status")):  # at least one section is fresh
        return pricing, notes
    return base, ["The live data cache is more than a day old: using the static assumptions. Refresh to update."]


def interviewer():
    """Guided (keyless) interviewer, or the LLM one when available. Both share the profile and history."""
    if state.interviewer is None or (isinstance(state.interviewer, InterviewerAgent) and not use_llm()):
        state.interviewer = InterviewerAgent(provider(), state.profile) if use_llm() else GuidedInterviewer(state.profile)
    state.interviewer.profile = state.profile
    return state.interviewer


def interview_turn(prompt: str, box) -> tuple[str, list[str]]:
    """Run one interview turn, streaming into `box`. Falls back to the guided interviewer if the LLM fails."""
    agent = interviewer()
    sent = prompt
    if state.import_context and isinstance(agent, InterviewerAgent):
        sent = f"{state.import_context}\n\n{prompt}"
    state.import_context = None
    mark = len(state.history)
    text, tools = "", []
    try:
        for event in agent.run_turn(state.history, sent):
            if isinstance(event, TextDelta):
                text += event.text
                box.markdown(text + "▌")
            elif isinstance(event, ToolStarted):
                tools.append("Recording details…" if event.name == "update_profile" else "Finishing interview…")
            elif isinstance(event, ToolFinished) and event.is_error:
                tools.append(f"{event.name}: {event.result[:200]}")
            elif isinstance(event, Notice):
                tools.append(event.text)
    except (ProviderError, NotImplementedError) as e:
        if not isinstance(agent, InterviewerAgent):
            raise
        del state.history[mark:]  # drop the half-finished LLM turn so history stays valid
        state.llm_error = str(e)
        state.interviewer = None
        text, more = interview_turn(prompt, box)
        return text, [f"The AI interviewer failed ({e}). Switched to offline mode for this session."] + more
    return text, tools


def set_pending(result: ImportResult, source: str, transcript: str | None = None) -> None:
    state.pending_import = {"result": result, "source": source, "transcript": transcript}


def merge_import() -> None:
    """Merge the previewed import into the profile, then let the interviewer ask only what's missing."""
    pending = state.pending_import
    result, source = pending["result"], pending["source"]
    update = result.update.model_copy(deep=True)
    msg = f"Merged the {source} into the profile."
    if state.profile.name and update.name and update.name != state.profile.name:
        msg += f" Kept the name **{state.profile.name}** (the import suggested '{update.name}')."
        update.name = None
    state.profile.merge(update)
    clear_results()
    state.pending_import = None
    agent = interviewer()
    if isinstance(agent, GuidedInterviewer):
        msg += "\n\n" + agent.resume()
    else:
        missing = missing_fields(state.profile)
        state.import_context = (
            f"[The user imported a {source}; the profile now holds {len(state.profile.steps)} steps. "
            f"Import notes: {'; '.join(result.notes)}. Still missing: {missing}. "
            "Continue the interview from there.]"
        )
        msg += " Send a message to continue the interview."
    state.log.append({"role": "assistant", "text": msg, "tools": []})


def preview_import() -> None:
    pending = state.pending_import
    result, source = pending["result"], pending["source"]
    st.markdown(f"**Preview — {source}**")
    st.caption("Nothing is added to the profile until you merge it.")
    if pending.get("transcript"):
        st.caption(f"Transcript: {pending['transcript']}")
    u = result.update
    if u.name:
        st.markdown(f"Process name: **{u.name}**")
    if u.steps:
        rows = [
            {"Step": s.name, "Min": s.minutes_per_run, "System": s.system, "Input": s.input_type,
             "Approval": s.is_approval}
            for s in u.steps
        ]
        st.dataframe(rows, hide_index=True, width="stretch")
    if result.notes:
        st.markdown("\n".join(f"- {n}" for n in result.notes))
    st.json(u.model_dump(exclude_none=True), expanded=False)
    c1, c2 = st.columns(2)
    if c1.button("Merge into profile", type="primary", width="stretch"):
        merge_import()
        st.rerun()
    if c2.button("Discard import", width="stretch"):
        state.pending_import = None
        st.rerun()


def _import_attachment(name: str, data: bytes, is_audio: bool) -> tuple[ImportResult, str, str | None]:
    """Pick the importer for one attachment. Raises ValueError (or VoiceUnavailable) on bad input."""
    if is_audio:
        transcript = voice.transcribe(data, name)
        if not transcript.strip():
            raise ValueError("Nothing could be heard in that recording. Try again, or type the description instead.")
        return parse_process_text(transcript), "voice recording", transcript
    suffix = Path(name).suffix.lower()
    if suffix == ".vsdx":
        return parse_vsdx(data), f"diagram {name}", None
    if suffix in (".bpmn", ".xml"):
        return parse_bpmn(data), f"diagram {name}", None
    text = data.decode("utf-8", errors="replace")
    if not text.strip():
        raise ValueError(f"{name} holds no text.")
    return parse_process_text(text[:MAX_TEXT_CHARS]), f"document {name}", None


def read_attachment(file) -> None:
    """Route one chat attachment (SOP, diagram or recording) into the import preview."""
    name = getattr(file, "name", None) or "attachment"
    is_audio = Path(name).suffix.lower() in AUDIO_SUFFIXES or (getattr(file, "type", "") or "").startswith("audio")
    try:
        set_pending(*_import_attachment(name, file.getvalue(), is_audio))
    except voice.VoiceUnavailable as e:
        state.import_error = str(e)
    except ValueError as e:
        state.import_error = str(e)
    except Exception as e:  # damaged archives, decoding and speech-model errors
        state.import_error = f"{name} could not be read ({type(e).__name__}): {e}"


def submission(value) -> tuple[str, list]:
    """Split a chat submission: a plain str without attachments, a ChatInputValue with them."""
    if isinstance(value, str):
        return value, []
    return (getattr(value, "text", "") or ""), list(getattr(value, "files", None) or [])


def uploads_key(files) -> tuple:
    return tuple((f.name, f.size, getattr(f, "file_id", "")) for f in files)


def current_pricing() -> dict:
    """Session pricing (static or live) with the runtime model the user picked."""
    pricing = copy.deepcopy(state.pricing)
    model = state.get("runtime_model")
    if model in pricing["ai"]["models_per_mtok"]:
        pricing["ai"]["runtime_model"] = model
    return pricing


def run_analysis() -> None:
    """Analyse the profile now. A rejected profile is remembered so it isn't retried on every rerun."""
    try:
        state.assessment = Orchestrator(settings, current_pricing()).analyze(
            state.profile, flows=state.flows, inventory=state.inventory
        )
        state.document = None
        state.analysis_error = None
    except ValueError as e:
        state.assessment = None
        state.analysis_error = str(e)


if "pricing" not in state:
    state.pricing, state.live_notes = startup_pricing()
if "flows" not in state:
    state.flows, state.flow_notes, state.flows_key = [], [], ()
    state.inventory, state.license_notes, state.license_key = None, [], None
if "profile" not in state:
    reset()

# ---------- Sidebar ----------
with st.sidebar:
    # The logo sits above this, so the heading carries the live process name instead of the app name.
    st.markdown(f"**{state.profile.name or 'New assessment'}**")
    if use_llm():
        st.caption(f"AI agents: **on** · {settings.provider} `{settings.model}` · LLM data region: {settings.llm_data_region}")
    else:
        st.caption("AI agents: **offline** · guided interviewer + template writer (no API key needed)")
    # Stacked rather than side by side: the labels truncate to "New assess..." in two sidebar columns.
    c1 = c2 = st
    if c1.button("New assessment", width="stretch", icon=":material/restart_alt:", help=ui.FIELD_HELP["new_assessment"]):
        reset()
        st.rerun()
    if c2.button("Load sample", width="stretch", icon=":material/science:", help=ui.FIELD_HELP["load_sample"]):
        reset(ProcessProfile.model_validate_json(SAMPLE.read_text(encoding="utf-8")))
        state.log.append(
            {"role": "assistant", "text": "Loaded the sample claims-intake process. The analysis is below.", "tools": []}
        )
        st.rerun()
    if LLM_OK and state.get("llm_error") and st.button("Retry AI agents", width="stretch"):
        state.llm_error = None
        state.interviewer = None
        st.rerun()

    # What the interview still needs, in the user's words rather than field names.
    ready = ui.readiness(state.profile)
    st.subheader("Progress" if not ready.complete else "Ready to analyse", help=ui.FIELD_HELP["readiness"])
    st.progress(ready.fraction)
    st.caption(
        f"{ready.done} of {ready.total} answered · {ready.percent}%"
        + ("" if ready.complete else " — answer in the chat, or type `skip` for a documented default")
    )
    for group in ready.groups:
        if group.total == 0:
            value = "Nothing needed yet"
        elif group.complete:
            value = "Complete"
        else:
            value = f"{group.done} of {group.total}"
        # A compact row per group: four bordered metric cards would push the checklist off-screen.
        mark = ":material/check_circle:" if group.complete else ":material/radio_button_unchecked:"
        st.markdown(f"{mark} **{group.title}** — {value}", help=group.help)
        if group.missing:
            shown = group.missing[:6]
            st.caption(
                "\n".join(f"- {m}" for m in shown)
                + (f"\n- … and {len(group.missing) - len(shown)} more" if len(group.missing) > len(shown) else "")
            )
    with st.expander("Process profile (JSON)"):
        st.caption(ui.FIELD_HELP["profile_json"])
        st.code(json.dumps(state.profile.model_dump(exclude_none=True), indent=2), language="json")

    # Live public data: only fetched on click; a fresh cache is loaded at startup.
    st.subheader("Live data", help=ui.FIELD_HELP["live_data"])
    live = state.pricing.get("_live")
    if live:
        st.caption(f"Prices & FX: **{live['status']}** · fetched {live['fetched_at'] or '—'} (UTC)")
        if live.get("sources"):
            st.caption("Sources: " + "; ".join(live["sources"]))
    else:
        st.caption("Prices & FX: **static assumptions**")
    if st.button("Refresh live prices & FX", width="stretch", help=ui.FIELD_HELP["live_refresh"]):
        with st.spinner("Fetching public FX rates and Azure list prices…"):
            try:
                state.pricing, state.live_notes = refresh_pricing(
                    load_pricing(), region=settings.live_region, cache_path=CACHE_PATH, force=True  # an explicit click always fetches
                )
            except Exception as e:  # e.g. a damaged cache file: keep the current prices
                state.live_notes = [f"Live refresh failed ({type(e).__name__}): kept the current prices."]
        clear_results()
        st.rerun()
    with st.expander("Data notes"):
        st.markdown("\n".join(f"- {n}" for n in state.live_notes + [LIST_PRICE_NOTE]))
    models = list(state.pricing["ai"]["models_per_mtok"])
    default_model = state.pricing["ai"]["runtime_model"]
    if state.get("runtime_model") not in models:
        state.runtime_model = default_model if default_model in models else models[0]
    st.selectbox(
        "AI runtime model (for cost estimates)", models, key="runtime_model", help=ui.FIELD_HELP["runtime_model"],
        on_change=clear_results,  # the model changes the data region and the result
    )
    st.caption(f"Data region for this model: {runtime_data_region(state.runtime_model, settings.llm_data_region)}")

    # Tenant inventory: files the user exports themselves (no credentials).
    st.subheader("Tenant inventory", help="Optional. Exports you make yourself — no credentials and nothing is sent to Microsoft.")
    flow_files = st.file_uploader(
        "Power Automate exports (.zip / .json)", type=["zip", "json"], accept_multiple_files=True, key="flow_files",
        help=ui.FIELD_HELP["flow_files"],
    )
    if uploads_key(flow_files or []) != state.flows_key:
        state.flows_key = uploads_key(flow_files or [])
        try:
            state.flows, state.flow_notes = load_flows([(f.name, f.getvalue()) for f in flow_files]) if flow_files else ([], [])
        except Exception as e:  # untrusted uploads must produce a note, never a traceback
            state.flows, state.flow_notes = [], [f"The flow files could not be read ({type(e).__name__})."]
        clear_results()
    license_file = st.file_uploader(
        "Licence inventory (.csv)", type=["csv"], key="license_file", help=ui.FIELD_HELP["license_file"]
    )
    lic_key = uploads_key([license_file]) if license_file else None
    if lic_key != state.license_key:
        state.license_key = lic_key
        try:
            state.inventory, state.license_notes = load_license_csv(license_file.getvalue()) if license_file else (None, [])
        except Exception as e:
            state.inventory, state.license_notes = None, [f"The licence file could not be read ({type(e).__name__})."]
        clear_results()
    if state.flows or state.inventory:
        inv = state.inventory
        st.caption(
            f"{len(state.flows)} existing flow(s) loaded"
            + (f" · spare licences: {inv.premium_available} Premium, {inv.process_available} Process, "
               f"{inv.ai_builder_credits_available:,} AI Builder credits" if inv else "")
        )
    if state.flow_notes or state.license_notes:
        with st.expander("Inventory notes"):
            st.markdown("\n".join(f"- {n}" for n in state.flow_notes + state.license_notes))

# ---------- Conversation ----------
st.title("Process-to-Automation Advisor")
st.caption(TAGLINE)
if not LLM_OK:
    st.info(f"{OFFLINE_BANNER}  \n{LLM_REASON}", icon=":material/cloud_off:")
elif state.get("llm_error"):
    st.warning(
        f"The LLM failed: {state.llm_error} Running in offline mode for this session (guided interviewer + "
        "template writer). Fix the key in .env and restart the app, or click **Retry AI agents** after a network problem.",
        icon=":material/warning:",
    )

# Open while the conversation is still just the greeting, so a first-time user is oriented.
with st.expander("How this works — what to tell it, and what you get back", expanded=len(state.log) <= 1, icon=":material/help:"):
    g1, g2, g3 = st.columns(3)
    with g1.container(border=True):
        st.markdown("**1 · Describe the process**")
        st.caption(
            "Talk through a manual process in the chat, one answer at a time: the steps people follow, how often "
            "it runs, which systems they touch, and what an hour of their time costs. Type `skip` for anything you "
            "do not know and a documented default is used instead."
        )
    with g2.container(border=True):
        st.markdown("**2 · Or bring what you already have**")
        st.caption(
            "Attach an SOP, a BPMN or Visio diagram, or a voice note with the **+** and microphone buttons in the "
            "chat bar. Everything extracted is shown for you to check, and nothing is merged until you approve it."
        )
    with g3.container(border=True):
        st.markdown("**3 · Get the assessment**")
        st.caption(
            "Once every fact is in, you get an automation fit score, a Rules-only / Hybrid / AI-first "
            "recommendation, a cost and payback comparison, PHI/PII and UAE residency checks, and a design "
            "document you can download as Word or Markdown."
        )
    st.caption(
        "Every score and cost is computed in Python, never written by the model. Prices are planning assumptions — "
        "validate them before quoting."
        + ("" if voice.is_available() else f"  \n{voice.INSTALL_HINT}")
    )
    st.divider()
    st.markdown("**Paste a long SOP or work instruction**")
    st.caption(ui.FIELD_HELP["import_text"])
    text = st.text_area("SOP or process description", key="import_text", height=160, max_chars=MAX_TEXT_CHARS)
    if st.button("Read text", icon=":material/article:"):
        if text.strip():
            set_pending(parse_process_text(text), "pasted text")
            st.rerun()
        else:
            st.warning("Paste some text first.")

for turn, entry in enumerate(state.log):
    # The key puts an st-key-turn-<role>-N class on the wrapper, which is how the stylesheet tells the
    # two sides apart: a custom avatar makes Streamlit's own user/assistant testids collapse into one.
    with st.container(key=f"turn-{entry['role']}-{turn}"), st.chat_message(entry["role"], avatar=AVATARS[entry["role"]]):
        st.markdown(entry["text"])
        for t in entry["tools"]:
            st.caption(t)

if state.get("import_error"):
    st.error(state.import_error)
if state.get("import_note"):
    st.info(state.import_note)
if state.pending_import:
    with st.container(border=True):
        preview_import()

# submit_mode="stop" turns the send arrow into a stop button while the reply streams.
submitted = st.chat_input(
    CHAT_PLACEHOLDER,
    submit_mode="stop",
    accept_file=True,
    file_type=DOC_FILE_TYPES + AUDIO_FILE_TYPES,
    accept_audio=voice.is_available(),
)
if submitted:
    prompt, attachments = submission(submitted)
    state.import_error = state.import_note = None
    if attachments:
        with st.spinner("Reading the attachment…"):
            read_attachment(attachments[0])
        if len(attachments) > 1:
            state.import_note = (
                f"Only {attachments[0].name} was read. Attach the others one at a time so each preview can be checked."
            )
    if prompt.strip():
        state.log.append({"role": "user", "text": prompt, "tools": []})
        with st.container(key="turn-user-live"), st.chat_message("user", avatar=AVATARS["user"]):
            st.markdown(prompt)
        with st.container(key="turn-assistant-live"), st.chat_message("assistant", avatar=AVATARS["assistant"]):
            box = st.empty()
            text, tools = "", []
            before = state.profile.model_dump_json()
            try:
                text, tools = interview_turn(prompt, box)
            except (ProviderError, NotImplementedError, ValueError) as e:
                st.error(str(e))
            if state.profile.model_dump_json() != before and (state.assessment or state.document):
                clear_results()  # the profile changed, so the old figures no longer apply
                tools.append("The profile changed, so the earlier analysis was refreshed.")
            box.markdown(text or "_(no reply)_")
            for t in tools:
                st.caption(t)
        state.log.append({"role": "assistant", "text": text, "tools": tools})
    st.rerun()

# ---------- Results: hidden until the profile holds every fact the analysis needs ----------
complete = not missing_fields(state.profile)
if complete and state.assessment is None and state.analysis_error is None:
    with st.spinner("Working out the automation fit…"):
        run_analysis()  # flows straight out of the conversation, with no button to press

if complete:
    st.divider()
    head, action = st.columns([3, 1], vertical_alignment="bottom")
    head.subheader("Automation assessment")
    if action.button(
        "Re-run analysis", width="stretch", help="Recomputes every figure from the profile as it stands now."
    ):
        clear_results()
        run_analysis()
    if state.analysis_error:
        st.error(state.analysis_error)

a = state.assessment
if a:
    m1, m2, m3, m4 = st.columns(4)
    m1.metric("Fit score", f"{a.fit.total} / 100", border=True, help=METRIC_HELP["fit"])
    m2.metric("Recommendation", a.ai_decision.approach, border=True, help=METRIC_HELP["approach"])
    m3.metric(
        "Payback", "never" if a.chosen.payback_months is None else f"{a.chosen.payback_months:.1f} mo",
        border=True, help=METRIC_HELP["payback"],
    )
    m4.metric("Effort", f"{a.effort.size} · {a.effort.days:g} days", border=True, help=METRIC_HELP["effort"])
    live = state.pricing.get("_live")
    st.caption(
        ("Prices: static planning assumptions" if not live else f"Prices: {live['status']} data ({live['fetched_at']} UTC) "
         "over static assumptions") + " — validate before quoting."
    )

    names = ["Process", "Fit score", "AI decision", "Cost & ROI", "Compliance", "Delivery", "Existing automation"]
    renders = [process_table, fit_score, ai_decision, cost_table, compliance_section, delivery_section, existing_automation]
    for tab, render in zip(st.tabs(names), renders):
        with tab:
            st.markdown(render(a))

    # ---------- Design document ----------
    st.divider()
    st.subheader("Solution design document")
    st.caption("A full write-up you can hand to a delivery team, with the computed tables filled in.")
    if st.button("Generate design document", type="primary"):
        box = st.empty()
        draft = ""
        orch = Orchestrator(settings, current_pricing())
        llm = use_llm()
        try:
            for item in orch.write_document(a, provider() if llm else None):
                if isinstance(item, str):
                    state.document = item
                    state.document_by = "AI writer" if llm else "template writer (offline)"
                elif isinstance(item, TextDelta):
                    draft += item.text
                    box.markdown(draft + "▌")
                elif isinstance(item, Notice):
                    st.caption(item.text)
        except (ProviderError, NotImplementedError) as e:
            state.llm_error = str(e)
            st.warning(f"The AI writer failed ({e}). Wrote the document with the offline template writer instead.")
            state.document = next(i for i in orch.write_document(a, None) if isinstance(i, str))
            state.document_by = "template writer (offline)"
        box.empty()

    if state.document:
        st.caption(f"Written by the {state.document_by}. All figures come from the Python analysis.")
        name = (a.profile.name or "process").lower().replace(" ", "-")
        d1, d2 = st.columns(2)
        d1.download_button("Download Markdown", state.document, f"{name}-design.md", "text/markdown")
        try:
            docx_bytes = to_docx(state.document)
        except Exception as e:  # a bad character in the text must not take the whole page down
            docx_bytes = None
            d2.caption(f"The Word file could not be built ({type(e).__name__}). Use the Markdown download.")
        if docx_bytes is not None:
            d2.download_button(
                "Download Word (.docx)",
                docx_bytes,
                f"{name}-design.docx",
                "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            )
        st.markdown(state.document)
