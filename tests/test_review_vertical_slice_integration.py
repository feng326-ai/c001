"""Real PostgreSQL 15 proof for review RLS, strict revoke and atomic outbox."""

from __future__ import annotations

import os
import threading
import time
import uuid
from datetime import datetime, timedelta, timezone
from urllib.parse import quote, urlsplit, urlunsplit

import pytest

from wxsearch.db_connector import DatabaseConnector, TenantAccessDenied
from wxsearch.review_service import (
    ReviewConflict,
    ReviewInvalidInput,
    ReviewInvalidTransition,
    ReviewNotFound,
    ReviewService,
)
from wxsearch.runtime_db_role import RuntimeRoleSettings, provision_runtime_role

RUN = os.getenv("RUN_MIGRATION_INTEGRATION") == "1"


def _guarded_qa_database_url() -> str:
    if (
        os.getenv("ENVIRONMENT") != "qa"
        or os.getenv("ALLOW_DESTRUCTIVE_TESTS") != "1"
    ):
        raise RuntimeError("review integration requires guarded QA environment")
    database_url = os.environ["DATABASE_URL"]
    if "_lease_qa_" not in urlsplit(database_url).path:
        raise RuntimeError("review integration refused a non-QA database")
    return database_url


def _role_url(admin_url: str, role_name: str, password: str) -> str:
    parsed = urlsplit(admin_url)
    host = parsed.hostname or "localhost"
    port = f":{parsed.port}" if parsed.port else ""
    netloc = f"{quote(role_name)}:{quote(password)}@{host}{port}"
    return urlunsplit(
        (parsed.scheme, netloc, parsed.path, parsed.query, parsed.fragment)
    )


@pytest.mark.skipif(not RUN, reason="requires disposable QA PostgreSQL 15")
def test_review_vertical_slice_is_isolated_atomic_and_revoke_locked():
    import psycopg2
    from psycopg2 import errors, pool, sql

    admin_url = _guarded_qa_database_url()
    suffix = uuid.uuid4().hex
    role_name = f"qa_review_runtime_{suffix[:12]}"
    role_password = f"Qa-review-{uuid.uuid4().hex}"
    runtime_url = _role_url(admin_url, role_name, role_password)
    tenant_a, tenant_b = uuid.uuid4(), uuid.uuid4()
    reviewer_public_id = uuid.uuid4()
    membership_id = uuid.uuid4()
    source_realtime, source_historical, source_reopen, source_wrong_edition = (
        uuid.uuid4(),
        uuid.uuid4(),
        uuid.uuid4(),
        uuid.uuid4(),
    )
    edition_id, wrong_edition_id = uuid.uuid4(), uuid.uuid4()
    grant_a, grant_b = uuid.uuid4(), uuid.uuid4()
    candidate_a, candidate_b = uuid.uuid4(), uuid.uuid4()
    reviewer_legacy_id = None
    connector = None
    admin = psycopg2.connect(admin_url)
    try:
        with admin.cursor() as cursor:
            cursor.execute("SHOW server_version_num")
            assert 150000 <= int(cursor.fetchone()[0]) < 160000
            cursor.execute(
                "SELECT to_regprocedure("
                "'public.app_authorize_tenant_write(integer,uuid)')"
            )
            assert cursor.fetchone()[0] is not None
            cursor.execute(
                """
                INSERT INTO public.tenants(id, code, name, status)
                VALUES (%s, %s, 'QA Tenant A', 'active'),
                       (%s, %s, 'QA Tenant B', 'active')
                """,
                (
                    str(tenant_a),
                    f"qa-review-a-{suffix}",
                    str(tenant_b),
                    f"qa-review-b-{suffix}",
                ),
            )
            cursor.execute(
                """
                INSERT INTO public.users(
                    public_id, username, password_hash, role, enabled
                ) VALUES (%s, %s, 'qa-non-secret-hash', 'member', TRUE)
                RETURNING id
                """,
                (str(reviewer_public_id), f"qa-reviewer-{suffix}"),
            )
            reviewer_legacy_id = cursor.fetchone()[0]
            cursor.execute(
                """
                INSERT INTO public.tenant_memberships(
                    id, tenant_id, user_id, role, status
                ) VALUES (%s, %s, %s, 'resource_reviewer', 'active')
                """,
                (
                    str(membership_id),
                    str(tenant_a),
                    str(reviewer_public_id),
                ),
            )
            cursor.execute(
                """
                INSERT INTO public.tenant_review_ruleset_activations(
                    tenant_id, ruleset_version, ruleset_sha256,
                    activation_reference
                )
                SELECT tenant_id, ruleset.version, ruleset.definition_sha256,
                       'qa-explicit-activation'
                FROM unnest(%s::uuid[]) AS tenant_id
                CROSS JOIN public.review_rulesets AS ruleset
                WHERE ruleset.version = 'review-rules/1.0.0'
                """,
                ([str(tenant_a), str(tenant_b)],),
            )
            cursor.execute(
                """
                INSERT INTO public.source_documents(
                    id, source_channel, source_key, collection_mode,
                    content_sha256, observed_at
                ) VALUES
                    (%s, 'qa', %s, 'realtime_signal', %s, NOW()),
                    (%s, 'qa', %s, 'historical_backfill', %s, NOW())
                """,
                (
                    str(source_realtime),
                    f"live-{suffix}",
                    "1" * 64,
                    str(source_historical),
                    f"history-{suffix}",
                    "2" * 64,
                ),
            )
            cursor.execute(
                """
                INSERT INTO public.event_editions(
                    id, canonical_name, normalized_name,
                    first_observed_at, last_observed_at
                ) VALUES (%s, 'QA Review Edition', %s, NOW(), NOW())
                """,
                (str(edition_id), f"qa-review-edition-{suffix}"),
            )
            cursor.execute(
                """
                INSERT INTO public.event_sources(
                    event_edition_id, source_document_id, collection_mode
                ) VALUES (%s, %s, 'realtime_signal'),
                         (%s, %s, 'historical_backfill')
                """,
                (
                    str(edition_id),
                    str(source_realtime),
                    str(edition_id),
                    str(source_historical),
                ),
            )
            for tenant_id, grant_id, candidate_id in (
                (tenant_a, grant_a, candidate_a),
                (tenant_b, grant_b, candidate_b),
            ):
                cursor.execute(
                    """
                    INSERT INTO public.tenant_resource_grants(
                        id, tenant_id, event_edition_id,
                        trigger_source_document_id, policy_version,
                        grant_source
                    ) VALUES (%s, %s, %s, %s, 'qa-policy-v1', 'qa_seed')
                    """,
                    (
                        str(grant_id),
                        str(tenant_id),
                        str(edition_id),
                        str(source_realtime),
                    ),
                )
                cursor.execute(
                    """
                    INSERT INTO public.tenant_candidates(
                        id, tenant_id, grant_id, event_edition_id
                    ) VALUES (%s, %s, %s, %s)
                    """,
                    (
                        str(candidate_id),
                        str(tenant_id),
                        str(grant_id),
                        str(edition_id),
                    ),
                )
                cursor.execute(
                    """
                    INSERT INTO public.tenant_candidate_score_snapshots(
                        tenant_id, candidate_id, event_edition_id, grant_id,
                        ruleset_version, ruleset_sha256,
                        scoring_method_version, input_hash,
                        total_score, priority_band, component_scores,
                        score_as_of
                    )
                    SELECT %s, %s, %s, %s, version, definition_sha256,
                           'review-priority-envelope/1.0.0', %s,
                           40, 'normal',
                           '{
                              "timeliness_stage": {
                                "score": 15,
                                "explanation_code": "timeliness_active"
                              },
                              "online_voting_demand": {
                                "score": 10,
                                "explanation_code": "demand_indirect"
                              },
                              "organizer_value": {
                                "score": 8,
                                "explanation_code": "organizer_repeat"
                              },
                              "contactability": {
                                "score": 5,
                                "explanation_code": "contact_indirect"
                              },
                              "evidence_quality": {
                                "score": 2,
                                "explanation_code": "evidence_single_source"
                              }
                           }'::jsonb,
                           NOW()
                    FROM public.review_rulesets
                    WHERE version='review-rules/1.0.0'
                    """,
                    (
                        str(tenant_id),
                        str(candidate_id),
                        str(edition_id),
                        str(grant_id),
                        ("a" if tenant_id == tenant_a else "b") * 64,
                    ),
                )
        admin.commit()

        with admin.cursor() as cursor, pytest.raises(errors.CheckViolation):
            cursor.execute(
                """
                INSERT INTO public.tenant_reviews(
                    tenant_id, candidate_id, event_edition_id, grant_id,
                    grant_policy_version, review_status,
                    cancel_reason, cancelled_at
                ) VALUES (
                    %s, %s, %s, %s, 'qa-policy-v1', 'cancelled',
                    'qa-direct-cancelled-bypass', NOW()
                )
                """,
                (
                    str(tenant_a),
                    str(candidate_a),
                    str(edition_id),
                    str(grant_a),
                ),
            )
        admin.rollback()

        with admin.cursor() as cursor:
            with pytest.raises(errors.ForeignKeyViolation):
                cursor.execute(
                    """
                    INSERT INTO public.tenant_resource_grants(
                        tenant_id, event_edition_id,
                        trigger_source_document_id, policy_version,
                        grant_source, status, revoked_at
                    ) VALUES (
                        %s, %s, %s, 'qa-policy-v1', 'qa_invalid',
                        'revoked', NOW()
                    )
                    """,
                    (str(tenant_a), str(edition_id), str(source_historical)),
                )
        admin.rollback()

        settings = RuntimeRoleSettings(
            migration_database_url=admin_url,
            app_database_user=role_name,
            app_database_password=role_password,
        )
        result = provision_runtime_role(settings)
        assert result["write_function_granted"] is True
        assert result["ruleset_lock_function_granted"] is True
        connector = object.__new__(DatabaseConnector)
        connector.pool = pool.ThreadedConnectionPool(1, 4, runtime_url)
        connector._local = threading.local()

        def assert_runtime_share_lock_blocks_admin_update(
            lock_query: str,
            lock_params: tuple[str, ...],
            update_query: str,
            update_params: tuple[str, ...],
        ) -> None:
            lock_entered = threading.Event()
            lock_release = threading.Event()
            lock_errors = []

            def hold_runtime_lock() -> None:
                try:
                    with connector.tenant_write_transaction(
                        authenticated_user_id=reviewer_legacy_id,
                        requested_tenant_id=tenant_a,
                    ) as transaction:
                        assert transaction.execute_query(lock_query, lock_params)
                        lock_entered.set()
                        assert lock_release.wait(timeout=5)
                except BaseException as error:  # noqa: BLE001
                    lock_errors.append(error)

            lock_holder = threading.Thread(target=hold_runtime_lock)
            lock_holder.start()
            assert lock_entered.wait(timeout=5)
            lock_revoker = psycopg2.connect(admin_url)
            try:
                with lock_revoker.cursor() as cursor:
                    cursor.execute("SET LOCAL lock_timeout = '500ms'")
                    with pytest.raises(errors.LockNotAvailable):
                        cursor.execute(update_query, update_params)
                lock_revoker.rollback()
            finally:
                lock_release.set()
                lock_holder.join(timeout=5)
                lock_revoker.close()
            assert not lock_holder.is_alive()
            assert lock_errors == []

        assert_runtime_share_lock_blocks_admin_update(
            """
            SELECT activation_id
            FROM public.app_lock_active_review_ruleset(%s)
            """,
            (str(tenant_a),),
            """
            UPDATE public.tenant_review_ruleset_activations
            SET deactivated_at=NOW()
            WHERE tenant_id=%s AND deactivated_at IS NULL
            """,
            (str(tenant_a),),
        )
        assert_runtime_share_lock_blocks_admin_update(
            """
            SELECT grant_id
            FROM public.app_lock_active_review_grant(%s, %s)
            """,
            (str(tenant_a), str(grant_a)),
            """
            UPDATE public.tenant_resource_grants
            SET status='revoked', revoked_at=NOW(), version=version+1
            WHERE id=%s
            """,
            (str(grant_a),),
        )

        entered = threading.Event()
        release = threading.Event()
        holder_errors = []

        def hold_authorized_write():
            try:
                with connector.tenant_write_transaction(
                    authenticated_user_id=reviewer_legacy_id,
                    requested_tenant_id=tenant_a,
                ):
                    entered.set()
                    assert release.wait(timeout=5)
            except BaseException as error:  # noqa: BLE001
                holder_errors.append(error)

        holder = threading.Thread(target=hold_authorized_write)
        holder.start()
        assert entered.wait(timeout=5)
        revoker = psycopg2.connect(admin_url)
        try:
            with revoker.cursor() as cursor:
                cursor.execute("SET LOCAL lock_timeout = '500ms'")
                with pytest.raises(errors.LockNotAvailable):
                    cursor.execute(
                        """
                        UPDATE public.tenant_memberships
                        SET status = 'disabled'
                        WHERE id = %s
                        """,
                        (str(membership_id),),
                    )
            revoker.rollback()
        finally:
            release.set()
            holder.join(timeout=5)
            revoker.close()
        assert not holder.is_alive()
        assert holder_errors == []

        with admin.cursor() as cursor:
            cursor.execute(
                "UPDATE public.tenant_memberships SET status='disabled' WHERE id=%s",
                (str(membership_id),),
            )
        admin.commit()
        with pytest.raises(TenantAccessDenied):
            with connector.tenant_write_transaction(
                authenticated_user_id=reviewer_legacy_id,
                requested_tenant_id=tenant_a,
            ):
                pytest.fail("revoked reviewer must not enter a write transaction")
        with admin.cursor() as cursor:
            cursor.execute(
                "UPDATE public.tenant_memberships SET status='active' WHERE id=%s",
                (str(membership_id),),
            )
        admin.commit()

        service = ReviewService()
        with connector.tenant_transaction(
            authenticated_user_id=reviewer_legacy_id,
            requested_tenant_id=tenant_a,
        ) as transaction:
            visible = service.list_candidates(transaction)
        assert [item["id"] for item in visible] == [str(candidate_a)]

        with connector.tenant_transaction(
            authenticated_user_id=reviewer_legacy_id,
            requested_tenant_id=tenant_a,
        ) as transaction:
            activation_tenants = transaction.execute_query(
                """
                SELECT tenant_id
                FROM public.tenant_review_ruleset_activations
                ORDER BY tenant_id
                """
            )
            assert [str(row[0]) for row in activation_tenants] == [str(tenant_a)]
            assert transaction.execute_query(
                """
                SELECT activation_id
                FROM public.app_lock_active_review_ruleset(%s)
                """,
                (str(tenant_b),),
            ) == []
            score_tenants = transaction.execute_query(
                """
                SELECT tenant_id
                FROM public.tenant_candidate_score_snapshots
                ORDER BY tenant_id
                """
            )
            assert [str(row[0]) for row in score_tenants] == [str(tenant_a)]

        with pytest.raises(ReviewNotFound):
            with connector.tenant_write_transaction(
                authenticated_user_id=reviewer_legacy_id,
                requested_tenant_id=tenant_a,
            ) as transaction:
                service.start_review(
                    transaction,
                    candidate_id=candidate_b,
                    expected_candidate_version=1,
                    idempotency_key="qa-cross-tenant-0001",
                )

        with connector.tenant_write_transaction(
            authenticated_user_id=reviewer_legacy_id,
            requested_tenant_id=tenant_a,
        ) as transaction:
            started = service.start_review(
                transaction,
                candidate_id=candidate_a,
                expected_candidate_version=1,
                idempotency_key="qa-start-review-0001",
            )
        with admin.cursor() as cursor, pytest.raises(errors.CheckViolation):
            cursor.execute(
                """
                UPDATE public.tenant_reviews
                SET rule_snapshot = '{}'::jsonb
                WHERE id = %s
                """,
                (started["review_id"],),
            )
        admin.rollback()

        with admin.cursor() as cursor, pytest.raises(psycopg2.IntegrityError):
            cursor.execute(
                """
                UPDATE public.tenant_reviews
                SET review_status='completed',
                    review_decision='rejected', disposition='archive',
                    reason_code='sales_ready_confirmed',
                    reason_schema_version='review-rules/1.0.0',
                    completed_at=NOW(), version=version+1
                WHERE id=%s
                """,
                (started["review_id"],),
            )
        admin.rollback()

        with connector.tenant_write_transaction(
            authenticated_user_id=reviewer_legacy_id,
            requested_tenant_id=tenant_a,
        ) as transaction:
            completed = service.complete_review(
                transaction,
                review_id=started["review_id"],
                expected_review_version=started["review_version"],
                expected_candidate_version=started["candidate_version"],
                decision="rejected",
                disposition="archive",
                idempotency_key="qa-complete-review-0001",
                reason_code="not_selection_or_voting",
            )
        assert completed["review_status"] == "completed"

        with admin.cursor() as cursor:
            cursor.execute(
                """
                SELECT candidate_status, version
                FROM public.tenant_candidates WHERE id=%s
                """,
                (str(candidate_a),),
            )
            assert cursor.fetchone() == ("closed", 3)
            cursor.execute(
                """
                SELECT review_status, review_decision, disposition, version
                FROM public.tenant_reviews WHERE id=%s
                """,
                (started["review_id"],),
            )
            assert cursor.fetchone() == ("completed", "rejected", "archive", 2)
            cursor.execute(
                """
                SELECT event_type, aggregate_id, aggregate_version, payload
                FROM public.domain_outbox WHERE message_id=%s
                """,
                (completed["message_id"],),
            )
            event_type, aggregate_id, aggregate_version, payload = cursor.fetchone()
            assert event_type == "review.completed.v1"
            assert str(aggregate_id) == started["review_id"]
            assert aggregate_version == 2
            assert payload["candidate_id"] == str(candidate_a)
            assert payload["reason_code"] == "not_selection_or_voting"
            assert "reviewer_note" not in payload

            cursor.execute(
                """
                INSERT INTO public.source_documents(
                    id, source_channel, source_key, collection_mode,
                    content_sha256, observed_at
                ) VALUES
                    (%s, 'qa', %s, 'realtime_signal', %s, NOW()),
                    (%s, 'qa', %s, 'realtime_signal', %s, NOW())
                """,
                (
                    str(source_reopen),
                    f"reopen-{suffix}",
                    "3" * 64,
                    str(source_wrong_edition),
                    f"wrong-edition-{suffix}",
                    "4" * 64,
                ),
            )
            cursor.execute(
                """
                INSERT INTO public.event_editions(
                    id, canonical_name, normalized_name,
                    first_observed_at, last_observed_at
                ) VALUES (%s, 'QA Wrong Review Edition', %s, NOW(), NOW())
                """,
                (str(wrong_edition_id), f"qa-wrong-edition-{suffix}"),
            )
            cursor.execute(
                """
                INSERT INTO public.event_sources(
                    event_edition_id, source_document_id, collection_mode
                ) VALUES (%s, %s, 'realtime_signal'),
                         (%s, %s, 'realtime_signal')
                """,
                (
                    str(edition_id),
                    str(source_reopen),
                    str(wrong_edition_id),
                    str(source_wrong_edition),
                ),
            )
        admin.commit()

        for invalid_source, sequence in (
            (source_historical, 1),
            (source_realtime, 2),
            (source_wrong_edition, 3),
        ):
            with pytest.raises(ReviewInvalidTransition):
                with connector.tenant_write_transaction(
                    authenticated_user_id=reviewer_legacy_id,
                    requested_tenant_id=tenant_a,
                ) as transaction:
                    service.reopen_review(
                        transaction,
                        candidate_id=candidate_a,
                        previous_review_id=started["review_id"],
                        expected_candidate_version=completed["candidate_version"],
                        expected_review_version=completed["review_version"],
                        reopen_reason_code="new_realtime_evidence",
                        trigger_source_document_id=invalid_source,
                        idempotency_key=f"qa-reopen-invalid-source-{sequence:04d}",
                    )

        reopen_barrier = threading.Barrier(2)
        reopen_results = []
        reopen_errors = []

        def compete_to_reopen(sequence: int) -> None:
            try:
                reopen_barrier.wait(timeout=5)
                with connector.tenant_write_transaction(
                    authenticated_user_id=reviewer_legacy_id,
                    requested_tenant_id=tenant_a,
                ) as transaction:
                    result = service.reopen_review(
                        transaction,
                        candidate_id=candidate_a,
                        previous_review_id=started["review_id"],
                        expected_candidate_version=completed["candidate_version"],
                        expected_review_version=completed["review_version"],
                        reopen_reason_code="new_realtime_evidence",
                        trigger_source_document_id=source_reopen,
                        idempotency_key=f"qa-reopen-concurrent-{sequence:04d}",
                    )
                reopen_results.append((sequence, result))
            except BaseException as error:  # noqa: BLE001
                reopen_errors.append(error)

        competitors = [
            threading.Thread(target=compete_to_reopen, args=(sequence,))
            for sequence in (1, 2)
        ]
        for competitor in competitors:
            competitor.start()
        for competitor in competitors:
            competitor.join(timeout=10)
        assert all(not competitor.is_alive() for competitor in competitors)
        assert len(reopen_results) == 1
        assert len(reopen_errors) == 1
        assert isinstance(reopen_errors[0], ReviewConflict)
        winning_sequence, reopened = reopen_results[0]
        winning_reopen_key = f"qa-reopen-concurrent-{winning_sequence:04d}"
        assert reopened["review_round"] == 2
        assert reopened["supersedes_review_id"] == started["review_id"]

        with pytest.raises(ReviewConflict):
            with connector.tenant_write_transaction(
                authenticated_user_id=reviewer_legacy_id,
                requested_tenant_id=tenant_a,
            ) as transaction:
                service.reopen_review(
                    transaction,
                    candidate_id=candidate_a,
                    previous_review_id=started["review_id"],
                    expected_candidate_version=completed["candidate_version"],
                    expected_review_version=completed["review_version"],
                    reopen_reason_code="missing_information_resolved",
                    trigger_source_document_id=source_reopen,
                    idempotency_key=winning_reopen_key,
                )

        with admin.cursor() as cursor:
            cursor.execute(
                """
                UPDATE public.tenant_review_ruleset_activations
                SET deactivated_at=NOW()
                WHERE tenant_id=%s AND deactivated_at IS NULL
                """,
                (str(tenant_a),),
            )
            cursor.execute(
                """
                INSERT INTO public.tenant_review_ruleset_activations(
                    tenant_id, ruleset_version, ruleset_sha256,
                    activation_reference
                )
                SELECT %s, version, definition_sha256,
                       'qa-explicit-activation-switch'
                FROM public.review_rulesets
                WHERE version='review-rules/1.0.0'
                """,
                (str(tenant_a),),
            )
        admin.commit()

        with connector.tenant_write_transaction(
            authenticated_user_id=reviewer_legacy_id,
            requested_tenant_id=tenant_a,
        ) as transaction:
            replayed = service.reopen_review(
                transaction,
                candidate_id=candidate_a,
                previous_review_id=started["review_id"],
                expected_candidate_version=completed["candidate_version"],
                expected_review_version=completed["review_version"],
                reopen_reason_code="new_realtime_evidence",
                trigger_source_document_id=source_reopen,
                idempotency_key=winning_reopen_key,
            )
        assert replayed["review_id"] == reopened["review_id"]
        assert replayed["idempotency_replayed"] is True

        with connector.tenant_write_transaction(
            authenticated_user_id=reviewer_legacy_id,
            requested_tenant_id=tenant_a,
        ) as transaction:
            completed_round_two = service.complete_review(
                transaction,
                review_id=reopened["review_id"],
                expected_review_version=reopened["review_version"],
                expected_candidate_version=reopened["candidate_version"],
                decision="qualified",
                disposition="nurture",
                idempotency_key="qa-complete-review-0002",
                reason_code="future_contact_window",
                reopen_not_before=datetime.now(timezone.utc)
                + timedelta(seconds=2),
            )
        assert completed_round_two["candidate_version"] == 5

        with pytest.raises(ReviewInvalidTransition):
            with connector.tenant_write_transaction(
                authenticated_user_id=reviewer_legacy_id,
                requested_tenant_id=tenant_a,
            ) as transaction:
                service.reopen_review(
                    transaction,
                    candidate_id=candidate_a,
                    previous_review_id=started["review_id"],
                    expected_candidate_version=completed_round_two[
                        "candidate_version"
                    ],
                    expected_review_version=completed["review_version"],
                    reopen_reason_code="new_realtime_evidence",
                    trigger_source_document_id=source_reopen,
                    idempotency_key="qa-reopen-non-latest-0001",
                )

        with pytest.raises(ReviewInvalidInput):
            with connector.tenant_write_transaction(
                authenticated_user_id=reviewer_legacy_id,
                requested_tenant_id=tenant_a,
            ) as transaction:
                service.reopen_review(
                    transaction,
                    candidate_id=candidate_a,
                    previous_review_id=reopened["review_id"],
                    expected_candidate_version=completed_round_two[
                        "candidate_version"
                    ],
                    expected_review_version=completed_round_two[
                        "review_version"
                    ],
                    reopen_reason_code="new_realtime_evidence",
                    trigger_source_document_id=None,
                    idempotency_key="qa-reopen-source-required-0001",
                )

        time.sleep(2.1)

        with connector.tenant_write_transaction(
            authenticated_user_id=reviewer_legacy_id,
            requested_tenant_id=tenant_a,
        ) as transaction:
            round_three = service.reopen_review(
                transaction,
                candidate_id=candidate_a,
                previous_review_id=reopened["review_id"],
                expected_candidate_version=completed_round_two[
                    "candidate_version"
                ],
                expected_review_version=completed_round_two["review_version"],
                reopen_reason_code="scheduled_recheck_due",
                trigger_source_document_id=None,
                idempotency_key="qa-reopen-review-0003",
            )
        with connector.tenant_write_transaction(
            authenticated_user_id=reviewer_legacy_id,
            requested_tenant_id=tenant_a,
        ) as transaction:
            completed_round_three = service.complete_review(
                transaction,
                review_id=round_three["review_id"],
                expected_review_version=round_three["review_version"],
                expected_candidate_version=round_three["candidate_version"],
                decision="rejected",
                disposition="archive",
                idempotency_key="qa-complete-review-0003",
                reason_code="not_selection_or_voting",
            )
        with pytest.raises(ReviewInvalidTransition):
            with connector.tenant_write_transaction(
                authenticated_user_id=reviewer_legacy_id,
                requested_tenant_id=tenant_a,
            ) as transaction:
                service.reopen_review(
                    transaction,
                    candidate_id=candidate_a,
                    previous_review_id=round_three["review_id"],
                    expected_candidate_version=completed_round_three[
                        "candidate_version"
                    ],
                    expected_review_version=completed_round_three[
                        "review_version"
                    ],
                    reopen_reason_code="new_realtime_evidence",
                    trigger_source_document_id=source_reopen,
                    idempotency_key="qa-reopen-round-limit-0001",
                )

        with admin.cursor() as cursor:
            cursor.execute(
                """
                SELECT review_round, review_status, supersedes_review_id,
                       reopen_reason_code, reopen_trigger_source_document_id
                FROM public.tenant_reviews
                WHERE id=%s
                """,
                (reopened["review_id"],),
            )
            (
                review_round,
                review_status,
                supersedes_review_id,
                reopen_reason,
                reopen_source_id,
            ) = cursor.fetchone()
            assert review_round == 2
            assert review_status == "completed"
            assert str(supersedes_review_id) == started["review_id"]
            assert reopen_reason == "new_realtime_evidence"
            assert str(reopen_source_id) == str(source_reopen)
            cursor.execute(
                """
                SELECT id, review_status, review_decision, disposition,
                       reason_code, version, rule_version,
                       rule_definition_sha256, rule_snapshot
                FROM public.tenant_reviews
                WHERE tenant_id=%s AND candidate_id=%s
                ORDER BY review_round
                """,
                (str(tenant_a), str(candidate_a)),
            )
            completed_history_before_revoke = cursor.fetchall()
            assert len(completed_history_before_revoke) == 3
            assert all(row[1] == "completed" for row in completed_history_before_revoke)
            assert completed_history_before_revoke[0][2:5] == (
                "rejected",
                "archive",
                "not_selection_or_voting",
            )
            assert completed_history_before_revoke[1][2:5] == (
                "qualified",
                "nurture",
                "future_contact_window",
            )
            cursor.execute(
                """
                UPDATE public.tenant_resource_grants
                SET status='revoked', revoked_at=NOW(), version=version+1
                WHERE id=%s
                """,
                (str(grant_a),),
            )
            cursor.execute(
                "SELECT candidate_status FROM public.tenant_candidates WHERE id=%s",
                (str(candidate_a),),
            )
            assert cursor.fetchone()[0] == "closed"
            cursor.execute(
                """
                SELECT id, review_status, review_decision, disposition,
                       reason_code, version, rule_version,
                       rule_definition_sha256, rule_snapshot
                FROM public.tenant_reviews
                WHERE tenant_id=%s AND candidate_id=%s
                ORDER BY review_round
                """,
                (str(tenant_a), str(candidate_a)),
            )
            assert cursor.fetchall() == completed_history_before_revoke
        admin.rollback()
    finally:
        if connector is not None:
            connector.pool.closeall()
        admin.rollback()
        try:
            with admin.cursor() as cursor:
                cursor.execute("SET session_replication_role = replica")
                for table_name, column_name, values in (
                    ("domain_outbox", "tenant_id", (tenant_a, tenant_b)),
                    ("tenant_command_receipts", "tenant_id", (tenant_a, tenant_b)),
                    ("tenant_reviews", "tenant_id", (tenant_a, tenant_b)),
                    (
                        "tenant_candidate_score_snapshots",
                        "tenant_id",
                        (tenant_a, tenant_b),
                    ),
                    ("tenant_candidates", "tenant_id", (tenant_a, tenant_b)),
                    ("tenant_resource_grants", "tenant_id", (tenant_a, tenant_b)),
                    (
                        "tenant_review_ruleset_activations",
                        "tenant_id",
                        (tenant_a, tenant_b),
                    ),
                    (
                        "event_sources",
                        "event_edition_id",
                        (edition_id, wrong_edition_id),
                    ),
                    (
                        "event_editions",
                        "id",
                        (edition_id, wrong_edition_id),
                    ),
                    (
                        "source_documents",
                        "id",
                        (
                            source_realtime,
                            source_historical,
                            source_reopen,
                            source_wrong_edition,
                        ),
                    ),
                    ("tenant_memberships", "tenant_id", (tenant_a, tenant_b)),
                    ("tenants", "id", (tenant_a, tenant_b)),
                ):
                    placeholders = ", ".join(["%s"] * len(values))
                    cursor.execute(
                        sql.SQL("DELETE FROM public.{} WHERE {} IN (").format(
                            sql.Identifier(table_name), sql.Identifier(column_name)
                        )
                        + sql.SQL(placeholders)
                        + sql.SQL(")"),
                        tuple(str(value) for value in values),
                    )
                if reviewer_legacy_id is not None:
                    cursor.execute(
                        "DELETE FROM public.users WHERE id=%s",
                        (reviewer_legacy_id,),
                    )
                cursor.execute("SET session_replication_role = origin")
                cursor.execute(
                    "SELECT EXISTS(SELECT 1 FROM pg_roles WHERE rolname=%s)",
                    (role_name,),
                )
                if cursor.fetchone()[0]:
                    cursor.execute(
                        sql.SQL("DROP OWNED BY {}").format(
                            sql.Identifier(role_name)
                        )
                    )
                    cursor.execute(
                        sql.SQL("DROP ROLE {}").format(sql.Identifier(role_name))
                    )
            admin.commit()
        finally:
            admin.close()
