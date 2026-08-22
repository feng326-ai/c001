"""Immutable, versioned resource-review rules and explainable priority scoring."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, InvalidOperation
from itertools import pairwise
from typing import Any

RULESET_VERSION = "review-rules/1.0.0"
RULESET_SCHEMA_VERSION = "review-ruleset.v1"
RULESET_VERSION_PATTERN = re.compile(r"^review-rules/[1-9][0-9]*\.[0-9]+\.[0-9]+$")
CODE_PATTERN = re.compile(r"^[a-z][a-z0-9_]{2,63}$")
DECISIONS = {"qualified", "rejected", "needs_more_info"}
DISPOSITIONS = {"sales_handoff", "nurture", "competitor_watch", "archive"}
KNOWN_CAPABILITIES = {"opportunity_atomic_create"}
PRIORITY_METHOD_PATTERN = re.compile(
    r"^review-priority-envelope/[1-9][0-9]*\.[0-9]+\.[0-9]+$"
)
ALLOWED_COMPLETION_MATRIX = {
    ("qualified", "sales_handoff"),
    ("qualified", "nurture"),
    ("qualified", "competitor_watch"),
    ("rejected", "archive"),
    ("rejected", "competitor_watch"),
    ("needs_more_info", "nurture"),
}


DEFAULT_RULESET_DEFINITION: dict[str, Any] = {
    "schema_version": RULESET_SCHEMA_VERSION,
    "version": RULESET_VERSION,
    "completion_reasons": {
        "sales_ready_confirmed": {
            "decision": "qualified",
            "disposition": "sales_handoff",
            "requires_reopen_not_before": False,
            "requires_note": False,
            "required_capability": "opportunity_atomic_create",
        },
        "future_contact_window": {
            "decision": "qualified",
            "disposition": "nurture",
            "requires_reopen_not_before": True,
            "requires_note": False,
            "required_capability": None,
        },
        "valid_but_not_sales_ready": {
            "decision": "qualified",
            "disposition": "nurture",
            "requires_reopen_not_before": True,
            "requires_note": False,
            "required_capability": None,
        },
        "competitor_present_replaceable": {
            "decision": "qualified",
            "disposition": "competitor_watch",
            "requires_reopen_not_before": True,
            "requires_note": False,
            "required_capability": None,
        },
        "not_selection_or_voting": {
            "decision": "rejected",
            "disposition": "archive",
            "requires_reopen_not_before": False,
            "requires_note": False,
            "required_capability": None,
        },
        "event_ended_or_too_late": {
            "decision": "rejected",
            "disposition": "archive",
            "requires_reopen_not_before": False,
            "requires_note": False,
            "required_capability": None,
        },
        "no_online_voting_need": {
            "decision": "rejected",
            "disposition": "archive",
            "requires_reopen_not_before": False,
            "requires_note": False,
            "required_capability": None,
        },
        "outside_target_policy": {
            "decision": "rejected",
            "disposition": "archive",
            "requires_reopen_not_before": False,
            "requires_note": False,
            "required_capability": None,
        },
        "invalid_or_unverifiable_evidence": {
            "decision": "rejected",
            "disposition": "archive",
            "requires_reopen_not_before": False,
            "requires_note": False,
            "required_capability": None,
        },
        "other_rejection": {
            "decision": "rejected",
            "disposition": "archive",
            "requires_reopen_not_before": False,
            "requires_note": True,
            "required_capability": None,
        },
        "competitor_committed_no_entry": {
            "decision": "rejected",
            "disposition": "competitor_watch",
            "requires_reopen_not_before": True,
            "requires_note": False,
            "required_capability": None,
        },
        "stage_or_deadline_unknown": {
            "decision": "needs_more_info",
            "disposition": "nurture",
            "requires_reopen_not_before": True,
            "requires_note": False,
            "required_capability": None,
        },
        "online_voting_need_unknown": {
            "decision": "needs_more_info",
            "disposition": "nurture",
            "requires_reopen_not_before": True,
            "requires_note": False,
            "required_capability": None,
        },
        "organizer_unconfirmed": {
            "decision": "needs_more_info",
            "disposition": "nurture",
            "requires_reopen_not_before": True,
            "requires_note": False,
            "required_capability": None,
        },
        "contact_missing_or_stale": {
            "decision": "needs_more_info",
            "disposition": "nurture",
            "requires_reopen_not_before": True,
            "requires_note": False,
            "required_capability": None,
        },
        "evidence_conflict": {
            "decision": "needs_more_info",
            "disposition": "nurture",
            "requires_reopen_not_before": True,
            "requires_note": False,
            "required_capability": None,
        },
        "other_missing_information": {
            "decision": "needs_more_info",
            "disposition": "nurture",
            "requires_reopen_not_before": True,
            "requires_note": True,
            "required_capability": None,
        },
    },
    "reopen": {
        "max_rounds": 3,
        "reasons": {
            "scheduled_recheck_due": {"requires_new_realtime_source": False},
            "new_realtime_evidence": {"requires_new_realtime_source": True},
            "missing_information_resolved": {
                "requires_new_realtime_source": True
            },
            "competitor_status_changed": {"requires_new_realtime_source": True},
        },
    },
    "priority": {
        "method_version": "review-priority-envelope/1.0.0",
        "components": {
            "timeliness_stage": 30,
            "online_voting_demand": 25,
            "organizer_value": 20,
            "contactability": 15,
            "evidence_quality": 10,
        },
        "explanation_codes": {
            "timeliness_stage": [
                "timeliness_unknown",
                "timeliness_historical",
                "timeliness_future",
                "timeliness_active",
                "timeliness_urgent",
            ],
            "online_voting_demand": [
                "demand_unknown",
                "demand_negative",
                "demand_indirect",
                "demand_explicit",
            ],
            "organizer_value": [
                "organizer_unknown",
                "organizer_first_seen",
                "organizer_repeat",
                "organizer_strategic",
            ],
            "contactability": [
                "contact_none",
                "contact_stale",
                "contact_indirect",
                "contact_verified",
            ],
            "evidence_quality": [
                "evidence_weak",
                "evidence_single_source",
                "evidence_correlated",
                "evidence_verified",
            ],
        },
        "bands": [
            {"code": "urgent", "minimum": 80, "sla_minutes": 120},
            {"code": "high", "minimum": 60, "sla_minutes": 480},
            {"code": "normal", "minimum": 40, "sla_minutes": 1440},
            {"code": "low", "minimum": 0, "sla_minutes": None},
        ],
    },
}


class ReviewRulesetError(ValueError):
    """The persisted ruleset or a rule-dependent request is invalid."""


@dataclass(frozen=True)
class CompletionRule:
    decision: str
    disposition: str
    requires_reopen_not_before: bool
    requires_note: bool
    required_capability: str | None


@dataclass(frozen=True)
class ReopenRule:
    requires_new_realtime_source: bool


@dataclass(frozen=True)
class PriorityBand:
    code: str
    minimum: Decimal
    sla_minutes: int | None


@dataclass(frozen=True)
class ReviewRuleset:
    version: str
    definition_sha256: str
    definition: dict[str, Any]
    completion_reasons: Mapping[str, CompletionRule]
    reopen_reasons: Mapping[str, ReopenRule]
    max_review_rounds: int
    priority_method_version: str
    priority_components: Mapping[str, Decimal]
    priority_explanation_codes: Mapping[str, frozenset[str]]
    priority_bands: tuple[PriorityBand, ...]


def canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def definition_sha256(definition: Mapping[str, Any]) -> str:
    return hashlib.sha256(canonical_json(definition).encode("utf-8")).hexdigest()


DEFAULT_RULESET_SHA256 = definition_sha256(DEFAULT_RULESET_DEFINITION)


def _require_exact_keys(value: Mapping[str, Any], expected: set[str], field: str) -> None:
    if set(value) != expected:
        raise ReviewRulesetError(f"{field}_shape_invalid")


def _decimal(value: Any, field: str) -> Decimal:
    if isinstance(value, bool):
        raise ReviewRulesetError(f"{field}_invalid")
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as error:
        raise ReviewRulesetError(f"{field}_invalid") from error
    if not parsed.is_finite():
        raise ReviewRulesetError(f"{field}_invalid")
    return parsed


def parse_ruleset(
    version: str,
    expected_sha256: str,
    definition: Mapping[str, Any] | str,
) -> ReviewRuleset:
    if not isinstance(version, str) or not RULESET_VERSION_PATTERN.fullmatch(version):
        raise ReviewRulesetError("ruleset_version_invalid")
    if not isinstance(expected_sha256, str) or not re.fullmatch(
        r"[0-9a-f]{64}", expected_sha256
    ):
        raise ReviewRulesetError("ruleset_hash_invalid")
    if isinstance(definition, str):
        try:
            definition = json.loads(definition)
        except json.JSONDecodeError as error:
            raise ReviewRulesetError("ruleset_definition_invalid") from error
    if not isinstance(definition, dict):
        raise ReviewRulesetError("ruleset_definition_invalid")
    normalized = json.loads(canonical_json(definition))
    if definition_sha256(normalized) != expected_sha256:
        raise ReviewRulesetError("ruleset_hash_mismatch")
    _require_exact_keys(
        normalized,
        {"schema_version", "version", "completion_reasons", "reopen", "priority"},
        "ruleset",
    )
    if normalized["schema_version"] != RULESET_SCHEMA_VERSION:
        raise ReviewRulesetError("ruleset_schema_unsupported")
    if normalized["version"] != version:
        raise ReviewRulesetError("ruleset_version_mismatch")

    completion_raw = normalized["completion_reasons"]
    if not isinstance(completion_raw, dict) or not completion_raw:
        raise ReviewRulesetError("completion_reasons_invalid")
    completion: dict[str, CompletionRule] = {}
    for code, item in completion_raw.items():
        if not isinstance(code, str) or not CODE_PATTERN.fullmatch(code):
            raise ReviewRulesetError("completion_reason_code_invalid")
        if not isinstance(item, dict):
            raise ReviewRulesetError("completion_reason_invalid")
        _require_exact_keys(
            item,
            {
                "decision",
                "disposition",
                "requires_reopen_not_before",
                "requires_note",
                "required_capability",
            },
            "completion_reason",
        )
        decision = item["decision"]
        disposition = item["disposition"]
        if (
            decision not in DECISIONS
            or disposition not in DISPOSITIONS
            or (decision, disposition) not in ALLOWED_COMPLETION_MATRIX
        ):
            raise ReviewRulesetError("completion_reason_matrix_invalid")
        requires_reopen = item["requires_reopen_not_before"]
        requires_note = item["requires_note"]
        capability = item["required_capability"]
        if not isinstance(requires_reopen, bool) or not isinstance(requires_note, bool):
            raise ReviewRulesetError("completion_reason_flags_invalid")
        if capability is not None and capability not in KNOWN_CAPABILITIES:
            raise ReviewRulesetError("completion_reason_capability_invalid")
        expected_capability = (
            "opportunity_atomic_create" if disposition == "sales_handoff" else None
        )
        if capability != expected_capability:
            raise ReviewRulesetError("completion_reason_capability_invalid")
        completion[code] = CompletionRule(
            decision=decision,
            disposition=disposition,
            requires_reopen_not_before=requires_reopen,
            requires_note=requires_note,
            required_capability=capability,
        )

    reopen_raw = normalized["reopen"]
    if not isinstance(reopen_raw, dict):
        raise ReviewRulesetError("reopen_policy_invalid")
    _require_exact_keys(reopen_raw, {"max_rounds", "reasons"}, "reopen")
    max_rounds = reopen_raw["max_rounds"]
    if isinstance(max_rounds, bool) or not isinstance(max_rounds, int):
        raise ReviewRulesetError("reopen_max_rounds_invalid")
    if not 2 <= max_rounds <= 10:
        raise ReviewRulesetError("reopen_max_rounds_invalid")
    reasons_raw = reopen_raw["reasons"]
    if not isinstance(reasons_raw, dict) or not reasons_raw:
        raise ReviewRulesetError("reopen_reasons_invalid")
    reopen: dict[str, ReopenRule] = {}
    for code, item in reasons_raw.items():
        if not isinstance(code, str) or not CODE_PATTERN.fullmatch(code):
            raise ReviewRulesetError("reopen_reason_code_invalid")
        if not isinstance(item, dict):
            raise ReviewRulesetError("reopen_reason_invalid")
        _require_exact_keys(item, {"requires_new_realtime_source"}, "reopen_reason")
        required = item["requires_new_realtime_source"]
        if not isinstance(required, bool):
            raise ReviewRulesetError("reopen_reason_invalid")
        reopen[code] = ReopenRule(requires_new_realtime_source=required)

    priority_raw = normalized["priority"]
    if not isinstance(priority_raw, dict):
        raise ReviewRulesetError("priority_policy_invalid")
    _require_exact_keys(
        priority_raw,
        {"method_version", "components", "explanation_codes", "bands"},
        "priority",
    )
    method_version = priority_raw["method_version"]
    if not isinstance(method_version, str) or not PRIORITY_METHOD_PATTERN.fullmatch(
        method_version
    ):
        raise ReviewRulesetError("priority_method_version_invalid")
    components_raw = priority_raw["components"]
    if not isinstance(components_raw, dict) or not components_raw:
        raise ReviewRulesetError("priority_components_invalid")
    components: dict[str, Decimal] = {}
    for code, maximum in components_raw.items():
        if not isinstance(code, str) or not CODE_PATTERN.fullmatch(code):
            raise ReviewRulesetError("priority_component_code_invalid")
        cap = _decimal(maximum, "priority_component_maximum")
        if cap <= 0:
            raise ReviewRulesetError("priority_component_maximum_invalid")
        components[code] = cap
    if sum(components.values(), Decimal(0)) != Decimal(100):
        raise ReviewRulesetError("priority_component_total_invalid")

    explanations_raw = priority_raw["explanation_codes"]
    if not isinstance(explanations_raw, dict) or set(explanations_raw) != set(
        components
    ):
        raise ReviewRulesetError("priority_explanation_dictionary_invalid")
    explanation_codes: dict[str, frozenset[str]] = {}
    for component_code, raw_codes in explanations_raw.items():
        if (
            not isinstance(raw_codes, list)
            or not raw_codes
            or len(set(raw_codes)) != len(raw_codes)
            or any(
                not isinstance(code, str) or not CODE_PATTERN.fullmatch(code)
                for code in raw_codes
            )
        ):
            raise ReviewRulesetError("priority_explanation_dictionary_invalid")
        explanation_codes[component_code] = frozenset(raw_codes)

    bands_raw = priority_raw["bands"]
    if not isinstance(bands_raw, list) or not bands_raw:
        raise ReviewRulesetError("priority_bands_invalid")
    bands: list[PriorityBand] = []
    for item in bands_raw:
        if not isinstance(item, dict):
            raise ReviewRulesetError("priority_band_invalid")
        _require_exact_keys(item, {"code", "minimum", "sla_minutes"}, "priority_band")
        code = item["code"]
        minimum = _decimal(item["minimum"], "priority_band_minimum")
        sla = item["sla_minutes"]
        if not isinstance(code, str) or not CODE_PATTERN.fullmatch(code):
            raise ReviewRulesetError("priority_band_code_invalid")
        if sla is not None and (
            isinstance(sla, bool) or not isinstance(sla, int) or sla < 1
        ):
            raise ReviewRulesetError("priority_band_sla_invalid")
        bands.append(PriorityBand(code=code, minimum=minimum, sla_minutes=sla))
    if bands[-1].minimum != 0 or any(
        left.minimum <= right.minimum for left, right in pairwise(bands)
    ):
        raise ReviewRulesetError("priority_band_order_invalid")
    if bands[0].minimum > 100 or bands[-1].minimum < 0:
        raise ReviewRulesetError("priority_band_range_invalid")

    return ReviewRuleset(
        version=version,
        definition_sha256=expected_sha256,
        definition=normalized,
        completion_reasons=completion,
        reopen_reasons=reopen,
        max_review_rounds=max_rounds,
        priority_method_version=method_version,
        priority_components=components,
        priority_explanation_codes=explanation_codes,
        priority_bands=tuple(bands),
    )


def validate_completion(
    ruleset: ReviewRuleset,
    *,
    decision: str,
    disposition: str,
    reason_code: str,
    reviewer_note: str | None,
    reopen_not_before: datetime | None,
    now: datetime,
) -> CompletionRule:
    rule = ruleset.completion_reasons.get(reason_code)
    if rule is None:
        raise ReviewRulesetError("review_reason_invalid")
    if rule.decision != decision or rule.disposition != disposition:
        raise ReviewRulesetError("review_reason_matrix_invalid")
    if rule.requires_note and (
        not isinstance(reviewer_note, str) or not reviewer_note.strip()
    ):
        raise ReviewRulesetError("review_reason_note_required")
    if rule.requires_reopen_not_before:
        if (
            reopen_not_before is not None
            and (
                reopen_not_before.tzinfo is None
                or reopen_not_before.utcoffset() is None
            )
        ):
            raise ReviewRulesetError("reopen_not_before_invalid")
        if reopen_not_before is None or reopen_not_before <= now:
            raise ReviewRulesetError("reopen_not_before_required")
    elif reopen_not_before is not None:
        raise ReviewRulesetError("reopen_not_before_not_allowed")
    return rule


def score_priority(
    ruleset: ReviewRuleset,
    components: Mapping[str, Mapping[str, Any]],
) -> dict[str, Any]:
    if set(components) != set(ruleset.priority_components):
        raise ReviewRulesetError("priority_component_set_invalid")
    normalized: dict[str, dict[str, Any]] = {}
    total = Decimal(0)
    for code, maximum in ruleset.priority_components.items():
        item = components[code]
        if not isinstance(item, Mapping) or set(item) != {"score", "explanation_code"}:
            raise ReviewRulesetError("priority_component_shape_invalid")
        score = _decimal(item["score"], "priority_component_score")
        explanation = item["explanation_code"]
        if score < 0 or score > maximum:
            raise ReviewRulesetError("priority_component_score_invalid")
        if explanation not in ruleset.priority_explanation_codes[code]:
            raise ReviewRulesetError("priority_explanation_code_invalid")
        total += score
        normalized[code] = {
            "score": str(score.normalize()),
            "explanation_code": explanation,
        }
    band = next((item for item in ruleset.priority_bands if total >= item.minimum), None)
    if band is None:
        raise ReviewRulesetError("priority_band_missing")
    return {
        "total_score": str(total.normalize()),
        "priority_band": band.code,
        "sla_minutes": band.sla_minutes,
        "components": normalized,
        "scoring_method_version": ruleset.priority_method_version,
        "ruleset_version": ruleset.version,
        "ruleset_sha256": ruleset.definition_sha256,
    }


DEFAULT_RULESET = parse_ruleset(
    RULESET_VERSION,
    DEFAULT_RULESET_SHA256,
    DEFAULT_RULESET_DEFINITION,
)
