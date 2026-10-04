"""Process profile collected by the Interviewer and consumed by the analysis modules.

Every field is optional while the interview is in progress; `missing_fields()`
lists what is still needed before analysis can run.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, field_validator

InputType = Literal["structured", "semi_structured", "unstructured", "scanned_document", "email", "voice"]
Stability = Literal["stable", "changes_quarterly", "changes_monthly"]
UIChange = Literal["rare", "occasional", "frequent"]
Classification = Literal["public", "internal", "confidential", "restricted"]

AI_INPUT_TYPES = {"unstructured", "scanned_document", "email", "voice"}


_CURRENCY_ALIASES = {
    "DH": "AED", "DHS": "AED", "DIRHAM": "AED", "DIRHAMS": "AED", "$": "USD", "US$": "USD", "DOLLAR": "USD",
    "DOLLARS": "USD", "€": "EUR", "EURO": "EUR", "EUROS": "EUR", "£": "GBP", "POUND": "GBP", "POUNDS": "GBP",
    "RIYAL": "SAR", "RIYALS": "SAR", "₹": "INR", "RS": "INR", "RUPEE": "INR", "RUPEES": "INR",
}


def normalize_currency(value: str | None) -> str | None:
    """'aed' / ' Dh ' / 'dirham' -> 'AED'. Unknown text is only upper-cased and stripped."""
    if value is None:
        return None
    code = str(value).strip().rstrip(".").upper()
    return _CURRENCY_ALIASES.get(code, code) or None


class Step(BaseModel):
    name: str = Field(description="Short unique step name, used as the key for updates.")
    description: str | None = None
    minutes_per_run: float | None = Field(None, ge=0, description="Human handling time for this step per transaction.")
    system: str | None = Field(None, description="Name of the system this step is performed in.")
    input_type: InputType | None = Field(None, description="Kind of input the step works from.")
    requires_judgment: bool | None = Field(None, description="Needs human judgment that is not written down as rules.")
    needs_language_understanding: bool | None = Field(
        None, description="Must read/interpret/write natural language (classify an email, summarise notes, etc.)."
    )
    rules_documented: bool | None = Field(None, description="Decision rules for this step are written down.")
    exception_rate_pct: float | None = Field(None, ge=0, le=100, description="Share of runs that hit an exception.")
    is_approval: bool | None = Field(None, description="Step is a sign-off / approval.")


class System(BaseModel):
    name: str = Field(description="System/application name, used as the key for updates.")
    has_api_or_connector: bool | None = Field(None, description="Has an API or Power Automate connector.")
    ui_change_frequency: UIChange | None = Field(None, description="How often the screens change.")
    is_legacy_desktop: bool | None = Field(None, description="Desktop/legacy app reachable only through its UI.")


class DataProfile(BaseModel):
    contains_phi: bool | None = Field(None, description="Protected health information (patient data).")
    contains_pii: bool | None = Field(None, description="Personally identifiable information.")
    uae_data_residency_required: bool | None = Field(None, description="Data must stay in the UAE.")
    classification: Classification | None = None


class ProcessProfile(BaseModel):
    name: str | None = None
    description: str | None = None
    department: str | None = None
    industry: str | None = None
    transactions_per_month: float | None = Field(None, ge=0)
    frequency: str | None = Field(None, description="e.g. 'daily, peaks at month end'.")
    hourly_labor_cost: float | None = Field(None, ge=0, description="Fully loaded cost per staff hour.")
    currency: str | None = None
    process_stability: Stability | None = None
    error_rate_pct: float | None = Field(None, ge=0, le=100, description="Current manual error/rework rate.")
    steps: list[Step] = Field(default_factory=list)
    systems: list[System] = Field(default_factory=list)
    data: DataProfile = Field(default_factory=DataProfile)

    _normalize_currency = field_validator("currency")(lambda cls, v: normalize_currency(v))

    def merge(self, update: "ProfileUpdate") -> None:
        """Apply a partial update; steps and systems are upserted by name."""
        for key, value in update.model_dump(exclude_none=True, exclude={"steps", "systems", "data", "remove_steps"}).items():
            setattr(self, key, normalize_currency(value) if key == "currency" else value)
        if update.data:
            for key, value in update.data.model_dump(exclude_none=True).items():
                setattr(self.data, key, value)
        for step in update.steps or []:
            _upsert(self.steps, step)
        for system in update.systems or []:
            _upsert(self.systems, system)
        for name in update.remove_steps or []:
            self.steps = [s for s in self.steps if s.name.lower() != name.lower()]

    def total_minutes(self) -> float:
        return sum(s.minutes_per_run or 0 for s in self.steps)

    def system_by_name(self, name: str | None) -> System | None:
        if not name:
            return None
        return next((s for s in self.systems if s.name.lower() == name.lower()), None)


class ProfileUpdate(BaseModel):
    """Partial update sent by the Interviewer. Omit anything not yet known."""

    name: str | None = None
    description: str | None = None
    department: str | None = None
    industry: str | None = None
    transactions_per_month: float | None = Field(None, ge=0)
    frequency: str | None = None
    hourly_labor_cost: float | None = Field(None, ge=0)
    currency: str | None = None
    process_stability: Stability | None = None
    error_rate_pct: float | None = Field(None, ge=0, le=100)
    steps: list[Step] | None = Field(None, description="Steps to add or update (matched by name).")
    systems: list[System] | None = Field(None, description="Systems to add or update (matched by name).")
    data: DataProfile | None = None
    remove_steps: list[str] | None = Field(None, description="Names of steps to delete.")

    _normalize_currency = field_validator("currency")(lambda cls, v: normalize_currency(v))


def _upsert(items: list, new: BaseModel) -> None:
    for i, existing in enumerate(items):
        if existing.name.lower() == new.name.lower():
            merged = existing.model_dump() | new.model_dump(exclude_none=True, exclude={"name"})
            items[i] = type(existing)(**merged)
            return
    items.append(new)


STEP_FIELDS = ("minutes_per_run", "system", "input_type", "requires_judgment", "rules_documented", "exception_rate_pct")


def missing_fields(p: ProcessProfile) -> list[str]:
    """Facts still needed before the analysis can run."""
    missing = [
        f
        for f in ("name", "transactions_per_month", "hourly_labor_cost", "process_stability")
        if getattr(p, f) is None
    ]
    for f in ("contains_phi", "contains_pii", "uae_data_residency_required"):
        if getattr(p.data, f) is None:
            missing.append(f"data.{f}")
    if len(p.steps) < 2:
        missing.append("steps (at least 2)")
    for s in p.steps:
        missing += [f"step '{s.name}'.{f}" for f in STEP_FIELDS if getattr(s, f) is None]
        if s.system and p.system_by_name(s.system) is None:
            missing.append(f"system '{s.system}' details")
    for sys in p.systems:
        missing += [
            f"system '{sys.name}'.{f}"
            for f in ("has_api_or_connector", "ui_change_frequency")
            if getattr(sys, f) is None
        ]
    return missing
