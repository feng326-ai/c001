"""Real PostgreSQL 15 proof for review RLS, strict revoke and atomic outbox."""

from __future__ import annotations

import os
import threading
import uuid
from urllib.parse import quote, urlsplit, urlunsplit

import pytest

from wxsearch.db_connector import DatabaseConnector, TenantAccessDenied
from wxsearch.review_service import ReviewNotFound, ReviewService
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
    source_realtime, source_historical = uuid.uuid4(), uuid.uuid4()
    edition_id = uuid.uuid4()
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
        admin.commit()

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
        connector = object.__new__(DatabaseConnector)
        connector.pool = pool.ThreadedConnectionPool(1, 4, runtime_url)
        connector._local = threading.local()

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
                reason_code="qa_not_fit",
                reason_schema_version="draft-v1",
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
            assert "reviewer_note" not in payload
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
                    ("tenant_candidates", "tenant_id", (tenant_a, tenant_b)),
                    ("tenant_resource_grants", "tenant_id", (tenant_a, tenant_b)),
                    ("event_sources", "event_edition_id", (edition_id,)),
                    ("event_editions", "id", (edition_id,)),
                    (
                        "source_documents",
                        "id",
                        (source_realtime, source_historical),
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
