"""Offline Writer: builds the solution design narrative from the Assessment without an LLM.

Produces the same outline and placeholder tokens as `writer.SYSTEM_PROMPT`, so
`report.fill()` works on its output exactly as on the LLM Writer's.
"""

from __future__ import annotations

import re
from typing import Iterator

from advisor.agents.base import AgentEvent
from advisor.agents.specialists import Assessment
from advisor.analysis.delivery import HIGH_EXCEPTION_PCT
from advisor.llm.base import Message, TextDelta

DESKTOP_TOOL = "desktop flow"
MANUAL_TOOL = "Manual task"

STABILITY_TEXT = {
    "stable": "The process rules are stable.",
    "changes_quarterly": "The process rules change roughly every quarter.",
    "changes_monthly": "The process rules change roughly every month.",
}


def _join(items: list[str]) -> str:
    items = [i for i in items if i]
    if len(items) <= 1:
        return "".join(items)
    return ", ".join(items[:-1]) + " and " + items[-1]


def _the_steps(names: list[str]) -> str:
    """'the step X' or 'the steps X and Y' (keeps step names from being read as sentence parts)."""
    return ("the step " if len(names) == 1 else "the steps ") + _join(names)


def _sentence(text: str) -> str:
    text = text.strip()
    return text if not text or text[-1] in ".!?" else text + "."


def _step_detail(step) -> str:
    """Extra step information worth printing: BPMN actor/decision, or a description that adds to the name."""
    desc = (step.description or "").strip()
    if not desc:
        return ""
    parts: list[str] = []
    m = re.search(r"Actor:\s*([^.]+)\.?", desc)
    if m:
        parts.append(f"performed by {m.group(1).strip()}")
        desc = desc.replace(m.group(0), " ")
    for m in re.finditer(r"Follows decision '([^']*)'(?: \(branch '([^']*)'\))?\.?", desc):
        parts.append(f"follows the decision \"{m.group(1)}\"" + (f", branch \"{m.group(2)}\"" if m.group(2) else ""))
        desc = desc.replace(m.group(0), " ")
    desc = re.sub(r"\s+", " ", desc).strip(" .;")
    words = lambda t: {w for w in re.findall(r"[a-z0-9]+", t.lower()) if len(w) > 2}  # noqa: E731
    known = words(step.name) | words(step.system or "") | {"min", "mins", "minutes", "minute", "hour", "hours"}
    if desc and words(desc) - known:  # keep only text that says more than the name already does
        extra = words(desc) - known
        if len(extra) >= 3 or len(extra) / max(len(words(desc)), 1) > 0.4:
            parts.insert(0, _lower_first(desc))
    return "; ".join(parts)


def _lower_first(text: str) -> str:
    return text[:1].lower() + text[1:] if text else text


class TemplateWriter:
    """Deterministic drop-in for WriterAgent when no LLM is available."""

    def write(self, a: Assessment, history: list[Message]) -> Iterator[AgentEvent]:
        chunks: list[str] = []
        for section in self._sections(a):
            text = section.strip("\n") + "\n\n"
            chunks.append(text)
            yield TextDelta(text)
        history.append(Message(role="assistant", content="".join(chunks).rstrip() + "\n"))

    def render(self, a: Assessment) -> str:
        """Convenience: the full narrative (with placeholders) as one string."""
        history: list[Message] = []
        for _ in self.write(a, history):
            pass
        return history[-1].content

    # -- sections ---------------------------------------------------------

    def _sections(self, a: Assessment) -> Iterator[str]:
        name = a.profile.name or "Untitled process"
        yield f"# Solution Design: {name}"
        yield "## 1. Executive summary\n\n" + self._executive_summary(a)
        yield "## 2. Process summary\n\n" + self._process_summary(a) + "\n\n{{PROCESS_TABLE}}"
        yield "## 3. Automation fit\n\n{{FIT_SCORE}}\n\n" + self._fit(a)
        yield "## 4. AI decision\n\n{{AI_DECISION}}\n\n" + self._ai_decision(a)
        reuse = self._reuse(a)
        yield ("## 5. Target architecture\n\n" + self._architecture(a) + "\n\n{{DELIVERY}}\n\n"
               + (reuse + "\n\n" if reuse else "") + "{{EXISTING_AUTOMATION}}")
        yield "## 6. Risks and compliance\n\n{{COMPLIANCE}}\n\n" + self._risks(a)
        yield "## 7. Cost and ROI\n\n{{COST_TABLE}}\n\n" + self._cost_note(a)
        yield "## 8. Next steps\n\n" + self._next_steps(a)

    def _executive_summary(self, a: Assessment) -> str:
        p, d = a.profile, a.ai_decision
        owner = " in " + _join([x for x in (p.department, p.industry and f"the {p.industry} sector") if x]) \
            if (p.department or p.industry) else ""
        intro = f"This document sets out an automation design for **{p.name or 'the process'}**{owner}."
        if p.description:
            intro += " " + _sentence(p.description)
        rec = f"The recommended approach is **{d.approach}**. {_sentence(d.reason)}"
        fit = (f"The automation fit assessment places the process in the band "
               f"\"{a.fit.band}\", and the build effort is sized as **{a.effort.size}**.")
        parts = [intro, rec + " " + fit]
        if d.alternative:
            parts.append(f"Alternative to consider: {_sentence(d.alternative)}")
        return "\n\n".join(parts)

    def _process_summary(self, a: Assessment) -> str:
        p = a.profile
        steps = p.steps
        if not steps:
            return "No process steps were recorded."

        def describe(s) -> str:
            text = f"**{s.name}**"
            detail = _step_detail(s)
            if detail:
                text += f" ({detail})"
            if s.system:
                text += f" in {s.system}"
            return text

        if len(steps) == 1:
            flow = f"The process has a single step: {describe(steps[0])}."
        else:
            middle = "".join(f"; then {describe(s)}" for s in steps[1:-1])
            flow = f"The process runs in {len(steps)} steps: first {describe(steps[0])}{middle}; " \
                   f"and finally {describe(steps[-1])}."
        extra = []
        if p.frequency:
            extra.append(f"Work arrives {_lower_first(p.frequency.rstrip('.'))}.")
        if p.process_stability in STABILITY_TEXT:
            extra.append(STABILITY_TEXT[p.process_stability])
        approvals = [s.name for s in steps if s.is_approval]
        judgment = [s.name for s in steps if s.requires_judgment and not s.is_approval]
        unstructured = [s.name for s in steps if s.input_type and s.input_type != "structured"]
        if approvals:
            extra.append(f"Sign-off is performed in {_the_steps(approvals)}.")
        if judgment:
            extra.append(f"Staff apply judgment in {_the_steps(judgment)}.")
        if unstructured:
            extra.append(f"Non-structured inputs are handled in {_the_steps(unstructured)}.")
        return flow + ("\n\n" + " ".join(extra) if extra else "")

    def _fit(self, a: Assessment) -> str:
        comps = [c for c in a.fit.components if c.max_points]
        if not comps:
            return f"Overall the process is rated \"{a.fit.band}\"."
        ranked = sorted(comps, key=lambda c: c.points / c.max_points)
        weakest, strongest = ranked[0], ranked[-1]
        out = [f"Overall the process is rated \"{a.fit.band}\"."]
        if strongest is weakest or strongest.points / strongest.max_points == weakest.points / weakest.max_points:
            out.append("All factors contribute evenly to the score.")
        else:
            out.append(f"The strongest factor is **{strongest.name}** ({strongest.note}), "
                       f"and the weakest is **{weakest.name}** ({weakest.note}).")
            if weakest.points / weakest.max_points < 0.5:
                factor = weakest.name if weakest.name.isupper() else weakest.name.lower()  # keep acronyms such as ROI
                out.append(f"Improving {factor} is the main lever for a stronger case.")
        return " ".join(out)

    def _ai_decision(self, a: Assessment) -> str:
        d, c = a.ai_decision, a.compliance
        signal_steps = [s.step for s in d.ai_steps]
        all_steps = [s.name for s in a.profile.steps]
        rule_steps = [n for n in all_steps if n not in signal_steps]
        out = []
        if not signal_steps:
            out.append("No step shows a need for AI: inputs are structured and decisions follow rules, "
                       "so standard Power Automate flows cover the whole process. Leaving AI out keeps run "
                       "costs predictable and avoids sending data to a model.")
        elif d.ai_needed:
            reasons = "; ".join(f"{s.step} ({', '.join(s.reasons)})" for s in d.ai_steps)
            out.append(f"AI belongs in the steps that rules cannot handle reliably: {reasons}.")
            if rule_steps:
                out.append(f"The remaining steps ({_join(rule_steps)}) are rule-based and run as standard "
                           "flows without AI.")
            if d.approach == "AI-first":
                out.append("Because AI sits at the centre of this design, its output quality determines the "
                           "outcome, so validation and human review are built into each AI step.")
            else:
                out.append("AI is kept to a narrow role inside a rules-driven flow, which limits cost and "
                           "makes behaviour easier to predict.")
            if c.sensitive:
                out.append(f"Data condition for AI use: {_sentence(c.llm_condition)}")
        else:
            out.append(f"AI would help with {_join(signal_steps)}, but it is not used in the recommended "
                       "design; these steps remain manual tasks assigned through the flow, while "
                       + (f"{_the_steps(rule_steps)} {'is' if len(rule_steps) == 1 else 'are'} automated with rules." if rule_steps
                          else "the flow handles routing and tracking."))
            if not c.can_send_to_llm:
                out.append(f"The deciding constraint is data handling: {_sentence(c.llm_condition)}")
        return " ".join(out)  # the alternative is rendered by {{AI_DECISION}}

    def _architecture(self, a: Assessment) -> str:
        lines = ["The target flow follows the current process order. Each step uses the tool below, with "
                 "a person kept in the loop where shown:", ""]
        for i, sp in enumerate(a.step_plans, 1):
            line = f"{i}. **{sp.step}** — {sp.tool}."
            if sp.human_reason:
                line += f" Human review: {_sentence(sp.human_reason)}"
            lines.append(line)
        notes = []
        if any(DESKTOP_TOOL in sp.tool for sp in a.step_plans):
            notes.append("Systems without an API or connector are reached through unattended desktop flows "
                         "triggered from the cloud flow.")
        if any(sp.tool.startswith(MANUAL_TOOL) for sp in a.step_plans):
            notes.append("Manual steps are assigned to staff by the flow, so work is still tracked end to end.")
        if a.compliance.sensitive:
            notes.append("Sensitive data is handled under the controls listed in section 6.")
        if notes:
            lines += ["", " ".join(notes)]
        return "\n".join(lines)

    def _reuse(self, a: Assessment) -> str:
        if a.overlaps:
            steps = _join(sorted({o.step for o in a.overlaps}))
            text = (f"Existing flows in the tenant may already cover {steps}. Review them below before building, "
                    "and extend or reuse them where they fit.")
        elif a.flows:
            text = "The existing flows provided do not appear to cover any step, so the design builds new flows."
        else:
            text = ""  # the existing-automation block below already says no flows were provided
        if a.inventory is not None:
            text += " Spare licences already owned in the tenant are counted against the licence cost."
        return "**Reuse of existing automation.** " + text.strip() if text.strip() else ""

    def _risks(self, a: Assessment) -> str:
        c, d, p = a.compliance, a.ai_decision, a.profile
        bullets = []
        if c.sensitive:
            controls = "; ".join(c.required_controls) or "access control and audit logging"
            channels = "flows, bots or AI calls" if d.ai_needed else "flows or bots"
            bullets.append(f"**Data privacy ({', '.join(c.categories)})** — exposure of sensitive data through "
                           f"{channels}. *Mitigation:* {controls}.")
            if p.data.uae_data_residency_required:
                bullets.append("**Data residency** — data leaving the UAE. *Mitigation:* keep storage, flows and "
                               "any AI processing in a UAE region; " + _lower_first(_sentence(c.llm_condition)))
        else:
            bullets.append("**Data classification** — no sensitive data was declared. *Mitigation:* confirm the "
                           "classification with the data owner before go-live.")
        for driver in a.maintenance.drivers:
            bullets.append(f"**Maintenance: {driver}** — *Mitigation:* {self._mitigation(driver)}")
        if d.ai_needed:
            bullets += [
                "**AI accuracy** — the model can return confident but wrong output. *Mitigation:* validate "
                "outputs against business rules and route low-confidence results to a person.",
                "**AI run cost** — cost grows with volume. *Mitigation:* monitor usage against the cost "
                "assumptions and set budget alerts.",
                "**AI drift** — behaviour can change with model or input changes. *Mitigation:* re-test prompts "
                "on a labelled sample set before each change and on a regular schedule.",
            ]
        exc = sorted((s for s in p.steps if (s.exception_rate_pct or 0) > 0),
                     key=lambda s: -(s.exception_rate_pct or 0))
        if exc:
            high = [s.name for s in exc if (s.exception_rate_pct or 0) >= HIGH_EXCEPTION_PCT]
            focus = _join(high) if high else exc[0].name
            bullets.append(f"**Exceptions** — some runs will not fit the automated path (highest in {focus}). "
                           "*Mitigation:* route exceptions to a work queue with clear ownership and review "
                           "exception trends to refine the rules.")
        if any(s.is_approval for s in p.steps):
            bullets.append("**Approvals** — automated sign-off would weaken control. *Mitigation:* approvals stay "
                           "with a named person; the flow only routes and records them.")
        return "\n".join(f"- {b}" for b in bullets)

    @staticmethod
    def _mitigation(driver: str) -> str:
        low = driver.lower()
        if "ui automation" in low:
            return ("use robust selectors, monitor bot runs with alerts, and ask the vendor about an API or "
                    "connector to replace the UI automation.")
        if "process rules change" in low:
            return "keep business rules in configuration rather than in flow logic, with change control."
        if "ai prompts" in low:
            return "keep a regression set of real cases and re-test prompts and models before each change."
        return "monitor connector and flow health and review the design when systems change."

    def _cost_note(self, a: Assessment) -> str:
        chosen = "Rules + AI" if a.ai_decision.ai_needed else "Rules-only"
        return (f"The recommended scenario is **{chosen}**. All figures above are computed from the pricing "
                "assumptions file and should be validated before they are quoted.")

    def _next_steps(self, a: Assessment) -> str:
        c, d = a.compliance, a.ai_decision
        steps = [
            "**Validate the pricing assumptions** — confirm licence prices, AI usage rates, labour rates, day "
            "rates and exchange rates in the pricing assumptions file against current quotes.",
            "**Confirm the process facts** — review volumes, handling times and exception rates with the "
            "process owner.",
        ]
        if c.sensitive or c.regulations:
            steps.append("**Compliance sign-off** — review the listed regulations and required controls with "
                         "legal and information security, and sign data processing agreements before build.")
        hitl = [sp.step for sp in a.step_plans if sp.needs_human]
        if hitl:
            steps.append(f"**Design the human-in-the-loop steps** — define queues, owners and service levels "
                         f"for {_join(hitl)}.")
        if d.ai_needed:
            steps.append(f"**Build an AI evaluation set** — collect labelled real cases for "
                         f"{_join([s.step for s in d.ai_steps])} and agree accuracy targets before go-live.")
        elif d.alternative:
            steps.append(f"**Assess the alternative** — {_lower_first(_sentence(d.alternative))}")
        if any(DESKTOP_TOOL in sp.tool for sp in a.step_plans):
            steps.append("**Make UI automation resilient** — test desktop flows against upcoming UI changes, "
                         "set up run monitoring, and track any API or connector the vendor releases.")
        steps.append("**Run a pilot** — automate a limited scope first, measure against today's manual "
                     "baseline, then scale.")
        steps.append("**Plan operations** — assign ownership for monitoring, exceptions and change requests "
                     "after go-live.")
        return "\n".join(f"{i}. {s}" for i, s in enumerate(steps, 1))
