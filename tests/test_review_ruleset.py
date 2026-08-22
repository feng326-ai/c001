"""Unit contract for the immutable review ruleset and priority scoring."""

from __future__ import annotations

import copy
import json
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from wxsearch.review_rules import (
    DEFAULT_RULESET,
    DEFAULT_RULESET_DEFINITION,
    DEFAULT_RULESET_SHA256,
    RULESET_VERSION,
    ReviewRulesetError,
    canonical_json,
    definition_sha256,
    parse_ruleset,
    score_priority,
    validate_completion,
)

NOW = datetime(2026, 8, 23, 8, 0, tzinfo=timezone.utc)
EXPECTED_DEFAULT_SHA256 = (
    "285b8c2fe43ca8b6d3517df223488f4d2fd3e6c7940cbe84ae547ade4b3f48ff"
)

# This table deliberately does not derive from DEFAULT_RULESET_DEFINITION.  It is
# the executable copy of docs/审核规则集契约_v1.md and therefore detects drift.
COMPLETION_CASES = (
    (
        "sales_ready_confirmed",
        "qualified",
        "sales_handoff",
        False,
        False,
        "opportunity_atomic_create",
    ),
    ("future_contact_window", "qualified", "nurture", True, False, None),
    ("valid_but_not_sales_ready", "qualified", "nurture", True, False, None),
    (
        "competitor_present_replaceable",
        "qualified",
        "competitor_watch",
        True,
        False,
        None,
    ),
    ("not_selection_or_voting", "rejected", "archive", False, False, None),
    ("event_ended_or_too_late", "rejected", "archive", False, False, None),
    ("no_online_voting_need", "rejected", "archive", False, False, None),
    ("outside_target_policy", "rejected", "archive", False, False, None),
    (
        "invalid_or_unverifiable_evidence",
        "rejected",
        "archive",
        False,
        False,
        None,
    ),
    ("other_rejection", "rejected", "archive", False, True, None),
    (
        "competitor_committed_no_entry",
        "rejected",
        "competitor_watch",
        True,
        False,
        None,
    ),
    (
        "stage_or_deadline_unknown",
        "needs_more_info",
        "nurture",
        True,
        False,
        None,
    ),
    (
        "online_voting_need_unknown",
        "needs_more_info",
        "nurture",
        True,
        False,
        None,
    ),
    (
        "organizer_unconfirmed",
        "needs_more_info",
        "nurture",
        True,
        False,
        None,
    ),
    (
        "contact_missing_or_stale",
        "needs_more_info",
        "nurture",
        True,
        False,
        None,
    ),
    (
        "evidence_conflict",
        "needs_more_info",
        "nurture",
        True,
        False,
        None,
    ),
    (
        "other_missing_information",
        "needs_more_info",
        "nurture",
        True,
        True,
        None,
    ),
)


def _definition_with(mutator):
    definition = copy.deepcopy(DEFAULT_RULESET_DEFINITION)
    mutator(definition)
    return definition


def _parse_rehashed(definition, *, version=RULESET_VERSION):
    return parse_ruleset(version, definition_sha256(definition), definition)


def _priority_components(total: str) -> dict[str, dict[str, str]]:
    remaining = Decimal(total)
    result: dict[str, dict[str, str]] = {}
    for code, maximum in DEFAULT_RULESET.priority_components.items():
        score = min(remaining, maximum)
        remaining -= score
        result[code] = {
            "score": str(score),
            "explanation_code": min(
                DEFAULT_RULESET.priority_explanation_codes[code]
            ),
        }
    assert remaining == 0
    return result


def test_default_ruleset_sha_and_json_parsing_are_stable() -> None:
    assert DEFAULT_RULESET_SHA256 == EXPECTED_DEFAULT_SHA256
    assert definition_sha256(DEFAULT_RULESET_DEFINITION) == EXPECTED_DEFAULT_SHA256
    assert DEFAULT_RULESET.version == RULESET_VERSION
    assert DEFAULT_RULESET.definition_sha256 == EXPECTED_DEFAULT_SHA256
    assert DEFAULT_RULESET.definition == DEFAULT_RULESET_DEFINITION

    reordered = dict(reversed(list(DEFAULT_RULESET_DEFINITION.items())))
    assert canonical_json(reordered) == canonical_json(DEFAULT_RULESET_DEFINITION)
    parsed = parse_ruleset(
        RULESET_VERSION,
        DEFAULT_RULESET_SHA256,
        json.dumps(reordered, ensure_ascii=False, indent=2),
    )
    assert parsed == DEFAULT_RULESET


@pytest.mark.parametrize(
    (
        "reason_code",
        "decision",
        "disposition",
        "requires_reopen",
        "requires_note",
        "capability",
    ),
    COMPLETION_CASES,
)
def test_every_completion_reason_matches_the_frozen_matrix(
    reason_code: str,
    decision: str,
    disposition: str,
    requires_reopen: bool,
    requires_note: bool,
    capability: str | None,
) -> None:
    rule = validate_completion(
        DEFAULT_RULESET,
        decision=decision,
        disposition=disposition,
        reason_code=reason_code,
        reviewer_note="required operational explanation" if requires_note else None,
        reopen_not_before=NOW + timedelta(seconds=1) if requires_reopen else None,
        now=NOW,
    )
    assert rule.decision == decision
    assert rule.disposition == disposition
    assert rule.requires_reopen_not_before is requires_reopen
    assert rule.requires_note is requires_note
    assert rule.required_capability == capability


def test_default_completion_reason_set_has_no_undeclared_codes() -> None:
    expected = {case[0] for case in COMPLETION_CASES}
    assert set(DEFAULT_RULESET.completion_reasons) == expected


@pytest.mark.parametrize("wrong_field", ["decision", "disposition"])
def test_reason_cannot_be_reused_with_a_different_matrix_combination(
    wrong_field: str,
) -> None:
    values = {
        "decision": "needs_more_info",
        "disposition": "nurture",
    }
    values[wrong_field] = (
        "qualified" if wrong_field == "decision" else "archive"
    )
    with pytest.raises(ReviewRulesetError, match="^review_reason_matrix_invalid$"):
        validate_completion(
            DEFAULT_RULESET,
            reason_code="stage_or_deadline_unknown",
            reviewer_note=None,
            reopen_not_before=NOW + timedelta(days=1),
            now=NOW,
            **values,
        )


def test_unknown_completion_reason_fails_closed() -> None:
    with pytest.raises(ReviewRulesetError, match="^review_reason_invalid$"):
        validate_completion(
            DEFAULT_RULESET,
            decision="rejected",
            disposition="archive",
            reason_code="unknown_reason",
            reviewer_note=None,
            reopen_not_before=None,
            now=NOW,
        )


@pytest.mark.parametrize("note", [None, "", "   "])
def test_required_reviewer_note_rejects_missing_or_blank_values(
    note: str | None,
) -> None:
    with pytest.raises(ReviewRulesetError, match="^review_reason_note_required$"):
        validate_completion(
            DEFAULT_RULESET,
            decision="rejected",
            disposition="archive",
            reason_code="other_rejection",
            reviewer_note=note,
            reopen_not_before=None,
            now=NOW,
        )


@pytest.mark.parametrize(
    "reopen_at",
    [None, NOW - timedelta(microseconds=1), NOW],
)
def test_required_reopen_time_must_be_strictly_in_the_future(reopen_at) -> None:
    with pytest.raises(ReviewRulesetError, match="^reopen_not_before_required$"):
        validate_completion(
            DEFAULT_RULESET,
            decision="qualified",
            disposition="nurture",
            reason_code="future_contact_window",
            reviewer_note=None,
            reopen_not_before=reopen_at,
            now=NOW,
        )


def test_archive_reason_rejects_reopen_time() -> None:
    with pytest.raises(ReviewRulesetError, match="^reopen_not_before_not_allowed$"):
        validate_completion(
            DEFAULT_RULESET,
            decision="rejected",
            disposition="archive",
            reason_code="not_selection_or_voting",
            reviewer_note=None,
            reopen_not_before=NOW + timedelta(days=1),
            now=NOW,
        )


def test_reopen_time_requires_timezone_information() -> None:
    with pytest.raises(ReviewRulesetError, match="^reopen_not_before_invalid$"):
        validate_completion(
            DEFAULT_RULESET,
            decision="qualified",
            disposition="nurture",
            reason_code="future_contact_window",
            reviewer_note=None,
            reopen_not_before=datetime(2026, 8, 24, 8, 0),  # noqa: DTZ001
            now=NOW,
        )


def test_sales_handoff_rule_is_capability_gated() -> None:
    rule = DEFAULT_RULESET.completion_reasons["sales_ready_confirmed"]
    assert rule.required_capability == "opportunity_atomic_create"


@pytest.mark.parametrize(
    ("mutator", "error"),
    [
        (
            lambda item: item.__setitem__("unexpected", True),
            "ruleset_shape_invalid",
        ),
        (
            lambda item: item.__setitem__("schema_version", "review-ruleset.v999"),
            "ruleset_schema_unsupported",
        ),
        (
            lambda item: item.__setitem__("completion_reasons", {}),
            "completion_reasons_invalid",
        ),
        (
            lambda item: item["completion_reasons"]["other_rejection"].__setitem__(
                "requires_note", "yes"
            ),
            "completion_reason_flags_invalid",
        ),
        (
            lambda item: item["completion_reasons"][
                "sales_ready_confirmed"
            ].__setitem__("required_capability", "uncontrolled_sales_export"),
            "completion_reason_capability_invalid",
        ),
        (
            lambda item: item["reopen"].__setitem__("max_rounds", 1),
            "reopen_max_rounds_invalid",
        ),
        (
            lambda item: item["reopen"]["reasons"]["scheduled_recheck_due"].__setitem__(
                "requires_new_realtime_source", 1
            ),
            "reopen_reason_invalid",
        ),
        (
            lambda item: item["priority"]["components"].__setitem__(
                "timeliness_stage", 29
            ),
            "priority_component_total_invalid",
        ),
        (
            lambda item: item["priority"].__setitem__(
                "bands",
                [
                    {"code": "high", "minimum": 60, "sla_minutes": 480},
                    {"code": "urgent", "minimum": 80, "sla_minutes": 120},
                    {"code": "low", "minimum": 0, "sla_minutes": None},
                ],
            ),
            "priority_band_order_invalid",
        ),
    ],
)
def test_semantically_invalid_rehashed_rulesets_fail_closed(
    mutator, error: str
) -> None:
    definition = _definition_with(mutator)
    with pytest.raises(ReviewRulesetError, match=f"^{error}$"):
        _parse_rehashed(definition)


def test_definition_tampering_without_a_matching_hash_fails_closed() -> None:
    definition = _definition_with(
        lambda item: item["priority"]["components"].__setitem__(
            "timeliness_stage", 29
        )
    )
    with pytest.raises(ReviewRulesetError, match="^ruleset_hash_mismatch$"):
        parse_ruleset(RULESET_VERSION, DEFAULT_RULESET_SHA256, definition)


@pytest.mark.parametrize(
    ("mutator", "error"),
    [
        (
            lambda item: item["completion_reasons"]["not_selection_or_voting"].update(
                decision="qualified", disposition="archive"
            ),
            "completion_reason_matrix_invalid",
        ),
        (
            lambda item: item["completion_reasons"][
                "sales_ready_confirmed"
            ].__setitem__("required_capability", None),
            "completion_reason_capability_invalid",
        ),
        (
            lambda item: item["completion_reasons"][
                "future_contact_window"
            ].__setitem__("required_capability", "opportunity_atomic_create"),
            "completion_reason_capability_invalid",
        ),
    ],
)
def test_structurally_valid_but_unsafe_completion_rules_fail_closed(
    mutator, error: str
) -> None:
    definition = _definition_with(mutator)
    with pytest.raises(ReviewRulesetError, match=f"^{error}$"):
        _parse_rehashed(definition)


@pytest.mark.parametrize(
    ("total", "expected_band", "expected_sla"),
    [
        ("100", "urgent", 120),
        ("80", "urgent", 120),
        ("79.99", "high", 480),
        ("60", "high", 480),
        ("59.99", "normal", 1440),
        ("40", "normal", 1440),
        ("39.99", "low", None),
        ("0", "low", None),
    ],
)
def test_priority_band_boundaries(
    total: str, expected_band: str, expected_sla: int | None
) -> None:
    scored = score_priority(DEFAULT_RULESET, _priority_components(total))
    assert Decimal(scored["total_score"]) == Decimal(total)
    assert scored["priority_band"] == expected_band
    assert scored["sla_minutes"] == expected_sla
    assert scored["ruleset_version"] == RULESET_VERSION
    assert scored["ruleset_sha256"] == DEFAULT_RULESET_SHA256
    assert scored["scoring_method_version"] == (
        "review-priority-envelope/1.0.0"
    )


def test_priority_components_preserve_score_and_explanation_codes() -> None:
    components = _priority_components("100")
    scored = score_priority(DEFAULT_RULESET, components)
    assert set(scored["components"]) == {
        "timeliness_stage",
        "online_voting_demand",
        "organizer_value",
        "contactability",
        "evidence_quality",
    }
    for code, item in scored["components"].items():
        assert Decimal(item["score"]) == Decimal(components[code]["score"])
        assert item["explanation_code"] == components[code]["explanation_code"]


@pytest.mark.parametrize(
    ("mutator", "error"),
    [
        (
            lambda items: items.pop("evidence_quality"),
            "priority_component_set_invalid",
        ),
        (
            lambda items: items["timeliness_stage"].__setitem__("extra", True),
            "priority_component_shape_invalid",
        ),
        (
            lambda items: items["timeliness_stage"].__setitem__("score", -1),
            "priority_component_score_invalid",
        ),
        (
            lambda items: items["timeliness_stage"].__setitem__("score", 30.01),
            "priority_component_score_invalid",
        ),
        (
            lambda items: items["timeliness_stage"].__setitem__("score", True),
            "priority_component_score_invalid",
        ),
        (
            lambda items: items["timeliness_stage"].__setitem__(
                "explanation_code", ""
            ),
            "priority_explanation_code_invalid",
        ),
        (
            lambda items: items["timeliness_stage"].__setitem__(
                "explanation_code", "Not-Stable"
            ),
            "priority_explanation_code_invalid",
        ),
        (
            lambda items: items["timeliness_stage"].__setitem__(
                "explanation_code", "valid_but_unpublished"
            ),
            "priority_explanation_code_invalid",
        ),
    ],
)
def test_invalid_priority_components_fail_closed(mutator, error: str) -> None:
    components = _priority_components("50")
    mutator(components)
    with pytest.raises(ReviewRulesetError, match=f"^{error}$"):
        score_priority(DEFAULT_RULESET, components)
