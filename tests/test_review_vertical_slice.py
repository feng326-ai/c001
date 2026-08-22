"""Unit contract for the dormant tenant review vertical slice."""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from pydantic import ValidationError

from wxsearch.api import review_routes
from wxsearch.db_connector import TenantPrincipal
from wxsearch.review_service import (
    ReviewInvalidTransition,
    ReviewPermissionDenied,
    ReviewService,
)


KEY = "qa-command-key-0001"


class ScriptedTransaction:
    def __init__(self, *, role="resource_reviewer", query_results=(), write_results=()):
        self.principal = TenantPrincipal(
            user_id=17,
            user_public_id=uuid.uuid4(),
            tenant_id=uuid.uuid4(),
            membership_id=uuid.uuid4(),
            role=role,
        )
        self.query_results = list(query_results)
        self.write_results = list(write_results)
        self.calls = []

    def execute_query(self, query, params=None):
        normalized = " ".join(query.lower().split())
        self.calls.append(("query", normalized, params))
        assert self.query_results, f"unexpected query: {normalized}"
        result = self.query_results.pop(0)
        if (
            result
            and result[0][0] == "$request_hash"
            and hasattr(self, "last_request_hash")
        ):
            result = [(self.last_request_hash, *result[0][1:])]
        return result

    def execute_write(self, query, params=None):
        normalized = " ".join(query.lower().split())
        self.calls.append(("write", normalized, params))
        if "insert into public.tenant_command_receipts" in normalized:
            self.last_request_hash = params[-1]
        return self.write_results.pop(0) if self.write_results else 1


def _current_user():
    return {
        "id": 17,
        "username": "qa-reviewer",
        "role": "member",
        "team_id": None,
        "team_name": "",
    }


def test_start_review_is_reviewer_only_and_uses_fixed_lock_order():
    candidate_id = uuid.uuid4()
    grant_id = uuid.uuid4()
    edition_id = uuid.uuid4()
    transaction = ScriptedTransaction(
        query_results=[
            [("$request_hash", None)],
            [(grant_id,)],
            [(grant_id, edition_id, "policy-v1")],
            [(candidate_id, edition_id, grant_id, "open", 3)],
            [],
        ]
    )
    service = ReviewService()

    result = service.start_review(
        transaction,
        candidate_id=candidate_id,
        expected_candidate_version=3,
        idempotency_key=KEY,
    )

    assert result["candidate_id"] == str(candidate_id)
    assert result["candidate_version"] == 4
    assert result["review_status"] == "in_review"
    sql = [call[1] for call in transaction.calls]
    grant_lock = next(
        i
        for i, item in enumerate(sql)
        if "app_lock_active_review_grant" in item
    )
    candidate_lock = next(
        i
        for i, item in enumerate(sql)
        if "tenant_candidates" in item and "for update" in item
    )
    review_insert = next(
        i for i, item in enumerate(sql) if "insert into public.tenant_reviews" in item
    )
    assert grant_lock < candidate_lock < review_insert

    denied = ScriptedTransaction(role="tenant_admin")
    with pytest.raises(ReviewPermissionDenied):
        service.start_review(
            denied,
            candidate_id=candidate_id,
            expected_candidate_version=3,
            idempotency_key=KEY,
        )
    assert denied.calls == []


def test_idempotency_replay_returns_saved_result_without_business_writes(monkeypatch):
    candidate_id = uuid.uuid4()
    saved = {
        "review_id": str(uuid.uuid4()),
        "candidate_id": str(candidate_id),
        "review_round": 1,
        "review_status": "in_review",
        "review_version": 1,
        "candidate_version": 2,
    }
    transaction = ScriptedTransaction(query_results=[[('b' * 64, saved)]])
    monkeypatch.setattr(
        "wxsearch.review_service.hashlib.sha256",
        lambda _value: SimpleNamespace(hexdigest=lambda: "b" * 64),
    )

    result = ReviewService().start_review(
        transaction,
        candidate_id=candidate_id,
        expected_candidate_version=1,
        idempotency_key=KEY,
    )

    assert result == {**saved, "idempotency_replayed": True}
    assert len(transaction.calls) == 2
    assert "tenant_command_receipts" in transaction.calls[0][1]
    assert "tenant_command_receipts" in transaction.calls[1][1]


def test_complete_uses_grant_candidate_review_lock_order_and_minimal_event():
    review_id = uuid.uuid4()
    candidate_id = uuid.uuid4()
    grant_id = uuid.uuid4()
    edition_id = uuid.uuid4()
    transaction = ScriptedTransaction(
        query_results=[
            [("$request_hash", None)],
            [(candidate_id, grant_id)],
            [(grant_id, edition_id, "policy-v1")],
            [(candidate_id, edition_id, "in_review", 4)],
            [
                (
                    review_id,
                    candidate_id,
                    edition_id,
                    1,
                    "in_review",
                    None,
                    2,
                )
            ],
        ]
    )
    # The service must compare against the database principal, never a body user id.
    transaction.query_results[-1][0] = (
        review_id,
        candidate_id,
        edition_id,
        1,
        "in_review",
        transaction.principal.user_public_id,
        2,
    )

    result = ReviewService().complete_review(
        transaction,
        review_id=review_id,
        expected_review_version=2,
        expected_candidate_version=4,
        decision="rejected",
        disposition="archive",
        idempotency_key=KEY,
        reason_code="not_a_fit",
        reason_schema_version="draft-v1",
        reviewer_note="private note that must not enter the event",
    )

    assert result["review_status"] == "completed"
    assert result["review_version"] == 3
    sql = [call[1] for call in transaction.calls]
    grant_lock = next(
        i for i, item in enumerate(sql) if "app_lock_active_review_grant" in item
    )
    candidate_lock = next(
        i
        for i, item in enumerate(sql)
        if "tenant_candidates" in item and "for update" in item
    )
    review_lock = next(
        i
        for i, item in enumerate(sql)
        if "tenant_reviews" in item and "for update" in item
    )
    assert grant_lock < candidate_lock < review_lock
    outbox_call = next(
        call
        for call in transaction.calls
        if call[0] == "write" and "insert into public.domain_outbox" in call[1]
    )
    event_payload = json.loads(outbox_call[2][-1])
    assert event_payload["review_id"] == str(review_id)
    assert "reviewer_note" not in event_payload
    assert "reason_code" not in event_payload


def test_sales_handoff_is_rejected_until_opportunity_is_atomic():
    transaction = ScriptedTransaction()
    with pytest.raises(
        ReviewInvalidTransition, match="sales_handoff_not_available"
    ):
        ReviewService().complete_review(
            transaction,
            review_id=uuid.uuid4(),
            expected_review_version=1,
            expected_candidate_version=2,
            decision="qualified",
            disposition="sales_handoff",
            idempotency_key=KEY,
        )
    assert transaction.calls == []


def test_review_payloads_forbid_tenant_identity_and_server_fields():
    forbidden = {
        "tenant_id": uuid.uuid4(),
        "reviewer_user_id": uuid.uuid4(),
        "review_status": "completed",
        "completed_at": datetime.now(timezone.utc),
    }
    for field, value in forbidden.items():
        with pytest.raises(ValidationError):
            review_routes.CompleteReviewRequest(
                expected_review_version=1,
                expected_candidate_version=2,
                decision="rejected",
                disposition="archive",
                **{field: value},
            )


def test_review_flag_off_returns_404_before_database_or_scope(monkeypatch):
    monkeypatch.setenv("TENANT_REVIEW_ENABLED", "false")
    monkeypatch.setattr(
        review_routes,
        "DatabaseConnector",
        lambda: pytest.fail("disabled route must not create a database connector"),
    )
    with pytest.raises(HTTPException) as exc_info:
        review_routes.list_tenant_candidates(
            SimpleNamespace(cookies={}),
            None,
            50,
            _current_user(),
        )
    assert exc_info.value.status_code == 404
    assert exc_info.value.detail == "not_found"
