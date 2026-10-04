"""PHI/PII and UAE data-residency checks (rule-based)."""

from __future__ import annotations

from dataclasses import dataclass, field

from advisor.models import ProcessProfile


@dataclass
class ComplianceResult:
    sensitive: bool
    categories: list[str]
    can_send_to_llm: bool
    llm_condition: str
    regulations: list[str] = field(default_factory=list)
    required_controls: list[str] = field(default_factory=list)


def assess(profile: ProcessProfile, llm_data_region: str) -> ComplianceResult:
    d = profile.data
    categories = [c for c, flag in (("PHI", d.contains_phi), ("PII", d.contains_pii)) if flag]
    restricted = d.classification == "restricted"
    sensitive = bool(categories) or restricted
    in_uae = llm_data_region in {"UAE", "AE"}

    regulations: list[str] = []
    controls: list[str] = []
    if d.contains_pii or d.contains_phi:
        regulations.append("UAE Federal Decree-Law No. 45 of 2021 (PDPL) — personal data protection")
        controls += [
            "Data processing agreement with every AI/automation vendor",
            "Least-privilege service accounts and audit logging for bots and flows",
        ]
    if d.contains_phi:
        regulations.append("UAE Federal Law No. 2 of 2019 (ICT in health fields) — health data localisation")
        regulations.append("Emirate health-authority data standards (DoH Abu Dhabi / DHA) where applicable")
        controls += [
            "De-identify or redact patient identifiers before any AI call",
            "Human review of every AI output that affects patient records",
        ]
    if d.uae_data_residency_required:
        controls.append("Keep storage and processing in a UAE region (e.g. Azure UAE North)")

    if d.uae_data_residency_required and not in_uae:
        # residency is a hard rule on its own, whether or not PHI/PII is declared
        can_send = False
        condition = (
            f"Raw data cannot go to an LLM hosted in {llm_data_region}. Use a UAE-hosted model "
            + ("or send only de-identified data." if sensitive else "(the data must stay in the UAE).")
        )
    elif not sensitive:
        can_send, condition = True, "No sensitive data declared; standard vendor terms apply."
    elif d.contains_phi:
        can_send = True
        condition = "Allowed only with de-identification, a signed DPA, and human review of outputs."
    else:
        can_send = True
        condition = "Allowed with a signed DPA and minimised fields (send only what the step needs)."

    if regulations:
        regulations.append("Verify applicability with your legal/compliance team")

    return ComplianceResult(
        sensitive=sensitive,
        categories=categories + (["Restricted classification"] if restricted else []),
        can_send_to_llm=can_send,
        llm_condition=condition,
        regulations=regulations,
        required_controls=controls,
    )
