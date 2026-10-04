"""Tests for the presentation helpers in advisor/ui.py."""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from advisor.models import DataProfile, ProcessProfile, Step, System, missing_fields
from advisor.ui import CSS, FIELD_HELP, GROUP_HELP, Readiness, readiness

SAMPLE = Path(__file__).resolve().parents[1] / "advisor" / "samples" / "claims_intake.json"

GROUP_TITLES = ["Process basics", "Steps", "Systems", "Data"]


def sample_profile() -> ProcessProfile:
    return ProcessProfile.model_validate_json(SAMPLE.read_text(encoding="utf-8"))


def complete_profile() -> ProcessProfile:
    """The smallest profile that `missing_fields()` accepts."""
    return ProcessProfile(
        name="Invoice posting",
        transactions_per_month=500,
        hourly_labor_cost=80,
        process_stability="stable",
        steps=[
            Step(
                name="Receive invoice",
                minutes_per_run=4,
                system="Outlook",
                input_type="email",
                requires_judgment=False,
                rules_documented=True,
                exception_rate_pct=5,
            ),
            Step(
                name="Post to ledger",
                minutes_per_run=6,
                system="ERP",
                input_type="structured",
                requires_judgment=False,
                rules_documented=True,
                exception_rate_pct=2,
            ),
        ],
        systems=[
            System(name="Outlook", has_api_or_connector=True, ui_change_frequency="rare"),
            System(name="ERP", has_api_or_connector=True, ui_change_frequency="occasional"),
        ],
        data=DataProfile(contains_phi=False, contains_pii=True, uae_data_residency_required=False),
    )


def partial_profile() -> ProcessProfile:
    p = complete_profile()
    p.hourly_labor_cost = None
    p.steps[1].minutes_per_run = None
    p.systems[0].ui_change_frequency = None
    p.data.contains_phi = None
    return p


def one_step_profile() -> ProcessProfile:
    p = complete_profile()
    p.steps = p.steps[:1]
    return p


def unknown_system_profile() -> ProcessProfile:
    p = complete_profile()
    p.steps[1].system = "Mainframe"  # no matching System entry
    return p


def orphan_system_profile() -> ProcessProfile:
    p = complete_profile()
    p.systems.append(System(name="Fax gateway"))  # a system no step mentions, with no answers
    return p


PROFILES = {
    "empty": ProcessProfile(),
    "partial": partial_profile(),
    "sample": sample_profile(),
    "complete": complete_profile(),
    "one_step": one_step_profile(),
    "unknown_system": unknown_system_profile(),
    "orphan_system": orphan_system_profile(),
    "no_steps_but_systems": ProcessProfile(systems=[System(name="Outlook")]),
}


# ---------------------------------------------------------------- shape


def test_groups_are_the_four_expected_groups_in_order():
    assert [g.title for g in readiness(ProcessProfile()).groups] == GROUP_TITLES


def test_every_group_carries_help_text():
    for group in readiness(ProcessProfile()).groups:
        assert group.help == GROUP_HELP[group.title]
        assert len(group.help) > 60


def test_empty_profile_has_nothing_done():
    r = readiness(ProcessProfile())
    assert r.done == 0
    assert r.total == 8  # 4 basics + "at least two steps" + 3 data answers
    assert not r.complete
    assert r.percent == 0
    assert r.fraction == 0.0
    by_title = {g.title: g for g in r.groups}
    assert by_title["Process basics"].done == 0
    assert by_title["Steps"].missing == ["At least two steps in the process"]
    assert by_title["Systems"].total == 0
    assert len(by_title["Data"].missing) == 3


def test_empty_group_with_no_items_counts_as_complete():
    # No steps means no systems are expected yet, so that group must not read as blocking.
    systems = next(g for g in readiness(ProcessProfile()).groups if g.title == "Systems")
    assert (systems.done, systems.total) == (0, 0)
    assert systems.complete


def test_partial_profile_reports_each_open_item_once():
    r = readiness(partial_profile())
    assert not r.complete
    assert r.done == r.total - 4
    assert 0 < r.percent < 100
    missing = [m for g in r.groups for m in g.missing]
    assert "Fully loaded cost of one staff hour" in missing
    assert "Post to ledger — how many minutes it takes" in missing
    assert "Outlook — how often do its screens change?" in missing
    assert "Does it involve patient health data?" in missing
    assert len(missing) == 4


def test_sample_profile_is_complete():
    r = readiness(sample_profile())
    assert r.complete
    assert r.done == r.total
    assert r.percent == 100
    assert r.fraction == 1.0
    assert all(not g.missing for g in r.groups)
    # 4 basics + steps gate + 6 steps x 6 fields + 4 systems x 2 + 3 data answers
    assert r.total == 4 + 1 + 6 * 6 + 4 * 2 + 3


def test_complete_profile_is_complete():
    r = readiness(complete_profile())
    assert r.complete
    assert not [m for g in r.groups for m in g.missing]


def test_step_labels_name_the_step():
    r = readiness(ProcessProfile(steps=[Step(name="Register claim"), Step(name="Pay claim")]))
    steps = next(g for g in r.groups if g.title == "Steps")
    assert "Register claim — how many minutes it takes" in steps.missing
    assert any(m.startswith("Pay claim — ") for m in steps.missing)


def test_unknown_system_named_by_a_step_is_flagged():
    systems = next(g for g in readiness(unknown_system_profile()).groups if g.title == "Systems")
    assert "Mainframe — we still need the details of this system" in systems.missing


def test_orphan_system_still_needs_its_answers():
    systems = next(g for g in readiness(orphan_system_profile()).groups if g.title == "Systems")
    assert "Fax gateway — does it have an API or a connector?" in systems.missing
    assert "Fax gateway — how often do its screens change?" in systems.missing


# ---------------------------------------------------------------- invariants


@pytest.mark.parametrize("key", list(PROFILES))
def test_complete_agrees_with_missing_fields(key):
    profile = PROFILES[key]
    assert readiness(profile).complete is (not missing_fields(profile))


@pytest.mark.parametrize("key", list(PROFILES))
def test_open_item_count_matches_missing_fields(key):
    profile = PROFILES[key]
    r = readiness(profile)
    assert r.total - r.done == len(missing_fields(profile))
    assert sum(len(g.missing) for g in r.groups) == len(missing_fields(profile))


@pytest.mark.parametrize("key", list(PROFILES))
def test_counts_stay_within_bounds(key):
    r = readiness(PROFILES[key])
    assert r.done == sum(g.done for g in r.groups)
    assert r.total == sum(g.total for g in r.groups)
    assert 0 <= r.done <= r.total
    assert 0.0 <= r.fraction <= 1.0
    assert 0 <= r.percent <= 100
    for g in r.groups:
        assert 0 <= g.done <= g.total
        assert len(g.missing) == g.total - g.done
        assert g.complete is (not g.missing)


@pytest.mark.parametrize("key", list(PROFILES))
def test_no_raw_field_paths_in_any_label(key):
    for group in readiness(PROFILES[key]).groups:
        for label in group.missing:
            assert not re.search(r"[a-z]+\.[a-z_]+", label), label  # e.g. "data.contains_phi"
            assert "_" not in label, label
            assert "'" not in label, label
            assert label[0].isupper(), label


def test_readiness_does_not_mutate_the_profile():
    profile = partial_profile()
    before = profile.model_dump_json()
    readiness(profile)
    assert profile.model_dump_json() == before


def test_readiness_is_a_plain_dataclass_result():
    assert isinstance(readiness(ProcessProfile()), Readiness)


# ---------------------------------------------------------------- CSS and help text


def test_css_is_a_single_usable_style_block():
    assert isinstance(CSS, str)
    assert CSS.strip().startswith("<style>")
    assert CSS.strip().endswith("</style>")
    assert CSS.count("<style>") == 1
    assert len(CSS) > 500


def test_css_has_no_stray_format_placeholders():
    assert not re.search(r"\{[A-Za-z_][A-Za-z0-9_]*\}", CSS)
    assert "{}" not in CSS
    assert "%s" not in CSS
    assert CSS.count("{") == CSS.count("}")


def test_css_lays_out_the_two_sided_chat_and_hides_only_chrome():
    # The two sides are told apart by the st.container(key="turn-<role>-N") wrapper, because a custom
    # avatar collapses Streamlit's own user/assistant testids into one shared testid.
    assert '[class*="st-key-turn-user"]' in CSS
    assert '[class*="st-key-turn-assistant"]' in CSS
    assert "row-reverse" in CSS  # the advisor's turn sits on the right
    # The composer draws its own border; a second one on the outer shell reads as a box inside a box.
    assert re.search(r'\[data-testid="stChatInput"\]\s*\{[^}]*border:\s*none', CSS)
    # layout="wide" only reaches the page if nothing caps the main block.
    assert re.search(r'stMainBlockContainer"\]\s*\{[^}]*max-width:\s*none', CSS)
    # Colour, type and radius belong to .streamlit/config.toml; Streamlit exposes no theme CSS vars,
    # so a var(--...) here would silently resolve to nothing.
    assert "var(--" not in CSS
    # Nothing the test suite queries may be hidden: no display:none on inputs or buttons.
    for match in re.finditer(r"([^{}]+)\{([^{}]*)\}", CSS):
        selector, body = match.group(1), match.group(2)
        if "display: none" in body or "visibility: hidden" in body:
            assert not re.search(r"stChatInput|stButton|stTextArea|FileUploader|stSelectbox", selector), selector


def test_field_help_entries_are_short_and_informative():
    assert FIELD_HELP
    for key, text in FIELD_HELP.items():
        assert isinstance(text, str)
        assert 30 < len(text) <= 200, (key, len(text))
        assert text[0].isupper()
        assert text.endswith(".")


def test_field_help_covers_the_controls_app_needs():
    expected = {
        "flow_files", "license_file", "runtime_model", "live_refresh",
        "import_text", "import_diagram", "import_voice", "readiness",
    }
    assert expected <= set(FIELD_HELP)


def test_sample_file_still_parses_as_a_profile():
    # Guards the fixture above: a broken sample would quietly weaken every readiness test.
    assert json.loads(SAMPLE.read_text(encoding="utf-8"))["name"]
