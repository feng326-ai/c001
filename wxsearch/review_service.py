"""Tenant-isolated review workflow over an already authorized transaction."""

from __future__ import annotations

import hashlib
import json
import re
import uuid
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any

from .db_connector import TenantDbTransaction


IDEMPOTENCY_KEY = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{15,127}$")
REVIEWER_ROLE = "resource_reviewer"
READ_ROLES = {"resource_reviewer", "tenant_admin", "readonly_manager"}
DECISION_DISPOSITIONS = {
    "qualified": {"nurture", "competitor_watch"},
    "rejected": {"archive", "competitor_watch"},
    "needs_more_info": {"nurture"},
}


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
        rows = transaction.execute_query(
            """
            SELECT id, event_edition_id, grant_id, candidate_status,
                   score, score_version, generated_at, version
            FROM public.tenant_candidates
            WHERE (%s IS NULL OR candidate_status = %s)
            ORDER BY generated_at DESC, id
            LIMIT %s
            """,
            (status, status, limit),
        )
        return [
            {
                "id": str(row[0]),
                "event_edition_id": str(row[1]),
                "grant_id": str(row[2]),
                "status": row[3],
                "score": None if row[4] is None else str(row[4]),
                "score_version": row[5],
                "generated_at": row[6].isoformat(),
                "version": row[7],
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
                reviewer_user_id, started_at
            ) VALUES (
                %s, %s, %s, %s, %s, %s, 1, 'in_review', %s, NOW()
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
        reason_schema_version: str | None = None,
        reviewer_note: str | None = None,
    ) -> dict[str, Any]:
        self._require_reviewer(transaction)
        review_uuid = _canonical_uuid(review_id)
        review_version = _positive_version(
            expected_review_version, "expected_review_version"
        )
        candidate_version = _positive_version(
            expected_candidate_version, "expected_candidate_version"
        )
        if decision not in DECISION_DISPOSITIONS:
            raise ReviewInvalidInput("review_decision_invalid")
        if disposition == "sales_handoff":
            raise ReviewInvalidTransition("sales_handoff_not_available")
        if disposition not in DECISION_DISPOSITIONS[decision]:
            raise ReviewInvalidInput("review_disposition_invalid")
        cleaned_reason = _clean_optional_text(reason_code, "reason_code", 64)
        cleaned_reason_version = _clean_optional_text(
            reason_schema_version, "reason_schema_version", 64
        )
        cleaned_note = _clean_optional_text(reviewer_note, "reviewer_note", 2000)
        request_payload = {
            "review_id": str(review_uuid),
            "expected_review_version": review_version,
            "expected_candidate_version": candidate_version,
            "decision": decision,
            "disposition": disposition,
            "reason_code": cleaned_reason,
            "reason_schema_version": cleaned_reason_version,
            "reviewer_note": cleaned_note,
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
                   review_status, reviewer_user_id, version
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

        updated_review = transaction.execute_write(
            """
            UPDATE public.tenant_reviews
            SET review_status = 'completed', review_decision = %s,
                disposition = %s, reason_code = %s,
                reason_schema_version = %s, reviewer_note = %s,
                completed_at = NOW(), version = version + 1,
                updated_at = NOW()
            WHERE id = %s AND review_status = 'in_review' AND version = %s
            """,
            (
                decision,
                disposition,
                cleaned_reason,
                cleaned_reason_version,
                cleaned_note,
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
            "decision": decision,
            "disposition": disposition,
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
            "decision": decision,
            "disposition": disposition,
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
