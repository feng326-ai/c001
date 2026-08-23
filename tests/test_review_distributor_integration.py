"""PostgreSQL 15 proof for controlled, fenced review distribution."""

from __future__ import annotations

import os
import uuid
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import quote, urlsplit, urlunsplit

import pytest

from wxsearch.distributor_db_role import (
    DistributorRoleSettings,
    check_distributor_role,
    provision_distributor_role,
)
from wxsearch.review_distributor import (
    ReviewDistributor,
    ReviewDistributorConflict,
    ReviewDistributorError,
    ReviewDistributorInvalidInput,
)

RUN = os.getenv("RUN_MIGRATION_INTEGRATION") == "1"


def _guarded_qa_database_url() -> str:
    if os.getenv("ENVIRONMENT") != "qa" or os.getenv("ALLOW_DESTRUCTIVE_TESTS") != "1":
        raise RuntimeError("distributor integration requires guarded QA environment")
    database_url = os.environ["DATABASE_URL"]
    if "_lease_qa_" not in urlsplit(database_url).path:
        raise RuntimeError("distributor integration refused a non-QA database")
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
def test_distributor_is_least_privilege_idempotent_fenced_and_failure_isolated(
    monkeypatch,
) -> None:
    import psycopg2
    from psycopg2 import errors, sql

    admin_url = _guarded_qa_database_url()
    suffix = uuid.uuid4().hex[:8]
    distributor_role = f"qa_distributor_{suffix}"
    ordinary_role = f"qa_runtime_{suffix}"
    distributor_password = f"Qa-Distributor-{suffix}-Password"
    ordinary_password = f"Qa-Runtime-{suffix}-Password"
    distributor_url = _role_url(admin_url, distributor_role, distributor_password)
    tenant_ids = [uuid.uuid4() for _ in range(5)]
    tenant_a, tenant_b, tenant_c, tenant_disabled, tenant_no_rules = tenant_ids
    edition_ids = [uuid.uuid4() for _ in range(6)]
    source_ids = [uuid.uuid4() for _ in range(7)]
    inbox_ids = [uuid.uuid4() for _ in range(7)]
    upstream_ids = [uuid.uuid4() for _ in range(7)]
    admin = psycopg2.connect(admin_url)
    public_create_before = None
    distributor_connection = None

    def insert_realtime_fixture(index: int, *, stage: str = "planning") -> None:
        with admin.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO public.source_documents(
                    id, source_channel, source_key, collection_mode,
                    content_sha256, observed_at
                ) VALUES (%s, 'qa-distributor', %s, 'realtime_signal', %s, NOW())
                """,
                (
                    str(source_ids[index]),
                    f"qa-distributor-source-{suffix}-{index}",
                    f"{index + 1:x}" * 64,
                ),
            )
            cursor.execute(
                """
                INSERT INTO public.event_editions(
                    id, canonical_name, normalized_name, activity_stage,
                    first_observed_at, last_observed_at
                ) VALUES (%s, %s, %s, %s, NOW(), NOW())
                """,
                (
                    str(edition_ids[index]),
                    f"QA Distribution Edition {index}",
                    f"qa-distribution-edition-{suffix}-{index}",
                    stage,
                ),
            )
            cursor.execute(
                """
                INSERT INTO public.event_sources(
                    event_edition_id, source_document_id, collection_mode
                ) VALUES (%s, %s, 'realtime_signal')
                """,
                (str(edition_ids[index]), str(source_ids[index])),
            )
            cursor.execute(
                """
                INSERT INTO platform_control.review_distribution_inbox(
                    id, upstream_message_id, input_sha256,
                    event_edition_id, trigger_source_document_id,
                    trigger_collection_mode
                ) VALUES (%s, %s, %s, %s, %s, 'realtime_signal')
                """,
                (
                    str(inbox_ids[index]),
                    str(upstream_ids[index]),
                    f"{index + 1:x}" * 64,
                    str(edition_ids[index]),
                    str(source_ids[index]),
                ),
            )
        admin.commit()

    try:
        with admin.cursor() as cursor:
            cursor.execute("SELECT has_schema_privilege('public', 'public', 'CREATE')")
            public_create_before = bool(cursor.fetchone()[0])
            for index, tenant_id in enumerate(tenant_ids):
                status = "disabled" if tenant_id == tenant_disabled else "active"
                cursor.execute(
                    """
                    INSERT INTO public.tenants(
                        id, code, name, status, default_visibility_policy
                    ) VALUES (%s, %s, %s, %s, 'shared_competition')
                    """,
                    (
                        str(tenant_id),
                        f"qa-dist-{suffix}-{index}",
                        f"QA Distributor Tenant {index}",
                        status,
                    ),
                )
                cursor.execute(
                    """
                    INSERT INTO platform_control.review_distribution_tenant_settings(
                        tenant_id, policy_version, eligibility_version,
                        approval_reference
                    ) VALUES (
                        %s, 'shared-competition/1.0.0',
                        'eligibility/1.0.0', %s
                    )
                    """,
                    (str(tenant_id), f"qa-approved-{suffix}-{index}"),
                )
                if tenant_id != tenant_no_rules:
                    cursor.execute(
                        """
                        INSERT INTO public.tenant_review_ruleset_activations(
                            tenant_id, ruleset_version, ruleset_sha256,
                            activation_reference
                        )
                        SELECT %s, version, definition_sha256, %s
                        FROM public.review_rulesets
                        WHERE version='review-rules/1.0.0'
                        """,
                        (str(tenant_id), f"qa-activation-{suffix}-{index}"),
                    )
            cursor.execute(
                sql.SQL(
                    "CREATE ROLE {} WITH LOGIN NOSUPERUSER NOCREATEDB "
                    "NOCREATEROLE NOINHERIT NOREPLICATION NOBYPASSRLS PASSWORD %s"
                ).format(sql.Identifier(ordinary_role)),
                (ordinary_password,),
            )
            cursor.execute(
                sql.SQL("GRANT CONNECT ON DATABASE {} TO {}").format(
                    sql.Identifier(urlsplit(admin_url).path.lstrip("/")),
                    sql.Identifier(ordinary_role),
                )
            )
            cursor.execute(
                sql.SQL("GRANT USAGE ON SCHEMA public TO {}").format(
                    sql.Identifier(ordinary_role)
                )
            )
        admin.commit()

        insert_realtime_fixture(0)
        with admin.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO public.source_documents(
                    id, source_channel, source_key, collection_mode,
                    content_sha256, observed_at
                ) VALUES (%s, 'qa-distributor', %s, 'historical_backfill', %s, NOW())
                """,
                (
                    str(source_ids[6]),
                    f"qa-distributor-historical-{suffix}",
                    "f" * 64,
                ),
            )
            cursor.execute(
                """
                INSERT INTO public.event_editions(
                    id, canonical_name, normalized_name,
                    first_observed_at, last_observed_at
                ) VALUES (%s, 'QA Historical Edition', %s, NOW(), NOW())
                """,
                (
                    str(uuid.uuid5(uuid.NAMESPACE_URL, f"hist-{suffix}")),
                    f"qa-historical-edition-{suffix}",
                ),
            )
            historical_edition = uuid.uuid5(uuid.NAMESPACE_URL, f"hist-{suffix}")
            cursor.execute(
                """
                INSERT INTO public.event_sources(
                    event_edition_id, source_document_id, collection_mode
                ) VALUES (%s, %s, 'historical_backfill')
                """,
                (str(historical_edition), str(source_ids[6])),
            )
            cursor.execute(
                """
                INSERT INTO platform_control.review_distribution_inbox(
                    id, upstream_message_id, input_sha256,
                    event_edition_id, trigger_source_document_id,
                    trigger_collection_mode
                ) VALUES (%s, %s, %s, %s, %s, 'historical_backfill')
                """,
                (
                    str(inbox_ids[6]),
                    str(upstream_ids[6]),
                    "f" * 64,
                    str(historical_edition),
                    str(source_ids[6]),
                ),
            )
        admin.commit()

        settings = DistributorRoleSettings(
            migration_database_url=admin_url,
            distributor_database_user=distributor_role,
            distributor_database_password=distributor_password,
        )
        provisioned = provision_distributor_role(settings)
        assert provisioned["verified"] is True
        assert check_distributor_role(settings)["function_count"] == 4

        ordinary = psycopg2.connect(
            _role_url(admin_url, ordinary_role, ordinary_password)
        )
        try:
            with (
                ordinary.cursor() as cursor,
                pytest.raises(errors.InsufficientPrivilege),
            ):
                cursor.execute(
                    "SELECT * FROM public.app_expand_review_distribution(%s)",
                    (str(inbox_ids[0]),),
                )
            ordinary.rollback()
        finally:
            ordinary.close()

        distributor_connection = psycopg2.connect(distributor_url)
        with (
            distributor_connection.cursor() as cursor,
            pytest.raises(errors.InsufficientPrivilege),
        ):
            cursor.execute("SELECT * FROM platform_control.review_distribution_targets")
        distributor_connection.rollback()
        distributor_connection.close()
        distributor_connection = None

        monkeypatch.setenv("REVIEW_DISTRIBUTOR_ENABLED", "true")
        distributor = ReviewDistributor(
            connection_factory=lambda: psycopg2.connect(distributor_url)
        )
        expanded = distributor.expand_inbox(inbox_id=inbox_ids[0])
        assert expanded["target_count"] == 3
        assert expanded["replayed"] is False
        replayed = distributor.expand_inbox(inbox_id=inbox_ids[0])
        assert replayed == {**expanded, "replayed": True}

        with pytest.raises(ReviewDistributorInvalidInput):
            distributor.expand_inbox(inbox_id=inbox_ids[6])
        with admin.cursor() as cursor:
            cursor.execute(
                """
                SELECT COUNT(*)
                FROM platform_control.review_distribution_batches
                WHERE inbox_id=%s
                """,
                (str(inbox_ids[6]),),
            )
            assert cursor.fetchone()[0] == 0
        admin.rollback()

        outcomes = []
        while True:
            claim = distributor.claim_target(worker_id=f"worker-{suffix}")
            if claim is None:
                break
            outcomes.append(
                distributor.apply_target(
                    target_id=claim["target_id"],
                    fencing_token=claim["fencing_token"],
                )
            )
        assert len(outcomes) == 3
        assert {result["outcome_code"] for result in outcomes} == {"created"}
        with admin.cursor() as cursor:
            for table_name in (
                "tenant_resource_grants",
                "tenant_candidates",
                "domain_outbox",
            ):
                cursor.execute(
                    sql.SQL(
                        "SELECT COUNT(*) FROM public.{} WHERE tenant_id=ANY(%s::uuid[])"
                    ).format(sql.Identifier(table_name)),
                    ([str(tenant_a), str(tenant_b), str(tenant_c)],),
                )
                assert cursor.fetchone()[0] == 3
            cursor.execute(
                """
                SELECT payload
                FROM public.domain_outbox
                WHERE tenant_id=ANY(%s::uuid[])
                  AND event_type='candidate.created.v1'
                """,
                ([str(tenant_a), str(tenant_b), str(tenant_c)],),
            )
            payloads = [row[0] for row in cursor.fetchall()]
            assert len(payloads) == 3
            assert all("tenant_id" not in payload for payload in payloads)
            assert all("tenant_ids" not in payload for payload in payloads)
        admin.rollback()

        insert_realtime_fixture(1)
        distributor.expand_inbox(inbox_id=inbox_ids[1])
        old_claim = distributor.claim_target(worker_id=f"worker-old-{suffix}")
        assert old_claim is not None
        with admin.cursor() as cursor:
            cursor.execute(
                """
                UPDATE platform_control.review_distribution_targets
                SET lease_expires_at=NOW() - INTERVAL '1 second'
                WHERE id=%s
                """,
                (old_claim["target_id"],),
            )
        admin.commit()
        new_claim = distributor.claim_target(worker_id=f"worker-new-{suffix}")
        assert new_claim is not None
        assert new_claim["target_id"] == old_claim["target_id"]
        assert new_claim["fencing_token"] != old_claim["fencing_token"]
        with pytest.raises(ReviewDistributorConflict):
            distributor.apply_target(
                target_id=old_claim["target_id"],
                fencing_token=old_claim["fencing_token"],
            )
        distributor.apply_target(
            target_id=new_claim["target_id"],
            fencing_token=new_claim["fencing_token"],
        )
        while claim := distributor.claim_target(worker_id=f"worker-{suffix}"):
            distributor.apply_target(
                target_id=claim["target_id"],
                fencing_token=claim["fencing_token"],
            )

        insert_realtime_fixture(2)
        distributor.expand_inbox(inbox_id=inbox_ids[2])
        with admin.cursor() as cursor:
            cursor.execute(
                """
                CREATE OR REPLACE FUNCTION public.qa_fail_one_distribution_outbox()
                RETURNS TRIGGER LANGUAGE plpgsql AS $body$
                BEGIN
                    IF NEW.tenant_id = %s::uuid THEN
                        RAISE EXCEPTION 'qa injected candidate failure';
                    END IF;
                    RETURN NEW;
                END;
                $body$
                """,
                (str(tenant_b),),
            )
            cursor.execute(
                """
                CREATE TRIGGER qa_fail_one_distribution_outbox
                BEFORE INSERT ON public.domain_outbox
                FOR EACH ROW EXECUTE FUNCTION
                    public.qa_fail_one_distribution_outbox()
                """
            )
        admin.commit()

        failed_target_id = None
        successes = 0
        for _ in range(3):
            claim = distributor.claim_target(worker_id=f"worker-fault-{suffix}")
            assert claim is not None
            with admin.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT tenant_id
                    FROM platform_control.review_distribution_targets
                    WHERE id=%s
                    """,
                    (claim["target_id"],),
                )
                claimed_tenant = cursor.fetchone()[0]
            admin.rollback()
            if str(claimed_tenant) == str(tenant_b):
                with pytest.raises(ReviewDistributorError):
                    distributor.apply_target(
                        target_id=claim["target_id"],
                        fencing_token=claim["fencing_token"],
                    )
                failed = distributor.fail_target(
                    target_id=claim["target_id"],
                    fencing_token=claim["fencing_token"],
                    error_code="qa_injected_failure",
                )
                assert failed["status"] == "retry"
                failed_target_id = claim["target_id"]
                with admin.cursor() as cursor:
                    cursor.execute(
                        """
                        UPDATE platform_control.review_distribution_targets
                        SET next_attempt_at=NOW() + INTERVAL '1 hour'
                        WHERE id=%s
                        """,
                        (failed_target_id,),
                    )
                admin.commit()
            else:
                distributor.apply_target(
                    target_id=claim["target_id"],
                    fencing_token=claim["fencing_token"],
                )
                successes += 1
        assert successes == 2
        assert failed_target_id is not None
        with admin.cursor() as cursor:
            cursor.execute(
                """
                SELECT status
                FROM platform_control.review_distribution_batches
                WHERE inbox_id=%s
                """,
                (str(inbox_ids[2]),),
            )
            assert cursor.fetchone()[0] == "running"
            cursor.execute(
                "DROP TRIGGER qa_fail_one_distribution_outbox ON public.domain_outbox"
            )
            cursor.execute("DROP FUNCTION public.qa_fail_one_distribution_outbox()")
            cursor.execute(
                """
                SELECT COUNT(*)
                FROM public.tenant_resource_grants
                WHERE tenant_id=%s AND event_edition_id=%s
                """,
                (str(tenant_b), str(edition_ids[2])),
            )
            assert cursor.fetchone()[0] == 0
            cursor.execute(
                """
                SELECT COUNT(*)
                FROM public.tenant_candidates
                WHERE tenant_id=%s AND event_edition_id=%s
                """,
                (str(tenant_b), str(edition_ids[2])),
            )
            assert cursor.fetchone()[0] == 0
            cursor.execute(
                """
                SELECT COUNT(*)
                FROM public.domain_outbox
                WHERE tenant_id=%s AND correlation_id=%s
                """,
                (str(tenant_b), str(upstream_ids[2])),
            )
            assert cursor.fetchone()[0] == 0
            cursor.execute(
                """
                SELECT status, result_grant_id, result_candidate_id
                FROM platform_control.review_distribution_targets
                WHERE id=%s
                """,
                (failed_target_id,),
            )
            assert cursor.fetchone() == ("retry", None, None)
            cursor.execute(
                """
                UPDATE platform_control.review_distribution_targets
                SET next_attempt_at=NOW()
                WHERE id=%s
                """,
                (failed_target_id,),
            )
        admin.commit()
        retry_claim = distributor.claim_target(worker_id=f"worker-retry-{suffix}")
        assert retry_claim is not None
        assert retry_claim["target_id"] == failed_target_id
        assert (
            distributor.apply_target(
                target_id=retry_claim["target_id"],
                fencing_token=retry_claim["fencing_token"],
            )["outcome_code"]
            == "created"
        )

        insert_realtime_fixture(3)
        distributor.expand_inbox(inbox_id=inbox_ids[3])
        with admin.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO public.tenant_resource_grants(
                    tenant_id, event_edition_id, trigger_source_document_id,
                    trigger_collection_mode, policy, policy_version,
                    status, grant_source
                ) VALUES (
                    %s, %s, %s, 'realtime_signal', 'shared_competition',
                    'shared-competition/1.0.0', 'active', 'qa-regrant'
                ) RETURNING id
                """,
                (str(tenant_b), str(edition_ids[3]), str(source_ids[3])),
            )
            revoked_grant = cursor.fetchone()[0]
            cursor.execute(
                """
                INSERT INTO public.tenant_candidates(
                    tenant_id, grant_id, event_edition_id, candidate_status
                ) VALUES (%s, %s, %s, 'open') RETURNING id
                """,
                (str(tenant_b), str(revoked_grant), str(edition_ids[3])),
            )
            withdrawn_candidate = cursor.fetchone()[0]
            cursor.execute(
                """
                UPDATE public.tenant_resource_grants
                SET status='revoked', revoked_at=NOW(), version=version+1
                WHERE id=%s
                """,
                (str(revoked_grant),),
            )
        admin.commit()
        terminal_outcomes = []
        for _ in range(3):
            claim = distributor.claim_target(worker_id=f"worker-terminal-{suffix}")
            assert claim is not None
            terminal_outcomes.append(
                distributor.apply_target(
                    target_id=claim["target_id"],
                    fencing_token=claim["fencing_token"],
                )
            )
        blocked = [
            result
            for result in terminal_outcomes
            if result["outcome_code"] == "blocked_regrant_required"
        ]
        assert len(blocked) == 1
        assert blocked[0]["candidate_id"] == str(withdrawn_candidate)
        with admin.cursor() as cursor:
            cursor.execute(
                """
                SELECT candidate_status
                FROM public.tenant_candidates WHERE id=%s
                """,
                (str(withdrawn_candidate),),
            )
            assert cursor.fetchone()[0] == "withdrawn"
            cursor.execute(
                """
                SELECT COUNT(*)
                FROM public.tenant_resource_grants
                WHERE tenant_id=%s AND event_edition_id=%s
                """,
                (str(tenant_b), str(edition_ids[3])),
            )
            assert cursor.fetchone()[0] == 1
            cursor.execute(
                """
                SELECT status
                FROM platform_control.review_distribution_batches
                WHERE inbox_id=%s
                """,
                (str(inbox_ids[3]),),
            )
            assert cursor.fetchone()[0] == "partial"
        admin.rollback()

        insert_realtime_fixture(4)
        with ThreadPoolExecutor(max_workers=8) as executor:
            concurrent_expansions = list(
                executor.map(
                    lambda _: distributor.expand_inbox(inbox_id=inbox_ids[4]),
                    range(8),
                )
            )
        assert len({item["batch_id"] for item in concurrent_expansions}) == 1
        assert sum(not item["replayed"] for item in concurrent_expansions) == 1
        assert {item["target_count"] for item in concurrent_expansions} == {3}

        race_claims = []
        for _ in range(3):
            claim = distributor.claim_target(worker_id=f"worker-race-{suffix}")
            assert claim is not None
            race_claims.append(claim)

        with admin.cursor() as cursor:
            cursor.execute(
                """
                UPDATE platform_control.review_distribution_tenant_settings
                SET status='disabled', revoked_at=NOW(), version=version+1
                WHERE tenant_id=%s
                """,
                (str(tenant_c),),
            )
        admin.commit()
        with ThreadPoolExecutor(max_workers=3) as executor:
            revalidated_outcomes = list(
                executor.map(
                    lambda claim: distributor.apply_target(
                        target_id=claim["target_id"],
                        fencing_token=claim["fencing_token"],
                    ),
                    race_claims,
                )
            )
        assert sorted(result["outcome_code"] for result in revalidated_outcomes) == [
            "created",
            "created",
            "tenant_or_ruleset_ineligible",
        ]
        with admin.cursor() as cursor:
            cursor.execute(
                """
                SELECT status
                FROM platform_control.review_distribution_batches
                WHERE inbox_id=%s
                """,
                (str(inbox_ids[4]),),
            )
            assert cursor.fetchone()[0] == "completed"
            for table_name in (
                "tenant_resource_grants",
                "tenant_candidates",
            ):
                cursor.execute(
                    sql.SQL(
                        "SELECT COUNT(*) FROM public.{} "
                        "WHERE tenant_id=%s AND event_edition_id=%s"
                    ).format(sql.Identifier(table_name)),
                    (str(tenant_c), str(edition_ids[4])),
                )
                assert cursor.fetchone()[0] == 0
            cursor.execute(
                """
                SELECT COUNT(*) FROM public.domain_outbox
                WHERE tenant_id=%s AND correlation_id=%s
                """,
                (str(tenant_c), str(upstream_ids[4])),
            )
            assert cursor.fetchone()[0] == 0
            cursor.execute(
                """
                SELECT status, result_grant_id, result_candidate_id
                FROM platform_control.review_distribution_targets
                WHERE tenant_id=%s AND event_edition_id=%s
                """,
                (str(tenant_c), str(edition_ids[4])),
            )
            assert cursor.fetchone() == ("skipped", None, None)
        admin.rollback()

        with admin.cursor() as cursor:
            cursor.execute(
                """
                UPDATE platform_control.review_distribution_tenant_settings
                SET status='disabled', revoked_at=NOW(), version=version+1
                WHERE tenant_id=%s
                """,
                (str(tenant_b),),
            )
        admin.commit()
        insert_realtime_fixture(5)
        exhausted_batch = distributor.expand_inbox(inbox_id=inbox_ids[5])
        assert exhausted_batch["target_count"] == 1
        exhausted_target_id = None
        for _ in range(5):
            crash_claim = distributor.claim_target(worker_id=f"worker-crash-{suffix}")
            assert crash_claim is not None
            exhausted_target_id = crash_claim["target_id"]
            with admin.cursor() as cursor:
                cursor.execute(
                    """
                    UPDATE platform_control.review_distribution_targets
                    SET lease_expires_at=NOW() - INTERVAL '1 second'
                    WHERE id=%s
                    """,
                    (exhausted_target_id,),
                )
            admin.commit()
        assert distributor.claim_target(worker_id=f"worker-reap-{suffix}") is None
        with admin.cursor() as cursor:
            cursor.execute(
                """
                SELECT target.status, target.attempt_count,
                       target.last_error_code, batch.status
                FROM platform_control.review_distribution_targets AS target
                JOIN platform_control.review_distribution_batches AS batch
                  ON batch.id=target.batch_id
                WHERE target.id=%s
                """,
                (exhausted_target_id,),
            )
            assert cursor.fetchone() == ("dead", 5, "lease_expired", "dead")
        admin.rollback()
    finally:
        if distributor_connection is not None:
            distributor_connection.rollback()
            distributor_connection.close()
        admin.rollback()
        try:
            with admin.cursor() as cursor:
                cursor.execute(
                    "DROP TRIGGER IF EXISTS qa_fail_one_distribution_outbox "
                    "ON public.domain_outbox"
                )
                cursor.execute(
                    "DROP FUNCTION IF EXISTS public.qa_fail_one_distribution_outbox()"
                )
                cursor.execute(
                    """
                    DELETE FROM platform_control.review_distribution_targets
                    WHERE tenant_id=ANY(%s::uuid[])
                    """,
                    ([str(value) for value in tenant_ids],),
                )
                cursor.execute(
                    """
                    DELETE FROM platform_control.review_distribution_batches
                    WHERE inbox_id=ANY(%s::uuid[])
                    """,
                    ([str(value) for value in inbox_ids],),
                )
                cursor.execute(
                    "ALTER TABLE platform_control.review_distribution_inbox "
                    "DISABLE TRIGGER trg_review_distribution_inbox_immutable"
                )
                cursor.execute(
                    """
                    DELETE FROM platform_control.review_distribution_inbox
                    WHERE id=ANY(%s::uuid[])
                    """,
                    ([str(value) for value in inbox_ids],),
                )
                cursor.execute(
                    "ALTER TABLE platform_control.review_distribution_inbox "
                    "ENABLE TRIGGER trg_review_distribution_inbox_immutable"
                )
                cursor.execute(
                    """
                    DELETE FROM platform_control.review_distribution_tenant_settings
                    WHERE tenant_id=ANY(%s::uuid[])
                    """,
                    ([str(value) for value in tenant_ids],),
                )
                for table_name in (
                    "domain_outbox",
                    "tenant_candidate_score_snapshots",
                    "tenant_reviews",
                    "tenant_candidates",
                    "tenant_resource_grants",
                ):
                    cursor.execute(
                        sql.SQL(
                            "DELETE FROM public.{} WHERE tenant_id=ANY(%s::uuid[])"
                        ).format(sql.Identifier(table_name)),
                        ([str(value) for value in tenant_ids],),
                    )
                cursor.execute(
                    "ALTER TABLE public.tenant_review_ruleset_activations "
                    "DISABLE TRIGGER trg_guard_review_ruleset_activation"
                )
                cursor.execute(
                    """
                    DELETE FROM public.tenant_review_ruleset_activations
                    WHERE tenant_id=ANY(%s::uuid[])
                    """,
                    ([str(value) for value in tenant_ids],),
                )
                cursor.execute(
                    "ALTER TABLE public.tenant_review_ruleset_activations "
                    "ENABLE TRIGGER trg_guard_review_ruleset_activation"
                )
                cursor.execute(
                    """
                    DELETE FROM public.event_sources
                    WHERE source_document_id=ANY(%s::uuid[])
                    """,
                    ([str(value) for value in source_ids],),
                )
                cursor.execute(
                    """
                    DELETE FROM public.source_documents
                    WHERE id=ANY(%s::uuid[])
                    """,
                    ([str(value) for value in source_ids],),
                )
                cursor.execute(
                    """
                    DELETE FROM public.event_editions
                    WHERE id=ANY(%s::uuid[]) OR normalized_name=%s
                    """,
                    (
                        [str(value) for value in edition_ids],
                        f"qa-historical-edition-{suffix}",
                    ),
                )
                cursor.execute(
                    "DELETE FROM public.tenants WHERE id=ANY(%s::uuid[])",
                    ([str(value) for value in tenant_ids],),
                )
                for role_name in (distributor_role, ordinary_role):
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
                if public_create_before is True:
                    cursor.execute("GRANT CREATE ON SCHEMA public TO PUBLIC")
                elif public_create_before is False:
                    cursor.execute("REVOKE CREATE ON SCHEMA public FROM PUBLIC")
            admin.commit()
        finally:
            admin.close()
