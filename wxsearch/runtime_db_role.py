#!/usr/bin/env python3
"""Provision and verify the least-privilege PostgreSQL runtime login.

Cluster roles are environment-specific and therefore deliberately live outside
numbered schema migrations.  The management DSN is used only for provisioning;
all verification reconnects as the runtime login itself.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Mapping


ROLE_NAME = re.compile(r"^[a-z][a-z0-9_]{2,62}$")
RESERVED_ROLES = {
    "admin",
    "pg_database_owner",
    "postgres",
    "public",
    "qa_foundation",
    "staging_admin",
}
EXPECTED_DENIAL = "42501"


class RuntimeRoleError(RuntimeError):
    """Runtime database role configuration is unsafe or incomplete."""


@dataclass(frozen=True)
class RuntimeRoleSettings:
    migration_database_url: str = field(repr=False)
    app_database_user: str
    app_database_password: str = field(repr=False)


def _read_setting(name: str, environ: Mapping[str, str] | None = None) -> str:
    env = os.environ if environ is None else environ
    direct = env.get(name)
    file_name = env.get(f"{name}_FILE")
    if direct is not None and file_name is not None:
        raise RuntimeRoleError(
            f"configure exactly one of {name} or {name}_FILE"
        )
    if file_name is not None:
        path = Path(file_name)
        if not path.is_file():
            raise RuntimeRoleError(f"{name}_FILE is not a regular file")
        try:
            value = path.read_text(encoding="utf-8").rstrip("\r\n")
        except OSError as error:
            raise RuntimeRoleError(f"cannot read {name}_FILE") from error
    elif direct is not None:
        value = direct
    else:
        raise RuntimeRoleError(f"missing {name} or {name}_FILE")
    if not value or "\x00" in value or "\n" in value or "\r" in value:
        raise RuntimeRoleError(f"{name} must be one non-empty line")
    return value


def _validate_role_name(role_name: str) -> str:
    normalized = str(role_name or "").strip()
    if (
        not ROLE_NAME.fullmatch(normalized)
        or normalized.startswith("pg_")
        or normalized in RESERVED_ROLES
        or "runtime" not in normalized.split("_")
    ):
        raise RuntimeRoleError(
            "APP_DATABASE_USER must be a lowercase runtime role identifier"
        )
    return normalized


def load_settings(
    environ: Mapping[str, str] | None = None,
) -> RuntimeRoleSettings:
    migration_url = _read_setting("MIGRATION_DATABASE_URL", environ)
    runtime_user = _validate_role_name(
        _read_setting("APP_DATABASE_USER", environ)
    )
    runtime_password = _read_setting("APP_DATABASE_PASSWORD", environ)
    if len(runtime_password) < 16:
        raise RuntimeRoleError(
            "APP_DATABASE_PASSWORD must contain at least 16 characters"
        )
    return RuntimeRoleSettings(
        migration_database_url=migration_url,
        app_database_user=runtime_user,
        app_database_password=runtime_password,
    )


def _validate_settings(settings: RuntimeRoleSettings) -> RuntimeRoleSettings:
    role_name = _validate_role_name(settings.app_database_user)
    if not settings.migration_database_url:
        raise RuntimeRoleError("MIGRATION_DATABASE_URL must not be empty")
    if (
        len(settings.app_database_password) < 16
        or "\x00" in settings.app_database_password
        or "\n" in settings.app_database_password
        or "\r" in settings.app_database_password
    ):
        raise RuntimeRoleError("APP_DATABASE_PASSWORD is invalid")
    if role_name != settings.app_database_user:
        raise RuntimeRoleError("APP_DATABASE_USER must already be normalized")
    return settings


def _driver():
    try:
        import psycopg2
        from psycopg2 import sql
    except ImportError as error:  # pragma: no cover - deployment dependency
        raise RuntimeRoleError("psycopg2 is required") from error
    return psycopg2, sql


def _require_postgresql_15(cursor) -> None:
    cursor.execute("SHOW server_version_num")
    version_num = int(cursor.fetchone()[0])
    if not 150000 <= version_num < 160000:
        raise RuntimeRoleError("runtime role provisioning requires PostgreSQL 15")


def _runtime_connection_parameters(
    settings: RuntimeRoleSettings,
) -> dict[str, str]:
    psycopg2, _sql = _driver()
    try:
        parameters = psycopg2.extensions.parse_dsn(
            settings.migration_database_url
        )
    except Exception as error:
        raise RuntimeRoleError("invalid MIGRATION_DATABASE_URL") from error
    parameters["user"] = settings.app_database_user
    parameters["password"] = settings.app_database_password
    return parameters


def _assert_role_has_no_memberships(cursor, role_name: str) -> None:
    cursor.execute(
        """
        SELECT parent.rolname
        FROM pg_auth_members membership
        JOIN pg_roles member ON member.oid = membership.member
        JOIN pg_roles parent ON parent.oid = membership.roleid
        WHERE member.rolname = %s
        ORDER BY parent.rolname
        """,
        (role_name,),
    )
    memberships = [row[0] for row in cursor.fetchall()]
    if memberships:
        raise RuntimeRoleError(
            f"runtime role must not inherit or SET ROLE: {', '.join(memberships)}"
        )


def _owned_application_relations(cursor, role_name: str) -> list[str]:
    cursor.execute(
        """
        SELECT format('%%I.%%I', namespace.nspname, relation.relname)
        FROM pg_class relation
        JOIN pg_namespace namespace ON namespace.oid = relation.relnamespace
        JOIN pg_roles owner_role ON owner_role.oid = relation.relowner
        WHERE owner_role.rolname = %s
          AND relation.relkind IN ('r', 'p', 'v', 'm', 'f', 'S')
          AND namespace.nspname <> 'information_schema'
          AND namespace.nspname NOT LIKE 'pg_%%'
        ORDER BY 1
        """,
        (role_name,),
    )
    return [row[0] for row in cursor.fetchall()]


def _assert_not_database_or_schema_owner(cursor, role_name: str) -> None:
    cursor.execute(
        """
        SELECT pg_get_userbyid(database.datdba),
               pg_get_userbyid(namespace.nspowner)
        FROM pg_database database
        JOIN pg_namespace namespace ON namespace.nspname='public'
        WHERE database.datname=current_database()
        """
    )
    database_owner, schema_owner = cursor.fetchone()
    if role_name in {database_owner, schema_owner}:
        raise RuntimeRoleError(
            "runtime role must not own the database or public schema"
        )


def _tenant_function_state(cursor, runtime_role: str) -> dict[str, object] | None:
    cursor.execute(
        """
        SELECT procedure.prosecdef,
               COALESCE(procedure.proconfig, ARRAY[]::TEXT[]),
               owner.rolname,
               (owner.rolsuper OR owner.rolbypassrls),
               NOT EXISTS (
                   SELECT 1
                   FROM aclexplode(
                       COALESCE(
                           procedure.proacl,
                           acldefault('f', procedure.proowner)
                       )
                   ) acl
                   WHERE acl.grantee=0 AND acl.privilege_type='EXECUTE'
               )
        FROM pg_proc procedure
        JOIN pg_roles owner ON owner.oid=procedure.proowner
        WHERE procedure.oid=to_regprocedure(
            'public.app_list_active_tenants(uuid)'
        )
        """
    )
    row = cursor.fetchone()
    if row is None:
        return None
    (
        security_definer,
        configuration,
        owner_name,
        owner_can_bypass_rls,
        public_execute_revoked,
    ) = row
    search_path_settings = [
        value for value in configuration if value.startswith("search_path=")
    ]
    if (
        not security_definer
        or search_path_settings != ["search_path=pg_catalog"]
        or not owner_can_bypass_rls
        or owner_name == runtime_role
        or not public_execute_revoked
    ):
        raise RuntimeRoleError(
            "app_list_active_tenants(uuid) has an unsafe security definition"
        )
    return {
        "owner": owner_name,
        "security_definer": True,
        "search_path": "pg_catalog",
    }


def provision_runtime_role(
    settings: RuntimeRoleSettings | None = None,
) -> dict[str, object]:
    settings = _validate_settings(settings or load_settings())
    psycopg2, sql = _driver()
    connection = psycopg2.connect(settings.migration_database_url)
    try:
        with connection:
            with connection.cursor() as cursor:
                _require_postgresql_15(cursor)
                cursor.execute("SELECT current_user, current_database()")
                migration_owner, database_name = cursor.fetchone()
                if settings.app_database_user == migration_owner:
                    raise RuntimeRoleError(
                        "runtime login must differ from the migration role"
                    )
                _assert_not_database_or_schema_owner(
                    cursor, settings.app_database_user
                )
                cursor.execute(
                    "SELECT EXISTS(SELECT 1 FROM pg_roles WHERE rolname=%s)",
                    (settings.app_database_user,),
                )
                role_exists = bool(cursor.fetchone()[0])
                if role_exists:
                    owned = _owned_application_relations(
                        cursor, settings.app_database_user
                    )
                    if owned:
                        raise RuntimeRoleError(
                            "runtime role owns application relations: "
                            + ", ".join(owned)
                        )
                    _assert_role_has_no_memberships(
                        cursor, settings.app_database_user
                    )
                    cursor.execute(
                        sql.SQL(
                            "ALTER ROLE {} WITH LOGIN NOSUPERUSER NOCREATEDB "
                            "NOCREATEROLE NOINHERIT NOREPLICATION NOBYPASSRLS "
                            "PASSWORD %s"
                        ).format(sql.Identifier(settings.app_database_user)),
                        (settings.app_database_password,),
                    )
                else:
                    cursor.execute(
                        sql.SQL(
                            "CREATE ROLE {} WITH LOGIN NOSUPERUSER NOCREATEDB "
                            "NOCREATEROLE NOINHERIT NOREPLICATION NOBYPASSRLS "
                            "PASSWORD %s"
                        ).format(sql.Identifier(settings.app_database_user)),
                        (settings.app_database_password,),
                    )

                cursor.execute(
                    "SELECT to_regclass('public.schema_migrations') IS NOT NULL"
                )
                if not cursor.fetchone()[0]:
                    raise RuntimeRoleError(
                        "public.schema_migrations must exist before provisioning"
                    )

                runtime = sql.Identifier(settings.app_database_user)
                database = sql.Identifier(database_name)
                owner = sql.Identifier(migration_owner)
                public_schema = sql.Identifier("public")

                cursor.execute("REVOKE CREATE ON SCHEMA public FROM PUBLIC")
                cursor.execute(
                    sql.SQL("REVOKE ALL PRIVILEGES ON DATABASE {} FROM {}").format(
                        database, runtime
                    )
                )
                cursor.execute(
                    sql.SQL("GRANT CONNECT ON DATABASE {} TO {}").format(
                        database, runtime
                    )
                )
                cursor.execute(
                    sql.SQL("REVOKE ALL PRIVILEGES ON SCHEMA {} FROM {}").format(
                        public_schema, runtime
                    )
                )
                cursor.execute(
                    sql.SQL("GRANT USAGE ON SCHEMA {} TO {}").format(
                        public_schema, runtime
                    )
                )
                cursor.execute(
                    sql.SQL(
                        "REVOKE ALL PRIVILEGES ON ALL TABLES IN SCHEMA public "
                        "FROM {}"
                    ).format(runtime)
                )
                cursor.execute(
                    sql.SQL(
                        "GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES "
                        "IN SCHEMA public TO {}"
                    ).format(runtime)
                )
                cursor.execute(
                    sql.SQL(
                        "REVOKE INSERT, UPDATE, DELETE, TRUNCATE, REFERENCES, "
                        "TRIGGER ON TABLE public.tenants, "
                        "public.tenant_memberships FROM {}"
                    ).format(runtime)
                )
                cursor.execute(
                    sql.SQL(
                        "GRANT SELECT ON TABLE public.tenants, "
                        "public.tenant_memberships TO {}"
                    ).format(runtime)
                )
                cursor.execute(
                    sql.SQL(
                        "REVOKE INSERT, UPDATE, DELETE, TRUNCATE, REFERENCES, "
                        "TRIGGER ON TABLE public.schema_migrations FROM {}"
                    ).format(runtime)
                )
                cursor.execute(
                    sql.SQL(
                        "GRANT SELECT ON TABLE public.schema_migrations TO {}"
                    ).format(runtime)
                )
                cursor.execute(
                    sql.SQL(
                        "REVOKE ALL PRIVILEGES ON ALL SEQUENCES IN SCHEMA public "
                        "FROM {}"
                    ).format(runtime)
                )
                cursor.execute(
                    sql.SQL(
                        "GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public "
                        "TO {}"
                    ).format(runtime)
                )
                cursor.execute(
                    sql.SQL(
                        "REVOKE EXECUTE ON ALL FUNCTIONS IN SCHEMA public FROM {}"
                    ).format(runtime)
                )

                # Compatibility default for future application tables is limited
                # to public. Identity/control-plane tables require an explicit
                # narrower override (or a schema unavailable to runtime); they
                # must never inherit this write grant silently.
                cursor.execute(
                    sql.SQL(
                        "ALTER DEFAULT PRIVILEGES FOR ROLE {} IN SCHEMA public "
                        "REVOKE ALL PRIVILEGES ON TABLES FROM {}"
                    ).format(owner, runtime)
                )
                cursor.execute(
                    sql.SQL(
                        "ALTER DEFAULT PRIVILEGES FOR ROLE {} IN SCHEMA public "
                        "GRANT SELECT, INSERT, UPDATE ON TABLES TO {}"
                    ).format(owner, runtime)
                )
                cursor.execute(
                    sql.SQL(
                        "ALTER DEFAULT PRIVILEGES FOR ROLE {} IN SCHEMA public "
                        "REVOKE ALL PRIVILEGES ON SEQUENCES FROM {}"
                    ).format(owner, runtime)
                )
                cursor.execute(
                    sql.SQL(
                        "ALTER DEFAULT PRIVILEGES FOR ROLE {} IN SCHEMA public "
                        "GRANT USAGE, SELECT ON SEQUENCES TO {}"
                    ).format(owner, runtime)
                )
                cursor.execute(
                    sql.SQL(
                        "ALTER DEFAULT PRIVILEGES FOR ROLE {} IN SCHEMA public "
                        "REVOKE EXECUTE ON FUNCTIONS FROM PUBLIC"
                    ).format(owner)
                )

                cursor.execute(
                    "SELECT to_regprocedure("
                    "'public.app_list_active_tenants(uuid)') IS NOT NULL"
                )
                function_granted = bool(cursor.fetchone()[0])
                if function_granted:
                    cursor.execute(
                        "REVOKE EXECUTE ON FUNCTION "
                        "public.app_list_active_tenants(uuid) FROM PUBLIC"
                    )
                    _tenant_function_state(
                        cursor, settings.app_database_user
                    )
                    cursor.execute(
                        sql.SQL(
                            "GRANT EXECUTE ON FUNCTION "
                            "public.app_list_active_tenants(uuid) TO {}"
                        ).format(runtime)
                    )
        checked = check_runtime_role(settings)
        return {
            "status": "provisioned",
            "database": checked["database"],
            "migration_owner": migration_owner,
            "runtime_user": settings.app_database_user,
            "function_granted": function_granted,
            "verified": True,
        }
    finally:
        connection.close()


def _expect_permission_denied(cursor, statement) -> None:
    cursor.execute("SAVEPOINT runtime_permission_probe")
    try:
        cursor.execute(statement)
    except Exception as error:
        cursor.execute("ROLLBACK TO SAVEPOINT runtime_permission_probe")
        if getattr(error, "pgcode", None) != EXPECTED_DENIAL:
            raise
    else:
        cursor.execute("ROLLBACK TO SAVEPOINT runtime_permission_probe")
        raise RuntimeRoleError("runtime role unexpectedly passed a denied probe")
    finally:
        cursor.execute("RELEASE SAVEPOINT runtime_permission_probe")


def check_runtime_role(
    settings: RuntimeRoleSettings | None = None,
) -> dict[str, object]:
    settings = _validate_settings(settings or load_settings())
    psycopg2, sql = _driver()
    parameters = _runtime_connection_parameters(settings)
    connection = psycopg2.connect(**parameters)
    try:
        with connection.cursor() as cursor:
            _require_postgresql_15(cursor)
            cursor.execute("SELECT current_user, session_user, current_database()")
            current_user, session_user, database_name = cursor.fetchone()
            if current_user != settings.app_database_user or session_user != current_user:
                raise RuntimeRoleError("runtime connection identity mismatch")

            cursor.execute(
                """
                SELECT rolcanlogin, rolsuper, rolcreatedb, rolcreaterole,
                       rolinherit, rolreplication, rolbypassrls
                FROM pg_roles WHERE rolname=current_user
                """
            )
            flags = cursor.fetchone()
            if flags != (True, False, False, False, False, False, False):
                raise RuntimeRoleError("runtime role attributes are unsafe")
            _assert_role_has_no_memberships(cursor, current_user)
            owned = _owned_application_relations(cursor, current_user)
            if owned:
                raise RuntimeRoleError(
                    "runtime role owns application relations: " + ", ".join(owned)
                )
            _assert_not_database_or_schema_owner(cursor, current_user)

            cursor.execute(
                """
                SELECT has_database_privilege(current_user, current_database(), 'CONNECT'),
                       has_database_privilege(current_user, current_database(), 'CREATE'),
                       has_schema_privilege(current_user, 'public', 'USAGE'),
                       has_schema_privilege(current_user, 'public', 'CREATE')
                """
            )
            (
                can_connect,
                can_create_database_objects,
                can_use_schema,
                can_create_schema_objects,
            ) = cursor.fetchone()
            if (
                not can_connect
                or can_create_database_objects
                or not can_use_schema
                or can_create_schema_objects
            ):
                raise RuntimeRoleError("runtime database/schema privileges are unsafe")

            cursor.execute(
                """
                SELECT has_table_privilege(current_user, 'public.schema_migrations', 'SELECT'),
                       has_table_privilege(current_user, 'public.schema_migrations', 'INSERT'),
                       has_table_privilege(current_user, 'public.schema_migrations', 'UPDATE'),
                       has_table_privilege(current_user, 'public.schema_migrations', 'DELETE'),
                       has_table_privilege(current_user, 'public.schema_migrations', 'TRUNCATE')
                """
            )
            ledger_acl = cursor.fetchone()
            if ledger_acl != (True, False, False, False, False):
                raise RuntimeRoleError("schema_migrations privileges are unsafe")
            cursor.execute("SELECT version FROM public.schema_migrations LIMIT 1")

            cursor.execute(
                """
                SELECT COALESCE(bool_and(
                    has_table_privilege(
                        current_user, relation.oid, 'SELECT'
                    )
                    AND has_table_privilege(current_user, relation.oid, 'INSERT')
                    AND has_table_privilege(current_user, relation.oid, 'UPDATE')
                ), TRUE)
                FROM pg_class relation
                JOIN pg_namespace namespace ON namespace.oid=relation.relnamespace
                WHERE namespace.nspname='public'
                  AND relation.relkind IN ('r', 'p', 'v', 'm', 'f')
                  AND relation.relname NOT IN (
                      'schema_migrations', 'tenants', 'tenant_memberships'
                  )
                """
            )
            if not cursor.fetchone()[0]:
                raise RuntimeRoleError("runtime table DML privileges are incomplete")

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
            identity_acl = cursor.fetchall()
            if identity_acl != [
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
            ]:
                raise RuntimeRoleError(
                    "tenant identity tables must be runtime SELECT-only"
                )

            for table_name in ("tenants", "tenant_memberships"):
                _expect_permission_denied(
                    cursor,
                    f"INSERT INTO public.{table_name} SELECT * "
                    f"FROM public.{table_name} WHERE FALSE",
                )
                _expect_permission_denied(
                    cursor,
                    f"UPDATE public.{table_name} SET id=id WHERE FALSE",
                )

            cursor.execute(
                """
                SELECT COALESCE(bool_and(
                    has_sequence_privilege(current_user, relation.oid, 'USAGE')
                    AND has_sequence_privilege(current_user, relation.oid, 'SELECT')
                ), TRUE)
                FROM pg_class relation
                JOIN pg_namespace namespace ON namespace.oid=relation.relnamespace
                WHERE namespace.nspname='public' AND relation.relkind='S'
                """
            )
            if not cursor.fetchone()[0]:
                raise RuntimeRoleError("runtime sequence privileges are incomplete")

            tenant_function = _tenant_function_state(cursor, current_user)
            if tenant_function is not None:
                cursor.execute(
                    """
                    SELECT has_function_privilege(
                        current_user,
                        'public.app_list_active_tenants(uuid)',
                        'EXECUTE'
                    )
                    """
                )
                if not cursor.fetchone()[0]:
                    raise RuntimeRoleError(
                        "runtime tenant discovery function grant is missing"
                    )

            ddl_probe = sql.SQL("CREATE TABLE public.{}(id integer)").format(
                sql.Identifier(f"runtime_ddl_probe_{uuid.uuid4().hex}")
            )
            _expect_permission_denied(cursor, ddl_probe)
            _expect_permission_denied(
                cursor,
                "UPDATE public.schema_migrations "
                "SET description=description WHERE FALSE",
            )
        connection.rollback()
        return {
            "status": "verified",
            "database": database_name,
            "runtime_user": current_user,
            "function_available": tenant_function is not None,
            "relation_owner": False,
            "ddl_allowed": False,
            "ledger_write_allowed": False,
            "tenant_identity_write_allowed": False,
        }
    finally:
        connection.rollback()
        connection.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("provision", "check"))
    args = parser.parse_args(argv)
    try:
        settings = load_settings()
        if args.action == "provision":
            result = provision_runtime_role(settings)
        else:
            result = check_runtime_role(settings)
    except RuntimeRoleError as error:
        print(f"runtime role error: {error}", file=sys.stderr)
        return 2
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
