"""PostgreSQL tenant RLS integration contract.

This module is deliberately tied to the disposable QA database.  All isolation
assertions run as a temporary login role which is neither a superuser nor a
table owner; the QA superuser is used only to create and reliably clean up the
fixture role and rows.
"""

from __future__ import annotations

import os
import uuid
from urllib.parse import urlsplit

import pytest


RUN = os.getenv("RUN_MIGRATION_INTEGRATION") == "1"
TENANT_TABLES = ("tenants", "tenant_memberships")


def _guarded_qa_database_url() -> str:
    if (
        os.getenv("ENVIRONMENT") != "qa"
        or os.getenv("ALLOW_DESTRUCTIVE_TESTS") != "1"
    ):
        raise RuntimeError("tenant RLS test requires guarded QA environment")
    database_url = os.environ["DATABASE_URL"]
    database_name = urlsplit(database_url).path.lstrip("/")
    if "_lease_qa_" not in database_name:
        raise RuntimeError(f"tenant RLS test refused unsafe database: {database_name}")
    return database_url


def _connect_as_role(admin_connection, role_name: str, password: str):
    import psycopg2

    dsn = admin_connection.get_dsn_parameters()
    return psycopg2.connect(
        dbname=dsn["dbname"],
        host=dsn.get("host"),
        port=dsn.get("port"),
        user=role_name,
        password=password,
        connect_timeout=5,
    )


def _set_local_tenant(connection, tenant_id: str) -> None:
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT set_config('app.tenant_id', %s, true)",
            (tenant_id,),
        )


def _visible_fixture_ids(
    connection, table: str, tenant_a: uuid.UUID, tenant_b: uuid.UUID
):
    if table == "tenants":
        sql = "SELECT id FROM tenants WHERE id IN (%s, %s) ORDER BY id"
    elif table == "tenant_memberships":
        sql = (
            "SELECT tenant_id FROM tenant_memberships "
            "WHERE tenant_id IN (%s, %s) ORDER BY tenant_id"
        )
    else:  # Keep table names out of SQL unless they are explicitly allow-listed.
        raise AssertionError(f"unsupported tenant table: {table}")
    with connection.cursor() as cursor:
        cursor.execute(sql, (str(tenant_a), str(tenant_b)))
        return [str(row[0]) for row in cursor.fetchall()]


def _assert_transaction_context_cleared(connection) -> None:
    with connection.cursor() as cursor:
        cursor.execute("SELECT current_setting('app.tenant_id', true)")
        value = cursor.fetchone()[0]
    # PostgreSQL returns either NULL (never defined) or an empty string after a
    # transaction-local custom setting has gone out of scope.
    assert value in (None, "")
    connection.rollback()


@pytest.mark.skipif(not RUN, reason="requires disposable QA PostgreSQL 15")
def test_tenant_rls_isolation_with_non_owner_role_and_pool_reuse():
    import psycopg2
    from psycopg2 import errors, sql

    database_url = _guarded_qa_database_url()
    suffix = uuid.uuid4().hex
    role_name = f"qa_tenant_rls_{suffix}"
    role_password = f"qa-{uuid.uuid4().hex}"
    tenant_a = uuid.uuid4()
    tenant_b = uuid.uuid4()
    user_public_id = uuid.uuid4()
    user_b_public_id = uuid.uuid4()
    membership_a = uuid.uuid4()
    membership_b = uuid.uuid4()
    forged_membership = uuid.uuid4()
    no_context_tenant = uuid.uuid4()
    username = f"qa-tenant-rls-{suffix}"
    admin = psycopg2.connect(database_url)
    tenant_connection = None
    missing_context_connection = None
    setup_committed = False

    try:
        with admin.cursor() as cursor:
            cursor.execute("SHOW server_version_num")
            assert 150000 <= int(cursor.fetchone()[0]) < 160000
            cursor.execute(
                sql.SQL(
                    "CREATE ROLE {} LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE "
                    "NOINHERIT NOBYPASSRLS PASSWORD %s"
                ).format(sql.Identifier(role_name)),
                (role_password,),
            )
            cursor.execute(
                sql.SQL("GRANT USAGE ON SCHEMA public TO {}").format(
                    sql.Identifier(role_name)
                )
            )
            cursor.execute(
                sql.SQL(
                    "GRANT SELECT, INSERT, UPDATE ON TABLE tenants TO {}"
                ).format(sql.Identifier(role_name))
            )
            cursor.execute(
                sql.SQL(
                    "GRANT SELECT, INSERT ON TABLE tenant_memberships TO {}"
                ).format(sql.Identifier(role_name))
            )
            cursor.execute(
                """
                INSERT INTO users(public_id, username, password_hash, role, enabled)
                VALUES (%s, %s, 'qa-not-a-real-password', 'member', TRUE),
                       (%s, %s, 'qa-not-a-real-password', 'member', TRUE)
                """,
                (
                    str(user_public_id),
                    username,
                    str(user_b_public_id),
                    f"{username}-b",
                ),
            )
        admin.commit()
        setup_committed = True

        tenant_connection = _connect_as_role(admin, role_name, role_password)

        with tenant_connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT current_user, session_user, r.rolsuper, r.rolbypassrls
                FROM pg_roles r
                WHERE r.rolname = current_user
                """
            )
            current_user, session_user, is_superuser, bypasses_rls = cursor.fetchone()
            assert current_user == role_name
            assert session_user == role_name
            assert is_superuser is False
            assert bypasses_rls is False

            cursor.execute(
                """
                SELECT c.relname, c.relrowsecurity, c.relforcerowsecurity,
                       pg_get_userbyid(c.relowner)
                FROM pg_class c
                JOIN pg_namespace n ON n.oid = c.relnamespace
                WHERE n.nspname = 'public' AND c.relname = ANY(%s)
                ORDER BY c.relname
                """,
                (list(TENANT_TABLES),),
            )
            rls_state = cursor.fetchall()
        tenant_connection.rollback()
        assert [row[0] for row in rls_state] == sorted(TENANT_TABLES)
        for _table, enabled, forced, owner in rls_state:
            assert enabled is True
            assert forced is True
            assert owner != role_name

        # The same non-owner session creates A and B only while the matching
        # transaction-local context is active.
        _set_local_tenant(tenant_connection, str(tenant_a))
        with tenant_connection.cursor() as cursor:
            cursor.execute(
                "INSERT INTO tenants(id, code, name) VALUES (%s, %s, %s)",
                (str(tenant_a), f"qa-a-{suffix}", "QA tenant A"),
            )
            cursor.execute(
                """
                INSERT INTO tenant_memberships(
                    id, tenant_id, user_id, role, status
                ) VALUES (%s, %s, %s, 'sales', 'active')
                """,
                (str(membership_a), str(tenant_a), str(user_public_id)),
            )
            cursor.execute(
                "UPDATE tenants SET name = 'QA tenant A updated' WHERE id = %s",
                (str(tenant_a),),
            )
            assert cursor.rowcount == 1
        tenant_connection.commit()
        _assert_transaction_context_cleared(tenant_connection)

        _set_local_tenant(tenant_connection, str(tenant_b))
        with tenant_connection.cursor() as cursor:
            cursor.execute(
                "INSERT INTO tenants(id, code, name) VALUES (%s, %s, %s)",
                (str(tenant_b), f"qa-b-{suffix}", "QA tenant B"),
            )
            cursor.execute(
                """
                INSERT INTO tenant_memberships(
                    id, tenant_id, user_id, role, status
                ) VALUES (%s, %s, %s, 'sales', 'active')
                """,
                (str(membership_b), str(tenant_b), str(user_b_public_id)),
            )
        tenant_connection.commit()
        _assert_transaction_context_cleared(tenant_connection)
        assert _visible_fixture_ids(
            tenant_connection, "tenants", tenant_a, tenant_b
        ) == []
        tenant_connection.rollback()

        # A brand-new session distinguishes a truly missing custom setting
        # from PostgreSQL's empty-string representation after SET LOCAL ends.
        missing_context_connection = _connect_as_role(
            admin, role_name, role_password
        )
        with missing_context_connection.cursor() as cursor:
            cursor.execute("SELECT current_setting('app.tenant_id', true)")
            assert cursor.fetchone()[0] is None
        assert _visible_fixture_ids(
            missing_context_connection, "tenants", tenant_a, tenant_b
        ) == []
        with pytest.raises(errors.InsufficientPrivilege):
            with missing_context_connection.cursor() as cursor:
                cursor.execute(
                    "INSERT INTO tenants(id, code, name) VALUES (%s, %s, %s)",
                    (
                        str(no_context_tenant),
                        f"qa-no-context-{suffix}",
                        "must be rejected",
                    ),
                )
        missing_context_connection.rollback()
        missing_context_connection.close()
        missing_context_connection = None

        _set_local_tenant(tenant_connection, str(tenant_a))
        assert _visible_fixture_ids(
            tenant_connection, "tenants", tenant_a, tenant_b
        ) == [str(tenant_a)]
        assert _visible_fixture_ids(
            tenant_connection, "tenant_memberships", tenant_a, tenant_b
        ) == [str(tenant_a)]
        with tenant_connection.cursor() as cursor:
            cursor.execute(
                "UPDATE tenants SET name = 'must stay hidden' WHERE id = %s",
                (str(tenant_b),),
            )
            assert cursor.rowcount == 0
        tenant_connection.commit()
        _assert_transaction_context_cleared(tenant_connection)

        # A caller cannot widen its scope by explicitly supplying B's tenant_id.
        _set_local_tenant(tenant_connection, str(tenant_a))
        with pytest.raises(errors.InsufficientPrivilege):
            with tenant_connection.cursor() as cursor:
                cursor.execute(
                    """
                    INSERT INTO tenant_memberships(
                        id, tenant_id, user_id, role, status
                    ) VALUES (%s, %s, %s, 'sales', 'active')
                    """,
                    (
                        str(forged_membership),
                        str(tenant_b),
                        str(user_public_id),
                    ),
                )
        tenant_connection.rollback()
        _assert_transaction_context_cleared(tenant_connection)

        _set_local_tenant(tenant_connection, str(tenant_b))
        with tenant_connection.cursor() as cursor:
            cursor.execute(
                "SELECT COUNT(*) FROM tenant_memberships WHERE id = %s",
                (str(forged_membership),),
            )
            assert cursor.fetchone()[0] == 0
        tenant_connection.rollback()
        _assert_transaction_context_cleared(tenant_connection)

        # Pool-reuse simulation on this exact connection: after rollback the
        # next borrower receives no tenant context and sees no fixture rows.
        _set_local_tenant(tenant_connection, str(tenant_a))
        assert _visible_fixture_ids(
            tenant_connection, "tenants", tenant_a, tenant_b
        ) == [str(tenant_a)]
        tenant_connection.rollback()
        _assert_transaction_context_cleared(tenant_connection)
        assert _visible_fixture_ids(
            tenant_connection, "tenants", tenant_a, tenant_b
        ) == []
        tenant_connection.rollback()

        # Empty and malformed context also fail closed for reads and reject
        # writes.  In particular, malformed input must never be cast in
        # a way which turns a policy check into tenant-independent access.
        for tenant_context in ("", "not-a-canonical-uuid"):
            _set_local_tenant(tenant_connection, tenant_context)
            assert _visible_fixture_ids(
                tenant_connection, "tenants", tenant_a, tenant_b
            ) == []
            with pytest.raises(errors.InsufficientPrivilege):
                with tenant_connection.cursor() as cursor:
                    cursor.execute(
                        "INSERT INTO tenants(id, code, name) VALUES (%s, %s, %s)",
                        (
                            str(no_context_tenant),
                            f"qa-no-context-{suffix}",
                            "must be rejected",
                        ),
                    )
            tenant_connection.rollback()
            _assert_transaction_context_cleared(tenant_connection)
    finally:
        if missing_context_connection is not None:
            missing_context_connection.rollback()
            missing_context_connection.close()
        if tenant_connection is not None:
            tenant_connection.rollback()
            tenant_connection.close()
        try:
            admin.rollback()
            if setup_committed:
                with admin.cursor() as cursor:
                    cursor.execute(
                        "DELETE FROM tenant_memberships WHERE id = ANY(%s::uuid[])",
                        (
                            [
                                str(membership_a),
                                str(membership_b),
                                str(forged_membership),
                            ],
                        ),
                    )
                    cursor.execute(
                        "DELETE FROM tenants WHERE id = ANY(%s::uuid[])",
                        (
                            [
                                str(tenant_a),
                                str(tenant_b),
                                str(no_context_tenant),
                            ],
                        ),
                    )
                    cursor.execute(
                        "DELETE FROM users WHERE public_id = ANY(%s::uuid[])",
                        ([str(user_public_id), str(user_b_public_id)],),
                    )
                    cursor.execute(
                        sql.SQL("DROP OWNED BY {}").format(
                            sql.Identifier(role_name)
                        )
                    )
                    cursor.execute(
                        sql.SQL("DROP ROLE IF EXISTS {}").format(
                            sql.Identifier(role_name)
                        )
                    )
                admin.commit()
        finally:
            admin.close()
