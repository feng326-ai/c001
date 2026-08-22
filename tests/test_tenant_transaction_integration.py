"""Real PostgreSQL 15 contract for tenant transaction authorization.

The test creates a disposable, independent runtime LOGIN role in the guarded QA
database.  It never connects to staging or production and leaves no role or
fixture data behind.
"""

from __future__ import annotations

import os
import threading
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from urllib.parse import quote, urlsplit, urlunsplit

import pytest


RUN = os.getenv("RUN_MIGRATION_INTEGRATION") == "1"


def _guarded_qa_database_url() -> str:
    if (
        os.getenv("ENVIRONMENT") != "qa"
        or os.getenv("ALLOW_DESTRUCTIVE_TESTS") != "1"
    ):
        raise RuntimeError("tenant transaction test requires guarded QA environment")
    database_url = os.environ["DATABASE_URL"]
    database_name = urlsplit(database_url).path.lstrip("/")
    if "_lease_qa_" not in database_name:
        raise RuntimeError(
            f"tenant transaction test refused unsafe database: {database_name}"
        )
    return database_url


def _role_database_url(admin_url: str, role_name: str, password: str) -> str:
    parsed = urlsplit(admin_url)
    host = parsed.hostname or "localhost"
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    port = f":{parsed.port}" if parsed.port else ""
    netloc = f"{quote(role_name)}:{quote(password)}@{host}{port}"
    return urlunsplit(
        (parsed.scheme, netloc, parsed.path, parsed.query, parsed.fragment)
    )


@dataclass(frozen=True)
class RuntimeFixture:
    admin_url: str
    runtime_url: str
    role_name: str
    tenants: dict[str, uuid.UUID]
    users: dict[str, tuple[int, uuid.UUID]]
    memberships: dict[str, uuid.UUID]
    probe_table: str
    probe_rows: dict[str, uuid.UUID]


@pytest.fixture(scope="module")
def runtime_fixture():
    import psycopg2
    from psycopg2 import sql

    admin_url = _guarded_qa_database_url()
    suffix = uuid.uuid4().hex
    role_name = f"qa_tenant_tx_{suffix}"
    role_password = f"Qa{uuid.uuid4().hex}"
    runtime_url = _role_database_url(admin_url, role_name, role_password)
    tenants = {
        "a": uuid.uuid4(),
        "b": uuid.uuid4(),
        "disabled": uuid.uuid4(),
    }
    user_public_ids = {
        "ab": uuid.uuid4(),
        "a_only": uuid.uuid4(),
        "disabled_user": uuid.uuid4(),
        "disabled_membership": uuid.uuid4(),
        "disabled_tenant": uuid.uuid4(),
        "other": uuid.uuid4(),
    }
    memberships = {
        "ab_a": uuid.uuid4(),
        "ab_b": uuid.uuid4(),
        "a_only": uuid.uuid4(),
        "disabled_user": uuid.uuid4(),
        "disabled_membership": uuid.uuid4(),
        "disabled_tenant": uuid.uuid4(),
        "other_b": uuid.uuid4(),
    }
    probe_table = f"qa_tenant_tx_probe_{suffix}"
    probe_rows = {"a": uuid.uuid4(), "b": uuid.uuid4()}
    admin = psycopg2.connect(admin_url)
    setup_committed = False
    user_ids = {}
    try:
        with admin.cursor() as cursor:
            cursor.execute("SHOW server_version_num")
            assert 150000 <= int(cursor.fetchone()[0]) < 160000
            cursor.execute(
                "SELECT to_regprocedure('public.app_list_active_tenants(uuid)')"
            )
            assert cursor.fetchone()[0] is not None, "migration 022 is required"
            cursor.execute(
                sql.SQL(
                    "CREATE ROLE {} LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE "
                    "NOINHERIT NOBYPASSRLS PASSWORD %s"
                ).format(sql.Identifier(role_name)),
                (role_password,),
            )
            cursor.execute(
                sql.SQL("GRANT CONNECT ON DATABASE {} TO {}").format(
                    sql.Identifier(admin.get_dsn_parameters()["dbname"]),
                    sql.Identifier(role_name),
                )
            )
            cursor.execute(
                sql.SQL("GRANT USAGE ON SCHEMA public TO {}").format(
                    sql.Identifier(role_name)
                )
            )
            cursor.execute(
                sql.SQL(
                    "GRANT SELECT (id, public_id, enabled) ON users TO {}"
                ).format(sql.Identifier(role_name))
            )
            # The legacy identity lookup still locks users, whose update
            # permission is needed by existing user administration. Tenant
            # identity authorization itself goes through the 022 narrow
            # function, so tenants/memberships stay strictly read-only.
            cursor.execute(
                sql.SQL("GRANT UPDATE (enabled) ON users TO {}").format(
                    sql.Identifier(role_name)
                )
            )
            cursor.execute(
                sql.SQL("GRANT SELECT ON tenants TO {}").format(
                    sql.Identifier(role_name)
                )
            )
            cursor.execute(
                sql.SQL("GRANT SELECT ON tenant_memberships TO {}").format(
                    sql.Identifier(role_name)
                )
            )
            cursor.execute(
                sql.SQL(
                    "GRANT EXECUTE ON FUNCTION "
                    "public.app_list_active_tenants(uuid) TO {}"
                ).format(sql.Identifier(role_name))
            )
            cursor.execute(
                """
                INSERT INTO tenants(id, code, name, status)
                VALUES (%s, %s, 'Tenant A', 'active'),
                       (%s, %s, 'Tenant B', 'active'),
                       (%s, %s, 'Disabled tenant', 'disabled')
                """,
                (
                    str(tenants["a"]),
                    f"qa-a-{suffix}",
                    str(tenants["b"]),
                    f"qa-b-{suffix}",
                    str(tenants["disabled"]),
                    f"qa-disabled-{suffix}",
                ),
            )
            for key, public_id in user_public_ids.items():
                cursor.execute(
                    """
                    INSERT INTO users(
                        public_id, username, password_hash, role, enabled
                    ) VALUES (%s, %s, 'qa-not-a-real-password', 'member', %s)
                    RETURNING id
                    """,
                    (
                        str(public_id),
                        f"qa-tenant-tx-{key}-{suffix}",
                        key != "disabled_user",
                    ),
                )
                user_ids[key] = cursor.fetchone()[0]
            membership_rows = [
                (
                    memberships["ab_a"],
                    tenants["a"],
                    user_public_ids["ab"],
                    "sales",
                    "active",
                ),
                (
                    memberships["ab_b"],
                    tenants["b"],
                    user_public_ids["ab"],
                    "resource_reviewer",
                    "active",
                ),
                (
                    memberships["a_only"],
                    tenants["a"],
                    user_public_ids["a_only"],
                    "sales",
                    "active",
                ),
                (
                    memberships["disabled_user"],
                    tenants["a"],
                    user_public_ids["disabled_user"],
                    "sales",
                    "active",
                ),
                (
                    memberships["disabled_membership"],
                    tenants["a"],
                    user_public_ids["disabled_membership"],
                    "sales",
                    "disabled",
                ),
                (
                    memberships["disabled_tenant"],
                    tenants["disabled"],
                    user_public_ids["disabled_tenant"],
                    "sales",
                    "active",
                ),
                (
                    memberships["other_b"],
                    tenants["b"],
                    user_public_ids["other"],
                    "sales",
                    "active",
                ),
            ]
            cursor.executemany(
                """
                INSERT INTO tenant_memberships(
                    id, tenant_id, user_id, role, status
                ) VALUES (%s, %s, %s, %s, %s)
                """,
                [tuple(str(value) for value in row) for row in membership_rows],
            )
            cursor.execute(
                sql.SQL(
                    "CREATE TABLE public.{} ("
                    "id UUID PRIMARY KEY, tenant_id UUID NOT NULL, value TEXT NOT NULL)"
                ).format(sql.Identifier(probe_table))
            )
            cursor.execute(
                sql.SQL("ALTER TABLE public.{} ENABLE ROW LEVEL SECURITY").format(
                    sql.Identifier(probe_table)
                )
            )
            cursor.execute(
                sql.SQL("ALTER TABLE public.{} FORCE ROW LEVEL SECURITY").format(
                    sql.Identifier(probe_table)
                )
            )
            cursor.execute(
                sql.SQL(
                    "CREATE POLICY {} ON public.{} FOR ALL TO PUBLIC "
                    "USING (tenant_id = public.app_current_tenant_id()) "
                    "WITH CHECK (tenant_id = public.app_current_tenant_id())"
                ).format(
                    sql.Identifier(f"{probe_table}_isolation"),
                    sql.Identifier(probe_table),
                )
            )
            cursor.execute(
                sql.SQL(
                    "GRANT SELECT, INSERT, UPDATE, DELETE ON public.{} TO {}"
                ).format(
                    sql.Identifier(probe_table), sql.Identifier(role_name)
                )
            )
            cursor.executemany(
                sql.SQL(
                    "INSERT INTO public.{}(id, tenant_id, value) VALUES(%s, %s, %s)"
                ).format(sql.Identifier(probe_table)),
                [
                    (str(probe_rows[key]), str(tenants[key]), f"initial-{key}")
                    for key in ("a", "b")
                ],
            )
        admin.commit()
        setup_committed = True
        runtime_check = psycopg2.connect(runtime_url)
        try:
            with runtime_check.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT current_user, session_user, rolsuper, rolbypassrls
                    FROM pg_roles WHERE rolname = current_user
                    """
                )
                current_user, session_user, is_super, bypasses_rls = cursor.fetchone()
                assert current_user == role_name
                assert session_user == role_name
                assert is_super is False
                assert bypasses_rls is False
                cursor.execute(
                    """
                    SELECT has_table_privilege(
                               current_user, 'tenants', 'UPDATE'
                           ),
                           has_table_privilege(
                               current_user, 'tenant_memberships', 'UPDATE'
                           ),
                           has_table_privilege(
                               current_user, 'tenant_memberships', 'INSERT'
                           )
                    """
                )
                assert cursor.fetchone() == (False, False, False)
                cursor.execute(
                    """
                    SELECT pg_get_userbyid(relowner)
                    FROM pg_class
                    WHERE oid IN ('tenants'::regclass,
                                  'tenant_memberships'::regclass)
                    """
                )
                assert all(owner != role_name for (owner,) in cursor.fetchall())
            runtime_check.rollback()
        finally:
            runtime_check.close()
        yield RuntimeFixture(
            admin_url=admin_url,
            runtime_url=runtime_url,
            role_name=role_name,
            tenants=tenants,
            users={
                key: (user_ids[key], user_public_ids[key])
                for key in user_public_ids
            },
            memberships=memberships,
            probe_table=probe_table,
            probe_rows=probe_rows,
        )
    finally:
        admin.rollback()
        if setup_committed:
            with admin.cursor() as cursor:
                cursor.execute(
                    sql.SQL("DROP TABLE IF EXISTS public.{}").format(
                        sql.Identifier(probe_table)
                    )
                )
                cursor.execute(
                    "DELETE FROM tenant_memberships WHERE id = ANY(%s::uuid[])",
                    ([str(value) for value in memberships.values()],),
                )
                cursor.execute(
                    "DELETE FROM tenants WHERE id = ANY(%s::uuid[])",
                    ([str(value) for value in tenants.values()],),
                )
                cursor.execute(
                    "DELETE FROM users WHERE public_id = ANY(%s::uuid[])",
                    ([str(value) for value in user_public_ids.values()],),
                )
                cursor.execute(
                    sql.SQL("DROP OWNED BY {}").format(sql.Identifier(role_name))
                )
                cursor.execute(
                    sql.SQL("DROP ROLE IF EXISTS {}").format(
                        sql.Identifier(role_name)
                    )
                )
            admin.commit()
        admin.close()


@contextmanager
def _runtime_connector(runtime_url: str, max_connections: int):
    from unittest.mock import patch

    from wxsearch.db_connector import DatabaseConnector

    DatabaseConnector._instance = None
    with patch.dict(
        os.environ,
        {
            "DATABASE_URL": runtime_url,
            "DB_POOL_MAX": str(max_connections),
            "DB_CONNECT_TIMEOUT": "5",
        },
    ):
        connector = DatabaseConnector()
    try:
        yield connector
    finally:
        connector.pool.closeall()
        DatabaseConnector._instance = None


def _raw_pool_state(connector, tenant_ids):
    connection = connector.pool.getconn()
    try:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT pg_backend_pid(),
                       current_setting('app.tenant_id', true)
                """
            )
            backend_pid, tenant_setting = cursor.fetchone()
            cursor.execute(
                "SELECT id FROM tenants WHERE id = ANY(%s::uuid[]) ORDER BY id",
                ([str(value) for value in tenant_ids],),
            )
            visible = [row[0] for row in cursor.fetchall()]
        connection.rollback()
        return backend_pid, tenant_setting, visible
    finally:
        connector.pool.putconn(connection)


@pytest.mark.skipif(not RUN, reason="requires migrations 021-022 and QA PG15")
def test_active_membership_choices_are_limited_to_authenticated_user(runtime_fixture):
    from wxsearch.db_connector import TenantAccessDenied, TenantChoice

    fixture = runtime_fixture
    with _runtime_connector(fixture.runtime_url, 1) as connector:
        a_user_id, _public_id = fixture.users["a_only"]
        assert connector.list_active_tenants(authenticated_user_id=a_user_id) == [
            TenantChoice(
                tenant_id=fixture.tenants["a"],
                code=f"qa-a-{fixture.role_name.removeprefix('qa_tenant_tx_')}",
                name="Tenant A",
                membership_id=fixture.memberships["a_only"],
                role="sales",
            )
        ]
        ab_user_id, _public_id = fixture.users["ab"]
        choices = connector.list_active_tenants(authenticated_user_id=ab_user_id)
        assert {choice.tenant_id for choice in choices} == {
            fixture.tenants["a"],
            fixture.tenants["b"],
        }
        assert {choice.membership_id for choice in choices} == {
            fixture.memberships["ab_a"],
            fixture.memberships["ab_b"],
        }
        for denied_key in ("disabled_user",):
            with pytest.raises(TenantAccessDenied):
                connector.list_active_tenants(
                    authenticated_user_id=fixture.users[denied_key][0]
                )
        assert connector.list_active_tenants(
            authenticated_user_id=fixture.users["disabled_membership"][0]
        ) == []
        assert connector.list_active_tenants(
            authenticated_user_id=fixture.users["disabled_tenant"][0]
        ) == []


@pytest.mark.skipif(not RUN, reason="requires migrations 021-022 and QA PG15")
def test_transaction_denies_inactive_and_cross_tenant_without_context_leak(
    runtime_fixture,
):
    from wxsearch.db_connector import TenantAccessDenied

    fixture = runtime_fixture
    denied = [
        ("disabled_user", "a"),
        ("disabled_membership", "a"),
        ("disabled_tenant", "disabled"),
        ("a_only", "b"),
    ]
    with _runtime_connector(fixture.runtime_url, 1) as connector:
        initial_pid = None
        for user_key, tenant_key in denied:
            with pytest.raises(TenantAccessDenied):
                with connector.tenant_transaction(
                    authenticated_user_id=fixture.users[user_key][0],
                    requested_tenant_id=fixture.tenants[tenant_key],
                ):
                    pytest.fail("denied tenant transaction must not yield")
            pid, setting, visible = _raw_pool_state(
                connector, fixture.tenants.values()
            )
            initial_pid = initial_pid or pid
            assert pid == initial_pid
            assert setting in (None, "")
            assert visible == []


@pytest.mark.skipif(not RUN, reason="requires migrations 021-022 and QA PG15")
def test_same_backend_a_then_no_context_then_b_and_rollback(runtime_fixture):
    import psycopg2
    from psycopg2 import sql

    fixture = runtime_fixture
    user_id, user_public_id = fixture.users["ab"]
    tenant_ids = list(fixture.tenants.values())
    with _runtime_connector(fixture.runtime_url, 1) as connector:
        with connector.tenant_transaction(
            authenticated_user_id=user_id,
            requested_tenant_id=str(fixture.tenants["a"]),
        ) as transaction:
            assert transaction.principal.user_public_id == user_public_id
            assert transaction.principal.membership_id == fixture.memberships["ab_a"]
            rows = transaction.execute_query(
                """
                SELECT pg_backend_pid(), current_setting('app.tenant_id', true), id
                FROM tenants WHERE id = ANY(%s::uuid[])
                """,
                ([str(value) for value in tenant_ids],),
            )
            assert len(rows) == 1
            backend_a, setting_a, visible_a = rows[0]
            assert setting_a == str(fixture.tenants["a"])
            assert uuid.UUID(str(visible_a)) == fixture.tenants["a"]
            assert transaction.execute_write(
                sql.SQL("UPDATE public.{} SET value=%s WHERE tenant_id=%s").format(
                    sql.Identifier(fixture.probe_table)
                ),
                ("committed-a", str(fixture.tenants["a"])),
            ) == 1

        backend_none, setting_none, visible_none = _raw_pool_state(
            connector, tenant_ids
        )
        assert backend_none == backend_a
        assert setting_none in (None, "")
        assert visible_none == []

        with connector.tenant_transaction(
            authenticated_user_id=user_id,
            requested_tenant_id=fixture.tenants["b"],
        ) as transaction:
            rows = transaction.execute_query(
                """
                SELECT pg_backend_pid(), current_setting('app.tenant_id', true), id
                FROM tenants WHERE id = ANY(%s::uuid[])
                """,
                ([str(value) for value in tenant_ids],),
            )
            assert len(rows) == 1
            backend_b, setting_b, visible_b = rows[0]
            assert backend_b == backend_a
            assert setting_b == str(fixture.tenants["b"])
            assert uuid.UUID(str(visible_b)) == fixture.tenants["b"]

        with pytest.raises(RuntimeError, match="force rollback"):
            with connector.tenant_transaction(
                authenticated_user_id=user_id,
                requested_tenant_id=fixture.tenants["a"],
            ) as transaction:
                transaction.execute_write(
                    sql.SQL(
                        "UPDATE public.{} SET value=%s WHERE tenant_id=%s"
                    ).format(sql.Identifier(fixture.probe_table)),
                    ("must-roll-back", str(fixture.tenants["a"])),
                )
                raise RuntimeError("force rollback")

        admin = psycopg2.connect(fixture.admin_url)
        try:
            with admin.cursor() as cursor:
                cursor.execute(
                    sql.SQL("SELECT value FROM public.{} WHERE id=%s").format(
                        sql.Identifier(fixture.probe_table)
                    ),
                    (str(fixture.probe_rows["a"]),),
                )
                assert cursor.fetchone()[0] == "committed-a"
        finally:
            admin.rollback()
            admin.close()
        backend_after, setting_after, visible_after = _raw_pool_state(
            connector, tenant_ids
        )
        assert backend_after == backend_a
        assert setting_after in (None, "")
        assert visible_after == []


@pytest.mark.skipif(not RUN, reason="requires migrations 021-022 and QA PG15")
def test_membership_revocation_finishes_inflight_but_denies_next_transaction(
    runtime_fixture,
):
    import psycopg2
    from psycopg2 import sql

    from wxsearch.db_connector import TenantAccessDenied

    fixture = runtime_fixture
    user_id = fixture.users["a_only"][0]
    membership_id = fixture.memberships["a_only"]
    admin = psycopg2.connect(fixture.admin_url)
    try:
        with _runtime_connector(fixture.runtime_url, 1) as connector:
            with connector.tenant_transaction(
                authenticated_user_id=user_id,
                requested_tenant_id=fixture.tenants["a"],
            ) as transaction:
                with admin.cursor() as cursor:
                    cursor.execute(
                        "UPDATE tenant_memberships SET status='disabled' WHERE id=%s",
                        (str(membership_id),),
                    )
                admin.commit()
                rows = transaction.execute_query(
                    sql.SQL("SELECT value FROM public.{} WHERE id=%s").format(
                        sql.Identifier(fixture.probe_table)
                    ),
                    (str(fixture.probe_rows["a"]),),
                )
                assert len(rows) == 1

            with pytest.raises(TenantAccessDenied):
                with connector.tenant_transaction(
                    authenticated_user_id=user_id,
                    requested_tenant_id=fixture.tenants["a"],
                ):
                    pytest.fail("revoked membership must fail on the next transaction")
    finally:
        admin.rollback()
        with admin.cursor() as cursor:
            cursor.execute(
                "UPDATE tenant_memberships SET status='active' WHERE id=%s",
                (str(membership_id),),
            )
        admin.commit()
        admin.close()


@pytest.mark.skipif(not RUN, reason="requires migrations 021-022 and QA PG15")
def test_concurrent_tenant_transactions_keep_connections_and_contexts_isolated(
    runtime_fixture,
):
    fixture = runtime_fixture
    user_id = fixture.users["ab"][0]
    tenant_ids = list(fixture.tenants.values())
    barrier = threading.Barrier(2, timeout=10)
    results = {}
    failures = []
    result_lock = threading.Lock()

    with _runtime_connector(fixture.runtime_url, 2) as connector:

        def worker(key):
            try:
                tenant_id = fixture.tenants[key]
                with connector.tenant_transaction(
                    authenticated_user_id=user_id,
                    requested_tenant_id=tenant_id,
                ) as transaction:
                    barrier.wait()
                    rows = transaction.execute_query(
                        """
                        SELECT pg_backend_pid(),
                               current_setting('app.tenant_id', true), id
                        FROM tenants
                        WHERE id = ANY(%s::uuid[])
                        """,
                        ([str(value) for value in tenant_ids],),
                    )
                    barrier.wait()
                    with result_lock:
                        results[key] = rows
            except BaseException as exc:  # noqa: BLE001
                with result_lock:
                    failures.append(exc)

        threads = [
            threading.Thread(target=worker, args=(key,), daemon=True)
            for key in ("a", "b")
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=15)

        assert all(not thread.is_alive() for thread in threads)
        assert failures == []
        assert set(results) == {"a", "b"}
        for key in ("a", "b"):
            assert len(results[key]) == 1
            _pid, setting, visible_id = results[key][0]
            assert setting == str(fixture.tenants[key])
            assert uuid.UUID(str(visible_id)) == fixture.tenants[key]
        assert results["a"][0][0] != results["b"][0][0]
