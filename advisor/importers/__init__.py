"""Turn process descriptions (text, BPMN, Visio, voice) into profile updates without an LLM."""

from __future__ import annotations

from dataclasses import dataclass, field

from advisor.models import ProfileUpdate


@dataclass
class ImportResult:
    update: ProfileUpdate
    notes: list[str] = field(default_factory=list)  # what was inferred / assumed / skipped, shown to the user
