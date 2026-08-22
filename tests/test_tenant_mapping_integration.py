"""Real PostgreSQL 15 proof that tenant mapping dry-run performs zero writes."""

from __future__ import annotations

import hashlib
import os
import uuid
from urllib.parse import urlsplit

import pytest

from wxsearch.tenant_mapping import (
    dry_run_database,
    inventory_database,
    validate_manifest_data,
)


RUN = os.getenv("RUN_MIGRATION_INTEGRATION") == "1"


def _guarded_qa_database_url() -> str:
    if (
        os.getenv("ENVIRONMENT") != "qa"
        or os.getenv("ALLOW_DESTRUCTIVE_TESTS") != "1"
    ):
        raise RuntimeError("tenant mapping test requires guarded QA environment")
    database_url = os.environ["DATABASE_URL"]
    database_name = urlsplit(database_url).path.lstrip("/")
    if "_lease_qa_" not in database_name:
        raise RuntimeError(
            f"tenant mapping test refused unsafe database: {database_name}"
        )
    return database_url


def _stable_uuid(namespace: int, sequence: int) -> uuid.UUID:
    return uuid.UUID(int=(namespace << 96) | sequence, version=4)


def _database_digest(connection) -> dict[str, str]:
    table_order = {
        "users": "id",
        "teams": "id",
        "tenants": "id",
        "tenant_memberships": "id",
        "schema_migrations": "version",
    }
    result = {}
    with connection.cursor() as cursor:
        for table, ordering in table_order.items():
            cursor.execute(f"SELECT * FROM public.{table} ORDER BY {ordering}")
            encoded = repr(cursor.fetchall()).encode("utf-8")
            result[table] = hashlib.sha256(encoded).hexdigest()
    connection.rollback()
    return result


def _qa_manifest(*, tenant_id, memberships, excluded_users, digest):
    return validate_manifest_data(
        {
            "schema_version": 1,
            "batch_id": str(_stable_uuid(0x71000000, 1)),
            "target_environment": "qa",
            "source_snapshot_at": "2026-08-23T12:00:00+08:00",
            "source_users_digest_sha256": digest,
            "approval_reference": "synthetic-qa-approval",
            "policy": {
                "expected_tenant_count": 1,
                "expected_sales_per_tenant": 1,
                "expected_reviewers_per_tenant": 1,
                "allow_cross_tenant_users": False,
            },
            "tenants": [
                {
                    "tenant_id": str(tenant_id),
                    "tenant_code": f"qa-mapping-{tenant_id.hex[:12]}",
                    "tenant_name": "Synthetic QA tenant",
                    "default_visibility_policy": "shared_competition",
                    "initial_status": "disabled",
                    "observed_legacy_team_id": memberships[0][
                        "legacy_team_id_observed"
                    ],
                    "expected_sales_count": 1,
                    "expected_reviewer_count": 1,
                    "company_approval_reference": "synthetic-company-approval",
                    "memberships": memberships,
                }
            ],
            "excluded_users": excluded_users,
        }
    )


@pytest.mark.skipif(not RUN, reason="requires migrations 021-022 and QA PG15")
def test_pg15_dry_run_keeps_all_mapping_related_content_unchanged():
    import psycopg2

    database_url = _guarded_qa_database_url()
    suffix = uuid.uuid4().hex
    tenant_id = uuid.uuid4()
    team_id = None
    created_user_ids = []
    admin = psycopg2.connect(database_url)
    mapping_connection = None
    try:
        with admin.cursor() as cursor:
            cursor.execute("SHOW server_version_num")
            assert 150000 <= int(cursor.fetchone()[0]) < 160000
            cursor.execute(
                "SELECT version FROM schema_migrations "
                "WHERE version IN ('021', '022') ORDER BY version"
            )
            assert [row[0] for row in cursor.fetchall()] == ["021", "022"]
            cursor.execute(
                "SELECT to_regprocedure('public.app_list_active_tenants(uuid)')"
            )
            assert cursor.fetchone()[0] is not None
            cursor.execute(
                "INSERT INTO teams(name) VALUES (%s) RETURNING id",
                (f"qa-mapping-team-{suffix}",),
            )
            team_id = cursor.fetchone()[0]

            seeded_users = []
            for sequence, (legacy_role, enabled) in enumerate(
                (("admin", True), ("member", True), ("super", True)), 1
            ):
                public_id = uuid.uuid4()
                cursor.execute(
                    """
                    INSERT INTO users(
                        public_id, username, password_hash, team_id, role, enabled
                    ) VALUES (%s, %s, 'qa-synthetic-hash', %s, %s, %s)
                    RETURNING id
                    """,
                    (
                        str(public_id),
                        f"qa-mapping-{sequence}-{suffix}",
                        team_id if legacy_role != "super" else None,
                        legacy_role,
                        enabled,
                    ),
                )
                legacy_id = cursor.fetchone()[0]
                created_user_ids.append(legacy_id)
                seeded_users.append((legacy_id, public_id, legacy_role))
        admin.commit()

        before = _database_digest(admin)
        mapping_connection = psycopg2.connect(database_url)
        inventory = inventory_database(mapping_connection)
        assert inventory["migration_head"] == "022"
        assert inventory["mapping_ready"] is True

        with admin.cursor() as cursor:
            cursor.execute(
                """
                SELECT id, public_id, enabled, role, team_id
                FROM users ORDER BY id
                """
            )
            all_users = cursor.fetchall()
        admin.rollback()

        member_by_role = {role: (legacy_id, public_id) for legacy_id, public_id, role in seeded_users}
        memberships = []
        for sequence, (legacy_role, tenant_role) in enumerate(
            (("admin", "resource_reviewer"), ("member", "sales")), 1
        ):
            legacy_id, public_id = member_by_role[legacy_role]
            memberships.append(
                {
                    "membership_id": str(_stable_uuid(0x73000000, sequence)),
                    "legacy_user_id_observed": legacy_id,
                    "user_public_id": str(public_id),
                    "legacy_team_id_observed": team_id,
                    "legacy_role_observed": legacy_role,
                    "tenant_role": tenant_role,
                    "membership_status": "active",
                    "mapping_action": "create",
                    "reason_code": "company_roster_confirmed",
                    "company_approval_reference": "synthetic-member-approval",
                }
            )

        member_ids = {item["legacy_user_id_observed"] for item in memberships}
        excluded_users = []
        for legacy_id, public_id, enabled, role, observed_team_id in all_users:
            if not enabled or legacy_id in member_ids:
                continue
            excluded_users.append(
                {
                    "legacy_user_id_observed": legacy_id,
                    "user_public_id": str(public_id),
                    "legacy_team_id_observed": observed_team_id,
                    "legacy_role_observed": role,
                    "classification": (
                        "platform_only" if role == "super" else "legacy_only"
                    ),
                    "reason_code": (
                        "platform_administration_only"
                        if role == "super"
                        else "outside_synthetic_mapping"
                    ),
                    "approval_reference": "synthetic-exclusion-approval",
                }
            )

        manifest = _qa_manifest(
            tenant_id=tenant_id,
            memberships=memberships,
            excluded_users=excluded_users,
            digest=inventory["source_users_digest_sha256"],
        )
        result = dry_run_database(mapping_connection, manifest)
        assert result["status"] == "ready"
        assert result["issue_count"] == 0
        assert result["actions"] == {
            "tenant_create": 1,
            "tenant_noop": 0,
            "membership_create": 2,
            "membership_noop": 0,
        }
        assert mapping_connection.get_transaction_status() == 0

        after = _database_digest(admin)
        assert after == before
    finally:
        if mapping_connection is not None:
            mapping_connection.rollback()
            mapping_connection.close()
        admin.rollback()
        with admin.cursor() as cursor:
            cursor.execute(
                "DELETE FROM tenant_memberships WHERE tenant_id=%s",
                (str(tenant_id),),
            )
            cursor.execute("DELETE FROM tenants WHERE id=%s", (str(tenant_id),))
            if created_user_ids:
                cursor.execute(
                    "DELETE FROM users WHERE id = ANY(%s)",
                    (created_user_ids,),
                )
            if team_id is not None:
                cursor.execute("DELETE FROM teams WHERE id=%s", (team_id,))
        admin.commit()
        admin.close()
