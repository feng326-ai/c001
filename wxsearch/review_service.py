"""Tenant-isolated review workflow over an already authorized transaction."""

from __future__ import annotations

import hashlib
import json
import re
import uuid
from dataclasses import dataclass
from datetime import date, datetime, timezone
from typing import Any

from .db_connector import TenantDbTransaction
from .review_rules import (
    ReviewRuleset,
    ReviewRulesetError,
    parse_ruleset,
    validate_completion,
)
from .review_rules import (
    canonical_json as canonical_ruleset_json,
)

IDEMPOTENCY_KEY = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{15,127}$")
REVIEWER_ROLE = "resource_reviewer"
READ_ROLES = {"resource_reviewer", "tenant_admin", "readonly_manager"}


class ReviewServiceError(RuntimeError):
    """Base class for stable route-level error mapping."""


class ReviewNotFound(ReviewServiceError):
    pass


class ReviewPermissionDenied(ReviewServiceError):
    pass


class ReviewConflict(ReviewServiceError):
    pass


class ReviewInvalidTransition(ReviewServiceError):
    pass


class ReviewInvalidInput(ReviewServiceError):
    pass


@dataclass(frozen=True)
class ReceiptState:
    request_hash: str
    replay: dict[str, Any] | None


def _json_default(value):
    if isinstance(value, (uuid.UUID, datetime, date)):
        return str(value)
    raise TypeError(f"unsupported canonical JSON value: {type(value).__name__}")


def _canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=_json_default,
    )


def _canonical_uuid(value: uuid.UUID | str) -> uuid.UUID:
    try:
        parsed = value if isinstance(value, uuid.UUID) else uuid.UUID(str(value))
    except (TypeError, ValueError, AttributeError) as error:
        raise ReviewInvalidInput("invalid_identifier") from error
    if isinstance(value, str) and str(parsed) != value.lower():
        raise ReviewInvalidInput("invalid_identifier")
    return parsed


def _positive_version(value: int, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ReviewInvalidInput(f"{field}_invalid")
    return value


def _clean_optional_text(value: str | None, field: str, maximum: int) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ReviewInvalidInput(f"{field}_invalid")
    cleaned = value.strip()
    if not cleaned or len(cleaned) > maximum or "\x00" in cleaned:
        raise ReviewInvalidInput(f"{field}_invalid")
    return cleaned


def _clean_required_text(value: str | None, field: str, maximum: int) -> str:
    cleaned = _clean_optional_text(value, field, maximum)
    if cleaned is None:
        raise ReviewInvalidInput(f"{field}_required")
    return cleaned


def _normalize_optional_datetime(
    value: datetime | None, field: str
) -> datetime | None:
    if value is None:
        return None
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise ReviewInvalidInput(f"{field}_invalid")
    return value.astimezone(timezone.utc)


def _json_object(value) -> dict:
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            decoded = json.loads(value)
        except json.JSONDecodeError as error:
            raise ReviewConflict("idempotency_response_invalid") from error
        if isinstance(decoded, dict):
            return decoded
    raise ReviewConflict("idempotency_response_invalid")


class ReviewService:
    """State transitions that never borrow a second database connection."""

    @staticmethod
    def _require_read_role(transaction: TenantDbTransaction) -> None:
        if transaction.principal.role not in READ_ROLES:
            raise ReviewPermissionDenied("review_read_forbidden")

    @staticmethod
    def _require_reviewer(transaction: TenantDbTransaction) -> None:
        if transaction.principal.role != REVIEWER_ROLE:
            raise ReviewPermissionDenied("review_write_forbidden")

    @staticmethod
    def _parse_ruleset_rows(rows) -> tuple[uuid.UUID, ReviewRuleset]:
        if len(rows) != 1:
            raise ReviewInvalidTransition("review_ruleset_unavailable")
        activation_id, version, definition_hash, definition = rows[0]
        try:
            parsed_activation = _canonical_uuid(activation_id)
            ruleset = parse_ruleset(version, definition_hash, definition)
        except (ReviewInvalidInput, ReviewRulesetError) as error:
            raise ReviewConflict("review_ruleset_invalid") from error
        return parsed_activation, ruleset

    @classmethod
    def _active_ruleset(
        cls,
        transaction: TenantDbTransaction,
        *,
        lock: bool,
    ) -> tuple[uuid.UUID, ReviewRuleset]:
        if lock:
            rows = transaction.execute_query(
                """
                SELECT activation_id, ruleset_version,
                       ruleset_sha256, definition
                FROM public.app_lock_active_review_ruleset(%s)
                """,
                (str(transaction.principal.tenant_id),),
            )
        else:
            rows = transaction.execute_query(
                """
                SELECT activation.id, ruleset.version,
                       ruleset.definition_sha256, ruleset.definition
                FROM public.tenant_review_ruleset_activations AS activation
                JOIN public.review_rulesets AS ruleset
                  ON ruleset.version = activation.ruleset_version
                 AND ruleset.definition_sha256 = activation.ruleset_sha256
                WHERE activation.tenant_id = %s
                  AND activation.deactivated_at IS NULL
                """,
                (str(transaction.principal.tenant_id),),
            )
        return cls._parse_ruleset_rows(rows)

    @staticmethod
    def _reserve_receipt(
        transaction: TenantDbTransaction,
        *,
        command_name: str,
        idempotency_key: str,
        request_payload: dict[str, Any],
    ) -> ReceiptState:
        if not isinstance(idempotency_key, str) or not IDEMPOTENCY_KEY.fullmatch(
            idempotency_key
        ):
            raise ReviewInvalidInput("idempotency_key_invalid")
        request_hash = hashlib.sha256(
            _canonical_json(request_payload).encode("utf-8")
        ).hexdigest()
        transaction.execute_write(
            """
            INSERT INTO public.tenant_command_receipts (
                tenant_id, command_name, idempotency_key, request_hash
            ) VALUES (%s, %s, %s, %s)
            ON CONFLICT (tenant_id, command_name, idempotency_key) DO NOTHING
            """,
            (
                str(transaction.principal.tenant_id),
                command_name,
                idempotency_key,
                request_hash,
            ),
        )
        rows = transaction.execute_query(
            """
            SELECT request_hash, response_json
            FROM public.tenant_command_receipts
            WHERE tenant_id = %s
              AND command_name = %s
              AND idempotency_key = %s
            FOR UPDATE
            """,
            (
                str(transaction.principal.tenant_id),
                command_name,
                idempotency_key,
            ),
        )
        if len(rows) != 1:
            raise ReviewConflict("idempotency_receipt_missing")
        stored_hash, response_json = rows[0]
        if stored_hash != request_hash:
            raise ReviewConflict("idempotency_key_reused")
        return ReceiptState(
            request_hash=request_hash,
            replay=None if response_json is None else _json_object(response_json),
        )

    @staticmethod
    def _save_receipt(
        transaction: TenantDbTransaction,
        *,
        command_name: str,
        idempotency_key: str,
        request_hash: str,
        response: dict[str, Any],
    ) -> None:
        updated = transaction.execute_write(
            """
            UPDATE public.tenant_command_receipts
            SET response_json = %s::jsonb, updated_at = NOW()
            WHERE tenant_id = %s
              AND command_name = %s
              AND idempotency_key = %s
              AND request_hash = %s
              AND response_json IS NULL
            """,
            (
                _canonical_json(response),
                str(transaction.principal.tenant_id),
                command_name,
                idempotency_key,
                request_hash,
            ),
        )
        if updated != 1:
            raise ReviewConflict("idempotency_receipt_conflict")

    def list_candidates(
        self,
        transaction: TenantDbTransaction,
        *,
        status: str | None = None,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        self._require_read_role(transaction)
        if status is not None and status not in {
            "open",
            "in_review",
            "closed",
            "withdrawn",
        }:
            raise ReviewInvalidInput("candidate_status_invalid")
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 100:
            raise ReviewInvalidInput("limit_invalid")
        _, ruleset = self._active_ruleset(transaction, lock=False)
        rows = transaction.execute_query(
            """
            SELECT id, event_edition_id, grant_id, candidate_status,
                   generated_at, version,
                   score_snapshot.total_score,
                   score_snapshot.priority_band,
                   score_snapshot.scoring_method_version,
                   score_snapshot.component_scores,
                   score_snapshot.score_as_of
            FROM public.tenant_candidates AS candidate
            LEFT JOIN LATERAL (
                SELECT total_score, priority_band, scoring_method_version,
                       component_scores, score_as_of
                FROM public.tenant_candidate_score_snapshots
                WHERE tenant_id = candidate.tenant_id
                  AND candidate_id = candidate.id
                  AND ruleset_version = %s
                  AND ruleset_sha256 = %s
                ORDER BY score_as_of DESC, id DESC
                LIMIT 1
            ) AS score_snapshot ON TRUE
            WHERE (%s IS NULL OR candidate_status = %s)
            ORDER BY
                CASE score_snapshot.priority_band
                    WHEN 'urgent' THEN 1
                    WHEN 'high' THEN 2
                    WHEN 'normal' THEN 3
                    WHEN 'low' THEN 4
                    ELSE 5
                END,
                score_snapshot.total_score DESC NULLS LAST,
                generated_at, id
            LIMIT %s
            """,
            (
                ruleset.version,
                ruleset.definition_sha256,
                status,
                status,
                limit,
            ),
        )
        return [
            {
                "id": str(row[0]),
                "event_edition_id": str(row[1]),
                "grant_id": str(row[2]),
                "status": row[3],
                "generated_at": row[4].isoformat(),
                "version": row[5],
                "score": None if row[6] is None else str(row[6]),
                "priority_band": row[7] or "unscored",
                "scoring_method_version": row[8],
                "score_components": row[9],
                "score_as_of": None if row[10] is None else row[10].isoformat(),
                "ruleset_version": ruleset.version,
                "ruleset_sha256": ruleset.definition_sha256,
            }
            for row in rows
        ]

    def start_review(
        self,
        transaction: TenantDbTransaction,
        *,
        candidate_id: uuid.UUID | str,
        expected_candidate_version: int,
        idempotency_key: str,
    ) -> dict[str, Any]:
        self._require_reviewer(transaction)
        candidate_uuid = _canonical_uuid(candidate_id)
        expected_version = _positive_version(
            expected_candidate_version, "expected_candidate_version"
        )
        request_payload = {
            "candidate_id": str(candidate_uuid),
            "expected_candidate_version": expected_version,
        }
        receipt = self._reserve_receipt(
            transaction,
            command_name="review.start.v1",
            idempotency_key=idempotency_key,
            request_payload=request_payload,
        )
        if receipt.replay is not None:
            return {**receipt.replay, "idempotency_replayed": True}

        lookup = transaction.execute_query(
            "SELECT grant_id FROM public.tenant_candidates WHERE id = %s",
            (str(candidate_uuid),),
        )
        if len(lookup) != 1:
            raise ReviewNotFound("candidate_not_found")
        grant_id = lookup[0][0]
        grant_rows = transaction.execute_query(
            """
            SELECT grant_id, event_edition_id, policy_version
            FROM public.app_lock_active_review_grant(%s, %s)
            """,
            (
                str(transaction.principal.tenant_id),
                str(grant_id),
            ),
        )
        if len(grant_rows) != 1:
            raise ReviewInvalidTransition("grant_inactive")
        grant = grant_rows[0]
        activation_id, ruleset = self._active_ruleset(transaction, lock=True)

        candidate_rows = transaction.execute_query(
            """
            SELECT id, event_edition_id, grant_id, candidate_status, version
            FROM public.tenant_candidates
            WHERE id = %s
            FOR UPDATE
            """,
            (str(candidate_uuid),),
        )
        if len(candidate_rows) != 1:
            raise ReviewNotFound("candidate_not_found")
        candidate = candidate_rows[0]
        if str(candidate[2]) != str(grant[0]) or str(candidate[1]) != str(grant[1]):
            raise ReviewConflict("candidate_grant_changed")
        if candidate[4] != expected_version:
            raise ReviewConflict("candidate_version_conflict")
        if candidate[3] != "open":
            raise ReviewInvalidTransition("candidate_not_open")
        existing = transaction.execute_query(
            """
            SELECT id FROM public.tenant_reviews
            WHERE candidate_id = %s
            LIMIT 1
            """,
            (str(candidate_uuid),),
        )
        if existing:
            raise ReviewInvalidTransition("candidate_already_reviewed")

        review_id = uuid.uuid4()
        transaction.execute_write(
            """
            INSERT INTO public.tenant_reviews (
                id, tenant_id, candidate_id, event_edition_id, grant_id,
                grant_policy_version, review_round, review_status,
                reviewer_user_id, started_at, rule_activation_id,
                rule_version, rule_definition_sha256, rule_snapshot
            ) VALUES (
                %s, %s, %s, %s, %s, %s, 1, 'in_review', %s, NOW(),
                %s, %s, %s, %s::jsonb
            )
            """,
            (
                str(review_id),
                str(transaction.principal.tenant_id),
                str(candidate_uuid),
                str(candidate[1]),
                str(candidate[2]),
                grant[2],
                str(transaction.principal.user_public_id),
                str(activation_id),
                ruleset.version,
                ruleset.definition_sha256,
                canonical_ruleset_json(ruleset.definition),
            ),
        )
        updated = transaction.execute_write(
            """
            UPDATE public.tenant_candidates
            SET candidate_status = 'in_review', version = version + 1,
                updated_at = NOW()
            WHERE id = %s AND candidate_status = 'open' AND version = %s
            """,
            (str(candidate_uuid), expected_version),
        )
        if updated != 1:
            raise ReviewConflict("candidate_version_conflict")
        response = {
            "review_id": str(review_id),
            "candidate_id": str(candidate_uuid),
            "review_round": 1,
            "review_status": "in_review",
            "review_version": 1,
            "candidate_version": expected_version + 1,
            "ruleset_version": ruleset.version,
            "ruleset_sha256": ruleset.definition_sha256,
            "idempotency_replayed": False,
        }
        stored_response = {key: value for key, value in response.items() if key != "idempotency_replayed"}
        self._save_receipt(
            transaction,
            command_name="review.start.v1",
            idempotency_key=idempotency_key,
            request_hash=receipt.request_hash,
            response=stored_response,
        )
        return response

    def reopen_review(
        self,
        transaction: TenantDbTransaction,
        *,
        candidate_id: uuid.UUID | str,
        previous_review_id: uuid.UUID | str,
        expected_candidate_version: int,
        expected_review_version: int,
        reopen_reason_code: str,
        idempotency_key: str,
        trigger_source_document_id: uuid.UUID | str | None = None,
    ) -> dict[str, Any]:
        self._require_reviewer(transaction)
        candidate_uuid = _canonical_uuid(candidate_id)
        previous_uuid = _canonical_uuid(previous_review_id)
        candidate_version = _positive_version(
            expected_candidate_version, "expected_candidate_version"
        )
        review_version = _positive_version(
            expected_review_version, "expected_review_version"
        )
        cleaned_reason = _clean_required_text(
            reopen_reason_code, "reopen_reason_code", 64
        )
        source_uuid = (
            None
            if trigger_source_document_id is None
            else _canonical_uuid(trigger_source_document_id)
        )
        request_payload = {
            "candidate_id": str(candidate_uuid),
            "previous_review_id": str(previous_uuid),
            "expected_candidate_version": candidate_version,
            "expected_review_version": review_version,
            "reopen_reason_code": cleaned_reason,
            "trigger_source_document_id": (
                None if source_uuid is None else str(source_uuid)
            ),
        }
        receipt = self._reserve_receipt(
            transaction,
            command_name="review.reopen.v1",
            idempotency_key=idempotency_key,
            request_payload=request_payload,
        )
        if receipt.replay is not None:
            return {**receipt.replay, "idempotency_replayed": True}

        locator = transaction.execute_query(
            "SELECT grant_id FROM public.tenant_candidates WHERE id = %s",
            (str(candidate_uuid),),
        )
        if len(locator) != 1:
            raise ReviewNotFound("candidate_not_found")
        grant_rows = transaction.execute_query(
            """
            SELECT grant_id, event_edition_id, policy_version
            FROM public.app_lock_active_review_grant(%s, %s)
            """,
            (
                str(transaction.principal.tenant_id),
                str(locator[0][0]),
            ),
        )
        if len(grant_rows) != 1:
            raise ReviewInvalidTransition("grant_inactive")
        grant = grant_rows[0]
        activation_id, ruleset = self._active_ruleset(transaction, lock=True)

        candidates = transaction.execute_query(
            """
            SELECT id, event_edition_id, grant_id, candidate_status, version
            FROM public.tenant_candidates
            WHERE id = %s
            FOR UPDATE
            """,
            (str(candidate_uuid),),
        )
        previous_rows = transaction.execute_query(
            """
            SELECT id, candidate_id, event_edition_id, review_round,
                   review_status, version, completed_at, reopen_not_before
            FROM public.tenant_reviews
            WHERE id = %s
            FOR UPDATE
            """,
            (str(previous_uuid),),
        )
        if len(candidates) != 1 or len(previous_rows) != 1:
            raise ReviewNotFound("review_resource_not_found")
        candidate = candidates[0]
        previous = previous_rows[0]
        if (
            str(candidate[1]) != str(grant[1])
            or str(candidate[2]) != str(grant[0])
            or str(previous[1]) != str(candidate[0])
            or str(previous[2]) != str(candidate[1])
        ):
            raise ReviewConflict("review_candidate_mismatch")
        if candidate[4] != candidate_version or previous[5] != review_version:
            raise ReviewConflict("review_version_conflict")
        if candidate[3] != "closed" or previous[4] != "completed":
            raise ReviewInvalidTransition("review_not_reopenable")

        latest = transaction.execute_query(
            """
            SELECT COALESCE(MAX(review_round), 0)
            FROM public.tenant_reviews
            WHERE candidate_id = %s
            """,
            (str(candidate_uuid),),
        )
        if len(latest) != 1 or latest[0][0] != previous[3]:
            raise ReviewInvalidTransition("review_not_latest")
        if previous[3] >= ruleset.max_review_rounds:
            raise ReviewInvalidTransition("review_round_limit_reached")
        reopen_rule = ruleset.reopen_reasons.get(cleaned_reason)
        if reopen_rule is None:
            raise ReviewInvalidInput("reopen_reason_invalid")

        now_rows = transaction.execute_query("SELECT NOW()", ())
        if len(now_rows) != 1:
            raise ReviewConflict("database_clock_unavailable")
        if cleaned_reason == "scheduled_recheck_due":
            if source_uuid is not None:
                raise ReviewInvalidInput("reopen_source_not_allowed")
            if previous[7] is None or previous[7] > now_rows[0][0]:
                raise ReviewInvalidTransition("scheduled_recheck_not_due")
        elif reopen_rule.requires_new_realtime_source:
            if source_uuid is None:
                raise ReviewInvalidInput("reopen_source_required")
            evidence = transaction.execute_query(
                """
                SELECT 1
                FROM public.event_sources
                WHERE event_edition_id = %s
                  AND source_document_id = %s
                  AND collection_mode = 'realtime_signal'
                  AND linked_at > %s
                """,
                (str(candidate[1]), str(source_uuid), previous[6]),
            )
            if len(evidence) != 1:
                raise ReviewInvalidTransition("new_realtime_evidence_missing")
        else:
            raise ReviewInvalidInput("reopen_reason_invalid")

        next_round = previous[3] + 1
        new_review_id = uuid.uuid4()
        transaction.execute_write(
            """
            INSERT INTO public.tenant_reviews (
                id, tenant_id, candidate_id, event_edition_id, grant_id,
                grant_policy_version, review_round, review_status,
                reviewer_user_id, started_at, supersedes_review_id,
                rule_activation_id, rule_version, rule_definition_sha256,
                rule_snapshot, reopen_reason_code,
                reopen_trigger_source_document_id
            ) VALUES (
                %s, %s, %s, %s, %s, %s, %s, 'in_review', %s, NOW(), %s,
                %s, %s, %s, %s::jsonb, %s, %s
            )
            """,
            (
                str(new_review_id),
                str(transaction.principal.tenant_id),
                str(candidate_uuid),
                str(candidate[1]),
                str(candidate[2]),
                grant[2],
                next_round,
                str(transaction.principal.user_public_id),
                str(previous_uuid),
                str(activation_id),
                ruleset.version,
                ruleset.definition_sha256,
                canonical_ruleset_json(ruleset.definition),
                cleaned_reason,
                None if source_uuid is None else str(source_uuid),
            ),
        )
        updated_candidate = transaction.execute_write(
            """
            UPDATE public.tenant_candidates
            SET candidate_status = 'in_review', version = version + 1,
                updated_at = NOW()
            WHERE id = %s AND candidate_status = 'closed' AND version = %s
            """,
            (str(candidate_uuid), candidate_version),
        )
        if updated_candidate != 1:
            raise ReviewConflict("candidate_version_conflict")

        message_id = uuid.uuid4()
        event_payload = {
            "review_id": str(new_review_id),
            "candidate_id": str(candidate_uuid),
            "event_edition_id": str(candidate[1]),
            "review_round": next_round,
            "supersedes_review_id": str(previous_uuid),
            "reopen_reason_code": cleaned_reason,
            "trigger_source_document_id": (
                None if source_uuid is None else str(source_uuid)
            ),
            "rule_version": ruleset.version,
        }
        transaction.execute_write(
            """
            INSERT INTO public.domain_outbox (
                message_id, tenant_id, event_type, schema_version,
                aggregate_type, aggregate_id, aggregate_version,
                correlation_id, causation_id, payload
            ) VALUES (
                %s, %s, 'review.reopened.v1', '1.0',
                'tenant_review', %s, 1, %s, %s, %s::jsonb
            )
            """,
            (
                str(message_id),
                str(transaction.principal.tenant_id),
                str(new_review_id),
                str(message_id),
                str(previous_uuid),
                _canonical_json(event_payload),
            ),
        )
        response = {
            "review_id": str(new_review_id),
            "candidate_id": str(candidate_uuid),
            "supersedes_review_id": str(previous_uuid),
            "review_round": next_round,
            "review_status": "in_review",
            "review_version": 1,
            "candidate_version": candidate_version + 1,
            "reopen_reason_code": cleaned_reason,
            "ruleset_version": ruleset.version,
            "ruleset_sha256": ruleset.definition_sha256,
            "message_id": str(message_id),
            "idempotency_replayed": False,
        }
        stored_response = {
            key: value
            for key, value in response.items()
            if key != "idempotency_replayed"
        }
        self._save_receipt(
            transaction,
            command_name="review.reopen.v1",
            idempotency_key=idempotency_key,
            request_hash=receipt.request_hash,
            response=stored_response,
        )
        return response

    def complete_review(
        self,
        transaction: TenantDbTransaction,
        *,
        review_id: uuid.UUID | str,
        expected_review_version: int,
        expected_candidate_version: int,
        decision: str,
        disposition: str,
        idempotency_key: str,
        reason_code: str | None = None,
        reviewer_note: str | None = None,
        reopen_not_before: datetime | None = None,
    ) -> dict[str, Any]:
        self._require_reviewer(transaction)
        review_uuid = _canonical_uuid(review_id)
        review_version = _positive_version(
            expected_review_version, "expected_review_version"
        )
        candidate_version = _positive_version(
            expected_candidate_version, "expected_candidate_version"
        )
        cleaned_decision = _clean_required_text(decision, "review_decision", 64)
        cleaned_disposition = _clean_required_text(
            disposition, "review_disposition", 64
        )
        cleaned_reason = _clean_required_text(reason_code, "reason_code", 64)
        cleaned_note = _clean_optional_text(reviewer_note, "reviewer_note", 2000)
        normalized_reopen_at = _normalize_optional_datetime(
            reopen_not_before, "reopen_not_before"
        )
        request_payload = {
            "review_id": str(review_uuid),
            "expected_review_version": review_version,
            "expected_candidate_version": candidate_version,
            "decision": cleaned_decision,
            "disposition": cleaned_disposition,
            "reason_code": cleaned_reason,
            "reviewer_note": cleaned_note,
            "reopen_not_before": normalized_reopen_at,
        }
        receipt = self._reserve_receipt(
            transaction,
            command_name="review.complete.v1",
            idempotency_key=idempotency_key,
            request_payload=request_payload,
        )
        if receipt.replay is not None:
            return {**receipt.replay, "idempotency_replayed": True}

        locator = transaction.execute_query(
            """
            SELECT candidate_id, grant_id
            FROM public.tenant_reviews
            WHERE id = %s
            """,
            (str(review_uuid),),
        )
        if len(locator) != 1:
            raise ReviewNotFound("review_not_found")
        candidate_id, grant_id = locator[0]
        grants = transaction.execute_query(
            """
            SELECT grant_id, event_edition_id, policy_version
            FROM public.app_lock_active_review_grant(%s, %s)
            """,
            (
                str(transaction.principal.tenant_id),
                str(grant_id),
            ),
        )
        if len(grants) != 1:
            raise ReviewInvalidTransition("grant_inactive")
        candidates = transaction.execute_query(
            """
            SELECT id, event_edition_id, candidate_status, version
            FROM public.tenant_candidates
            WHERE id = %s
            FOR UPDATE
            """,
            (str(candidate_id),),
        )
        reviews = transaction.execute_query(
            """
            SELECT id, candidate_id, event_edition_id, review_round,
                   review_status, reviewer_user_id, version,
                   rule_activation_id, rule_version,
                   rule_definition_sha256, rule_snapshot
            FROM public.tenant_reviews
            WHERE id = %s
            FOR UPDATE
            """,
            (str(review_uuid),),
        )
        if len(candidates) != 1 or len(reviews) != 1:
            raise ReviewNotFound("review_not_found")
        candidate = candidates[0]
        review = reviews[0]
        if str(review[1]) != str(candidate[0]) or str(review[2]) != str(candidate[1]):
            raise ReviewConflict("review_candidate_mismatch")
        if candidate[3] != candidate_version or review[6] != review_version:
            raise ReviewConflict("review_version_conflict")
        if candidate[2] != "in_review" or review[4] != "in_review":
            raise ReviewInvalidTransition("review_not_in_progress")
        if str(review[5]) != str(transaction.principal.user_public_id):
            raise ReviewPermissionDenied("reviewer_mismatch")

        try:
            ruleset = parse_ruleset(review[8], review[9], review[10])
        except ReviewRulesetError as error:
            raise ReviewConflict("review_ruleset_invalid") from error
        now_rows = transaction.execute_query("SELECT NOW()", ())
        if len(now_rows) != 1:
            raise ReviewConflict("database_clock_unavailable")
        try:
            completion_rule = validate_completion(
                ruleset,
                decision=cleaned_decision,
                disposition=cleaned_disposition,
                reason_code=cleaned_reason,
                reviewer_note=cleaned_note,
                reopen_not_before=normalized_reopen_at,
                now=now_rows[0][0],
            )
        except ReviewRulesetError as error:
            raise ReviewInvalidInput(str(error)) from error
        if completion_rule.required_capability is not None:
            raise ReviewInvalidTransition("sales_handoff_not_available")

        updated_review = transaction.execute_write(
            """
            UPDATE public.tenant_reviews
            SET review_status = 'completed', review_decision = %s,
                disposition = %s, reason_code = %s,
                reason_schema_version = %s, reviewer_note = %s,
                reopen_not_before = %s,
                completed_at = NOW(), version = version + 1,
                updated_at = NOW()
            WHERE id = %s AND review_status = 'in_review' AND version = %s
            """,
            (
                cleaned_decision,
                cleaned_disposition,
                cleaned_reason,
                ruleset.version,
                cleaned_note,
                normalized_reopen_at,
                str(review_uuid),
                review_version,
            ),
        )
        updated_candidate = transaction.execute_write(
            """
            UPDATE public.tenant_candidates
            SET candidate_status = 'closed', version = version + 1,
                updated_at = NOW()
            WHERE id = %s AND candidate_status = 'in_review' AND version = %s
            """,
            (str(candidate_id), candidate_version),
        )
        if updated_review != 1 or updated_candidate != 1:
            raise ReviewConflict("review_version_conflict")

        next_review_version = review_version + 1
        message_id = uuid.uuid4()
        event_payload = {
            "review_id": str(review_uuid),
            "candidate_id": str(candidate_id),
            "event_edition_id": str(candidate[1]),
            "review_round": review[3],
            "decision": cleaned_decision,
            "disposition": cleaned_disposition,
            "reason_code": cleaned_reason,
            "rule_version": ruleset.version,
            "reopen_not_before": (
                None
                if normalized_reopen_at is None
                else normalized_reopen_at.isoformat()
            ),
        }
        transaction.execute_write(
            """
            INSERT INTO public.domain_outbox (
                message_id, tenant_id, event_type, schema_version,
                aggregate_type, aggregate_id, aggregate_version,
                correlation_id, causation_id, payload
            ) VALUES (
                %s, %s, 'review.completed.v1', '1.0',
                'tenant_review', %s, %s, %s, NULL, %s::jsonb
            )
            """,
            (
                str(message_id),
                str(transaction.principal.tenant_id),
                str(review_uuid),
                next_review_version,
                str(message_id),
                _canonical_json(event_payload),
            ),
        )
        response = {
            "review_id": str(review_uuid),
            "candidate_id": str(candidate_id),
            "review_status": "completed",
            "decision": cleaned_decision,
            "disposition": cleaned_disposition,
            "reason_code": cleaned_reason,
            "ruleset_version": ruleset.version,
            "ruleset_sha256": ruleset.definition_sha256,
            "reopen_not_before": (
                None
                if normalized_reopen_at is None
                else normalized_reopen_at.isoformat()
            ),
            "review_version": next_review_version,
            "candidate_version": candidate_version + 1,
            "message_id": str(message_id),
            "idempotency_replayed": False,
        }
        stored_response = {key: value for key, value in response.items() if key != "idempotency_replayed"}
        self._save_receipt(
            transaction,
            command_name="review.complete.v1",
            idempotency_key=idempotency_key,
            request_hash=receipt.request_hash,
            response=stored_response,
        )
        return response
