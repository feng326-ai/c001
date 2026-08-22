from __future__ import annotations

import os
import uuid
from urllib.parse import urlsplit

import pytest

from wxsearch.runtime_db_role import (
    _runtime_connection_parameters,
    check_runtime_role,
    load_settings,
    provision_runtime_role,
)


RUN = os.getenv("RUN_MIGRATION_INTEGRATION") == "1"


def _guarded_qa_database_url() -> str:
    if (
        os.getenv("ENVIRONMENT") != "qa"
        or os.getenv("ALLOW_DESTRUCTIVE_TESTS") != "1"
    ):
        raise RuntimeError("runtime role test requires guarded QA environment")
    database_url = os.environ["DATABASE_URL"]
    database_name = urlsplit(database_url).path.lstrip("/")
    if "_lease_qa_" not in database_name:
        raise RuntimeError(
            f"runtime role test refused unsafe database: {database_name}"
        )
    return database_url


@pytest.mark.skipif(not RUN, reason="requires disposable QA PostgreSQL 15")
def test_runtime_login_is_idempotent_non_owner_and_least_privilege(monkeypatch):
    import psycopg2
    from psycopg2 import errors, sql

    database_url = _guarded_qa_database_url()
    suffix = uuid.uuid4().hex[:16]
    role_name = f"qa_runtime_{suffix}"
    role_password = f"qa-runtime-{uuid.uuid4().hex}"
    existing_table = f"qa_runtime_existing_{suffix}"
    future_table = f"qa_runtime_future_{suffix}"
    denied_table = f"qa_runtime_denied_{suffix}"
    admin = psycopg2.connect(database_url)
    runtime = None
    public_create_before = False
    default_function_public_execute_before = True

    monkeypatch.setenv("MIGRATION_DATABASE_URL", database_url)
    monkeypatch.setenv("APP_DATABASE_USER", role_name)
    monkeypatch.setenv("APP_DATABASE_PASSWORD", role_password)
    monkeypatch.delenv("MIGRATION_DATABASE_URL_FILE", raising=False)
    monkeypatch.delenv("APP_DATABASE_USER_FILE", raising=False)
    monkeypatch.delenv("APP_DATABASE_PASSWORD_FILE", raising=False)
    settings = load_settings()

    try:
        with admin.cursor() as cursor:
            cursor.execute("SHOW server_version_num")
            assert 150000 <= int(cursor.fetchone()[0]) < 160000
            cursor.execute(
                """
                SELECT EXISTS(
                    SELECT 1
                    FROM aclexplode(
                        COALESCE(n.nspacl, acldefault('n', n.nspowner))
                    ) acl
                    WHERE acl.grantee=0 AND acl.privilege_type='CREATE'
                )
                FROM pg_namespace n WHERE n.nspname='public'
                """
            )
            public_create_before = bool(cursor.fetchone()[0])
            cursor.execute(
                """
                SELECT EXISTS(
                    SELECT 1
                    FROM aclexplode(COALESCE(default_acl.defaclacl,
                        acldefault('f', owner_role.oid))) acl
                    WHERE acl.grantee=0 AND acl.privilege_type='EXECUTE'
                )
                FROM pg_roles owner_role
                LEFT JOIN pg_namespace namespace ON namespace.nspname='public'
                LEFT JOIN pg_default_acl default_acl
                  ON default_acl.defaclrole=owner_role.oid
                 AND default_acl.defaclnamespace=namespace.oid
                 AND default_acl.defaclobjtype='f'
                WHERE owner_role.rolname=current_user
                """
            )
            default_function_public_execute_before = bool(cursor.fetchone()[0])
            cursor.execute(
                sql.SQL(
                    "CREATE TABLE public.{} ("
                    "id BIGSERIAL PRIMARY KEY, value TEXT NOT NULL)"
                ).format(sql.Identifier(existing_table))
            )
        admin.commit()

        first = provision_runtime_role(settings)
        second = provision_runtime_role(settings)
        assert first["runtime_user"] == second["runtime_user"] == role_name
        assert first["verified"] is second["verified"] is True

        runtime = psycopg2.connect(**_runtime_connection_parameters(settings))
        with runtime.cursor() as cursor:
            cursor.execute(
                sql.SQL(
                    "INSERT INTO public.{}(value) VALUES('first') RETURNING id"
                ).format(sql.Identifier(existing_table))
            )
            row_id = cursor.fetchone()[0]
            cursor.execute(
                sql.SQL("UPDATE public.{} SET value='updated' WHERE id=%s").format(
                    sql.Identifier(existing_table)
                ),
                (row_id,),
            )
            assert cursor.rowcount == 1
            cursor.execute(
                sql.SQL("SELECT value FROM public.{} WHERE id=%s").format(
                    sql.Identifier(existing_table)
                ),
                (row_id,),
            )
            assert cursor.fetchone()[0] == "updated"
            cursor.execute(
                sql.SQL("DELETE FROM public.{} WHERE id=%s").format(
                    sql.Identifier(existing_table)
                ),
                (row_id,),
            )
            assert cursor.rowcount == 1
        runtime.commit()

        with runtime.cursor() as cursor:
            with pytest.raises(errors.InsufficientPrivilege):
                cursor.execute(
                    sql.SQL("CREATE TABLE public.{}(id INTEGER)").format(
                        sql.Identifier(denied_table)
                    )
                )
        runtime.rollback()
        with runtime.cursor() as cursor:
            cursor.execute("SELECT COUNT(*) FROM public.schema_migrations")
            assert cursor.fetchone()[0] >= 21
            cursor.execute(
                """
                SELECT has_table_privilege(
                           current_user, 'public.schema_migrations', 'SELECT'
                       ),
                       has_table_privilege(
                           current_user, 'public.schema_migrations', 'UPDATE'
                       )
                """
            )
            assert cursor.fetchone() == (True, False)
        runtime.rollback()

        with runtime.cursor() as cursor:
            cursor.execute(
                """
                SELECT relation_name,
                       has_table_privilege(
                           current_user, relation_name, 'SELECT'
                       ),
                       has_table_privilege(
                           current_user, relation_name, 'INSERT'
                       ),
                       has_table_privilege(
                           current_user, relation_name, 'UPDATE'
                       ),
                       has_table_privilege(
                           current_user, relation_name, 'DELETE'
                       ),
                       has_table_privilege(
                           current_user, relation_name, 'TRUNCATE'
                       ),
                       has_table_privilege(
                           current_user, relation_name, 'REFERENCES'
                       ),
                       has_table_privilege(
                           current_user, relation_name, 'TRIGGER'
                       )
                FROM unnest(ARRAY[
                    'public.tenants', 'public.tenant_memberships'
                ]) AS relation_name
                ORDER BY relation_name
                """
            )
            assert cursor.fetchall() == [
                (
                    "public.tenant_memberships",
                    True,
                    False,
                    False,
                    False,
                    False,
                    False,
                    False,
                ),
                (
                    "public.tenants",
                    True,
                    False,
                    False,
                    False,
                    False,
                    False,
                    False,
                ),
            ]

            for table_name in ("tenants", "tenant_memberships"):
                with pytest.raises(errors.InsufficientPrivilege):
                    cursor.execute(
                        sql.SQL(
                            "INSERT INTO public.{} SELECT * FROM public.{} "
                            "WHERE FALSE"
                        ).format(
                            sql.Identifier(table_name),
                            sql.Identifier(table_name),
                        )
                    )
                runtime.rollback()
                with pytest.raises(errors.InsufficientPrivilege):
                    cursor.execute(
                        sql.SQL(
                            "UPDATE public.{} SET id=id WHERE FALSE"
                        ).format(sql.Identifier(table_name))
                    )
                runtime.rollback()

        with admin.cursor() as cursor:
            cursor.execute(
                sql.SQL(
                    "CREATE TABLE public.{} ("
                    "id BIGSERIAL PRIMARY KEY, value TEXT NOT NULL)"
                ).format(sql.Identifier(future_table))
            )
        admin.commit()

        with runtime.cursor() as cursor:
            cursor.execute(
                sql.SQL(
                    "INSERT INTO public.{}(value) VALUES('default-acl') "
                    "RETURNING id"
                ).format(sql.Identifier(future_table))
            )
            assert cursor.fetchone()[0] >= 1
        runtime.rollback()
        with runtime.cursor() as cursor:
            with pytest.raises(errors.InsufficientPrivilege):
                cursor.execute(
                    sql.SQL("DELETE FROM public.{} WHERE FALSE").format(
                        sql.Identifier(future_table)
                    )
                )
        runtime.rollback()

        result = check_runtime_role(settings)
        assert result["status"] == "verified"
        assert result["database"] == urlsplit(database_url).path.lstrip("/")
        assert result["runtime_user"] == role_name
        assert result["relation_owner"] is False
        assert result["ddl_allowed"] is False
        assert result["ledger_write_allowed"] is False
        assert result["tenant_identity_write_allowed"] is False
    finally:
        if runtime is not None:
            runtime.rollback()
            runtime.close()
        admin.rollback()
        try:
            with admin.cursor() as cursor:
                cursor.execute(
                    sql.SQL("DROP TABLE IF EXISTS public.{} CASCADE").format(
                        sql.Identifier(denied_table)
                    )
                )
                cursor.execute(
                    sql.SQL("DROP TABLE IF EXISTS public.{} CASCADE").format(
                        sql.Identifier(future_table)
                    )
                )
                cursor.execute(
                    sql.SQL("DROP TABLE IF EXISTS public.{} CASCADE").format(
                        sql.Identifier(existing_table)
                    )
                )
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
                if public_create_before:
                    cursor.execute("GRANT CREATE ON SCHEMA public TO PUBLIC")
                else:
                    cursor.execute("REVOKE CREATE ON SCHEMA public FROM PUBLIC")
                if default_function_public_execute_before:
                    cursor.execute(
                        "ALTER DEFAULT PRIVILEGES IN SCHEMA public "
                        "GRANT EXECUTE ON FUNCTIONS TO PUBLIC"
                    )
                else:
                    cursor.execute(
                        "ALTER DEFAULT PRIVILEGES IN SCHEMA public "
                        "REVOKE EXECUTE ON FUNCTIONS FROM PUBLIC"
                    )
            admin.commit()
        finally:
            admin.close()
