#!/usr/bin/env python3
"""Provision and verify the isolated review-distributor PostgreSQL login.

The distributor is a control-plane worker.  It never receives table access;
its only privileged database capability is executing four audited SECURITY
DEFINER functions.  Environment-specific roles remain outside migrations.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path

ROLE_NAME = re.compile(r"^[a-z][a-z0-9_]{2,62}$")
RESERVED_ROLES = {
    "admin",
    "pg_database_owner",
    "postgres",
    "public",
    "qa_foundation",
    "staging_admin",
}
FUNCTION_SIGNATURES = (
    "public.app_expand_review_distribution(uuid)",
    "public.app_claim_review_distribution_target(text,integer)",
    "public.app_apply_review_distribution_target(uuid,uuid)",
    "public.app_report_review_distribution_failure(uuid,uuid,text)",
)


class DistributorRoleError(RuntimeError):
    """The distributor database role is absent or unsafe."""


@dataclass(frozen=True)
class DistributorRoleSettings:
    migration_database_url: str = field(repr=False)
    distributor_database_user: str
    distributor_database_password: str = field(repr=False)


def _read_setting(name: str, environ: Mapping[str, str] | None = None) -> str:
    env = os.environ if environ is None else environ
    direct = env.get(name)
    file_name = env.get(f"{name}_FILE")
    if direct is not None and file_name is not None:
        raise DistributorRoleError(f"configure exactly one of {name} or {name}_FILE")
    if file_name is not None:
        path = Path(file_name)
        if not path.is_file():
            raise DistributorRoleError(f"{name}_FILE is not a regular file")
        try:
            value = path.read_text(encoding="utf-8").rstrip("\r\n")
        except OSError as error:
            raise DistributorRoleError(f"cannot read {name}_FILE") from error
    elif direct is not None:
        value = direct
    else:
        raise DistributorRoleError(f"missing {name} or {name}_FILE")
    if not value or "\x00" in value or "\n" in value or "\r" in value:
        raise DistributorRoleError(f"{name} must be one non-empty line")
    return value


def _validate_role_name(role_name: str) -> str:
    normalized = str(role_name or "").strip()
    role_tokens = normalized.split("_")
    if (
        not ROLE_NAME.fullmatch(normalized)
        or normalized.startswith(("pg_", "replace_"))
        or normalized in RESERVED_ROLES
        or "distributor" not in role_tokens
        or "runtime" in role_tokens
    ):
        raise DistributorRoleError(
            "DISTRIBUTOR_DATABASE_USER must be a lowercase distributor role identifier"
        )
    return normalized


def load_settings(
    environ: Mapping[str, str] | None = None,
) -> DistributorRoleSettings:
    migration_url = _read_setting("MIGRATION_DATABASE_URL", environ)
    distributor_user = _validate_role_name(
        _read_setting("DISTRIBUTOR_DATABASE_USER", environ)
    )
    distributor_password = _read_setting("DISTRIBUTOR_DATABASE_PASSWORD", environ)
    if len(distributor_password) < 16:
        raise DistributorRoleError(
            "DISTRIBUTOR_DATABASE_PASSWORD must contain at least 16 characters"
        )
    return DistributorRoleSettings(
        migration_database_url=migration_url,
        distributor_database_user=distributor_user,
        distributor_database_password=distributor_password,
    )


def _validate_settings(
    settings: DistributorRoleSettings,
) -> DistributorRoleSettings:
    role_name = _validate_role_name(settings.distributor_database_user)
    if (
        not settings.migration_database_url
        or "\x00" in settings.migration_database_url
        or "\n" in settings.migration_database_url
        or "\r" in settings.migration_database_url
    ):
        raise DistributorRoleError("MIGRATION_DATABASE_URL is invalid")
    password = settings.distributor_database_password
    if (
        len(password) < 16
        or "\x00" in password
        or "\n" in password
        or "\r" in password
        or "replace" in password.lower()
        or "placeholder" in password.lower()
        or "change-me" in password.lower()
    ):
        raise DistributorRoleError("DISTRIBUTOR_DATABASE_PASSWORD is invalid")
    if role_name != settings.distributor_database_user:
        raise DistributorRoleError(
            "DISTRIBUTOR_DATABASE_USER must already be normalized"
        )
    return settings


def _driver():
    try:
        import psycopg2
        from psycopg2 import sql
    except ImportError as error:  # pragma: no cover - deployment dependency
        raise DistributorRoleError("psycopg2 is required") from error
    return psycopg2, sql


def _require_postgresql_15(cursor) -> None:
    cursor.execute("SHOW server_version_num")
    version_num = int(cursor.fetchone()[0])
    if not 150000 <= version_num < 160000:
        raise DistributorRoleError(
            "distributor role provisioning requires PostgreSQL 15"
        )


def _distributor_connection_parameters(
    settings: DistributorRoleSettings,
) -> dict[str, str]:
    psycopg2, _sql = _driver()
    try:
        parameters = psycopg2.extensions.parse_dsn(settings.migration_database_url)
    except Exception as error:
        raise DistributorRoleError("invalid MIGRATION_DATABASE_URL") from error
    parameters["user"] = settings.distributor_database_user
    parameters["password"] = settings.distributor_database_password
    return parameters


def _application_schemas(cursor) -> list[str]:
    cursor.execute(
        """
        SELECT nspname
        FROM pg_namespace
        WHERE nspname <> 'information_schema'
          AND nspname NOT LIKE 'pg_%%'
        ORDER BY nspname
        """
    )
    return [row[0] for row in cursor.fetchall()]


def _assert_no_role_memberships(cursor, role_name: str) -> None:
    cursor.execute(
        """
        SELECT parent.rolname, member.rolname
        FROM pg_auth_members membership
        JOIN pg_roles member ON member.oid = membership.member
        JOIN pg_roles parent ON parent.oid = membership.roleid
        WHERE member.rolname = %s OR parent.rolname = %s
        ORDER BY parent.rolname, member.rolname
        """,
        (role_name, role_name),
    )
    if cursor.fetchall():
        raise DistributorRoleError(
            "distributor role must not participate in role memberships"
        )


def _owned_application_objects(cursor, role_name: str) -> list[str]:
    cursor.execute(
        """
        SELECT object_name
        FROM (
            SELECT format('relation:%%I.%%I', namespace.nspname, relation.relname)
                       AS object_name
            FROM pg_class relation
            JOIN pg_namespace namespace ON namespace.oid=relation.relnamespace
            WHERE relation.relowner=(SELECT oid FROM pg_roles WHERE rolname=%s)
              AND relation.relkind IN ('r', 'p', 'v', 'm', 'f', 'S')
              AND namespace.nspname <> 'information_schema'
              AND namespace.nspname NOT LIKE 'pg_%%'
            UNION ALL
            SELECT format('schema:%%I', namespace.nspname)
            FROM pg_namespace namespace
            WHERE namespace.nspowner=(SELECT oid FROM pg_roles WHERE rolname=%s)
              AND namespace.nspname <> 'information_schema'
              AND namespace.nspname NOT LIKE 'pg_%%'
            UNION ALL
            SELECT format('function:%%s', procedure.oid::regprocedure)
            FROM pg_proc procedure
            JOIN pg_namespace namespace ON namespace.oid=procedure.pronamespace
            WHERE procedure.proowner=(SELECT oid FROM pg_roles WHERE rolname=%s)
              AND namespace.nspname <> 'information_schema'
              AND namespace.nspname NOT LIKE 'pg_%%'
        ) objects
        ORDER BY object_name
        """,
        (role_name, role_name, role_name),
    )
    return [row[0] for row in cursor.fetchall()]


def _assert_not_database_owner(cursor, role_name: str) -> None:
    cursor.execute(
        """
        SELECT pg_get_userbyid(datdba)
        FROM pg_database
        WHERE datname=current_database()
        """
    )
    if cursor.fetchone()[0] == role_name:
        raise DistributorRoleError("distributor role must not own the database")


def _function_state(cursor, distributor_role: str, signature: str) -> int:
    cursor.execute(
        """
        SELECT procedure.oid,
               procedure.prosecdef,
               COALESCE(procedure.proconfig, ARRAY[]::TEXT[]),
               owner.rolname,
               (owner.rolsuper OR owner.rolbypassrls),
               NOT EXISTS (
                   SELECT 1
                   FROM aclexplode(COALESCE(
                       procedure.proacl,
                       acldefault('f', procedure.proowner)
                   )) acl
                   WHERE acl.grantee=0 AND acl.privilege_type='EXECUTE'
               )
        FROM pg_proc procedure
        JOIN pg_roles owner ON owner.oid=procedure.proowner
        WHERE procedure.oid=to_regprocedure(%s)
        """,
        (signature,),
    )
    row = cursor.fetchone()
    if row is None:
        raise DistributorRoleError(
            f"required distributor function is missing: {signature}"
        )
    (
        oid,
        security_definer,
        configuration,
        owner_name,
        owner_can_bypass_rls,
        public_execute_revoked,
    ) = row
    search_paths = [
        value for value in configuration if value.startswith("search_path=")
    ]
    if (
        not security_definer
        or search_paths != ["search_path=pg_catalog"]
        or not owner_can_bypass_rls
        or owner_name == distributor_role
        or not public_execute_revoked
    ):
        raise DistributorRoleError(
            f"required distributor function is unsafe: {signature}"
        )
    return int(oid)


def _required_function_oids(cursor, distributor_role: str) -> set[int]:
    return {
        _function_state(cursor, distributor_role, signature)
        for signature in FUNCTION_SIGNATURES
    }


def provision_distributor_role(
    settings: DistributorRoleSettings | None = None,
) -> dict[str, object]:
    settings = _validate_settings(settings or load_settings())
    psycopg2, sql = _driver()
    connection = psycopg2.connect(settings.migration_database_url)
    try:
        with connection, connection.cursor() as cursor:
            _require_postgresql_15(cursor)
            cursor.execute("SELECT current_user, current_database()")
            migration_owner, database_name = cursor.fetchone()
            role_name = settings.distributor_database_user
            if role_name == migration_owner:
                raise DistributorRoleError(
                    "distributor login must differ from the migration role"
                )

            # Function ACLs are part of the migration contract, but the
            # provisioner repeats the PUBLIC revoke to remain idempotent.
            for signature in FUNCTION_SIGNATURES:
                cursor.execute(
                    sql.SQL("REVOKE ALL ON FUNCTION {} FROM PUBLIC").format(
                        sql.SQL(signature)
                    )
                )
            required_oids = _required_function_oids(cursor, role_name)

            cursor.execute(
                "SELECT EXISTS(SELECT 1 FROM pg_roles WHERE rolname=%s)",
                (role_name,),
            )
            role_exists = bool(cursor.fetchone()[0])
            if role_exists:
                if _owned_application_objects(cursor, role_name):
                    raise DistributorRoleError(
                        "distributor role owns application objects"
                    )
                _assert_not_database_owner(cursor, role_name)
                _assert_no_role_memberships(cursor, role_name)
                cursor.execute(
                    sql.SQL(
                        "ALTER ROLE {} WITH LOGIN NOSUPERUSER NOCREATEDB "
                        "NOCREATEROLE NOINHERIT NOREPLICATION NOBYPASSRLS "
                        "PASSWORD %s"
                    ).format(sql.Identifier(role_name)),
                    (settings.distributor_database_password,),
                )
            else:
                cursor.execute(
                    sql.SQL(
                        "CREATE ROLE {} WITH LOGIN NOSUPERUSER NOCREATEDB "
                        "NOCREATEROLE NOINHERIT NOREPLICATION NOBYPASSRLS "
                        "PASSWORD %s"
                    ).format(sql.Identifier(role_name)),
                    (settings.distributor_database_password,),
                )

            role = sql.Identifier(role_name)
            database = sql.Identifier(database_name)
            cursor.execute("REVOKE CREATE ON SCHEMA public FROM PUBLIC")
            cursor.execute(
                sql.SQL("REVOKE ALL PRIVILEGES ON DATABASE {} FROM {}").format(
                    database, role
                )
            )
            cursor.execute(
                sql.SQL("GRANT CONNECT ON DATABASE {} TO {}").format(database, role)
            )

            for schema_name in _application_schemas(cursor):
                schema = sql.Identifier(schema_name)
                cursor.execute(
                    sql.SQL("REVOKE ALL PRIVILEGES ON SCHEMA {} FROM {}").format(
                        schema, role
                    )
                )
                cursor.execute(
                    sql.SQL(
                        "REVOKE ALL PRIVILEGES ON ALL TABLES IN SCHEMA {} FROM {}"
                    ).format(schema, role)
                )
                cursor.execute(
                    sql.SQL(
                        "REVOKE ALL PRIVILEGES ON ALL SEQUENCES IN SCHEMA {} FROM {}"
                    ).format(schema, role)
                )
                cursor.execute(
                    sql.SQL(
                        "REVOKE EXECUTE ON ALL FUNCTIONS IN SCHEMA {} FROM {}"
                    ).format(schema, role)
                )
            cursor.execute(sql.SQL("GRANT USAGE ON SCHEMA public TO {}").format(role))
            for signature in FUNCTION_SIGNATURES:
                cursor.execute(
                    sql.SQL("GRANT EXECUTE ON FUNCTION {} TO {}").format(
                        sql.SQL(signature), role
                    )
                )

            # Keep the local variable used: it also proves all four OIDs
            # were distinct before any role mutation is committed.
            if len(required_oids) != len(FUNCTION_SIGNATURES):
                raise DistributorRoleError(
                    "distributor function signatures are not distinct"
                )
        checked = check_distributor_role(settings)
        return {
            "status": "provisioned",
            "database": checked["database"],
            "migration_owner": migration_owner,
            "distributor_user": settings.distributor_database_user,
            "function_count": len(FUNCTION_SIGNATURES),
            "verified": True,
        }
    finally:
        connection.close()


def check_distributor_role(
    settings: DistributorRoleSettings | None = None,
) -> dict[str, object]:
    settings = _validate_settings(settings or load_settings())
    psycopg2, _sql = _driver()
    connection = psycopg2.connect(**_distributor_connection_parameters(settings))
    try:
        with connection.cursor() as cursor:
            _require_postgresql_15(cursor)
            cursor.execute("SELECT current_user, session_user, current_database()")
            current_user, session_user, database_name = cursor.fetchone()
            if (
                current_user != settings.distributor_database_user
                or session_user != current_user
            ):
                raise DistributorRoleError("distributor connection identity mismatch")

            cursor.execute(
                """
                SELECT rolcanlogin, rolsuper, rolcreatedb, rolcreaterole,
                       rolinherit, rolreplication, rolbypassrls
                FROM pg_roles WHERE rolname=current_user
                """
            )
            if cursor.fetchone() != (
                True,
                False,
                False,
                False,
                False,
                False,
                False,
            ):
                raise DistributorRoleError("distributor role attributes are unsafe")
            _assert_no_role_memberships(cursor, current_user)
            _assert_not_database_owner(cursor, current_user)
            if _owned_application_objects(cursor, current_user):
                raise DistributorRoleError("distributor role owns application objects")

            cursor.execute(
                """
                SELECT has_database_privilege(
                           current_user, current_database(), 'CONNECT'
                       ),
                       has_database_privilege(
                           current_user, current_database(), 'CREATE'
                       )
                """
            )
            if cursor.fetchone() != (True, False):
                raise DistributorRoleError("distributor database privileges are unsafe")

            for schema_name in _application_schemas(cursor):
                cursor.execute(
                    """
                    SELECT has_schema_privilege(current_user, %s, 'USAGE'),
                           has_schema_privilege(current_user, %s, 'CREATE')
                    """,
                    (schema_name, schema_name),
                )
                actual = cursor.fetchone()
                expected = (
                    (True, False)
                    if schema_name == "public"
                    else (
                        False,
                        False,
                    )
                )
                if actual != expected:
                    raise DistributorRoleError(
                        f"distributor schema privileges are unsafe: {schema_name}"
                    )

            cursor.execute(
                """
                SELECT COALESCE(bool_or(
                    has_table_privilege(current_user, relation.oid, 'SELECT')
                    OR has_table_privilege(current_user, relation.oid, 'INSERT')
                    OR has_table_privilege(current_user, relation.oid, 'UPDATE')
                    OR has_table_privilege(current_user, relation.oid, 'DELETE')
                    OR has_table_privilege(current_user, relation.oid, 'TRUNCATE')
                    OR has_table_privilege(current_user, relation.oid, 'REFERENCES')
                    OR has_table_privilege(current_user, relation.oid, 'TRIGGER')
                ), FALSE)
                FROM pg_class relation
                JOIN pg_namespace namespace ON namespace.oid=relation.relnamespace
                WHERE relation.relkind IN ('r', 'p', 'v', 'm', 'f')
                  AND namespace.nspname <> 'information_schema'
                  AND namespace.nspname NOT LIKE 'pg_%%'
                """
            )
            if cursor.fetchone()[0]:
                raise DistributorRoleError("distributor must not have table privileges")

            cursor.execute(
                """
                SELECT COALESCE(bool_or(
                    has_sequence_privilege(current_user, relation.oid, 'USAGE')
                    OR has_sequence_privilege(current_user, relation.oid, 'SELECT')
                    OR has_sequence_privilege(current_user, relation.oid, 'UPDATE')
                ), FALSE)
                FROM pg_class relation
                JOIN pg_namespace namespace ON namespace.oid=relation.relnamespace
                WHERE relation.relkind='S'
                  AND namespace.nspname <> 'information_schema'
                  AND namespace.nspname NOT LIKE 'pg_%%'
                """
            )
            if cursor.fetchone()[0]:
                raise DistributorRoleError(
                    "distributor must not have sequence privileges"
                )

            required_oids = _required_function_oids(cursor, current_user)
            for signature in FUNCTION_SIGNATURES:
                cursor.execute(
                    "SELECT has_function_privilege(current_user, %s, 'EXECUTE')",
                    (signature,),
                )
                if cursor.fetchone() != (True,):
                    raise DistributorRoleError(
                        f"distributor function grant is missing: {signature}"
                    )

            cursor.execute(
                """
                SELECT procedure.oid::regprocedure::TEXT
                FROM pg_proc procedure
                JOIN pg_namespace namespace ON namespace.oid=procedure.pronamespace
                WHERE procedure.prosecdef
                  AND namespace.nspname <> 'information_schema'
                  AND namespace.nspname NOT LIKE 'pg_%%'
                  AND NOT (procedure.oid = ANY(%s::oid[]))
                  AND has_function_privilege(
                      current_user, procedure.oid, 'EXECUTE'
                  )
                ORDER BY procedure.oid::regprocedure::TEXT
                """,
                (list(required_oids),),
            )
            unexpected_privileged_functions = [row[0] for row in cursor.fetchall()]
            if unexpected_privileged_functions:
                raise DistributorRoleError(
                    "distributor can execute unexpected privileged functions"
                )

            cursor.execute(
                """
                SELECT procedure.oid
                FROM pg_proc procedure
                JOIN pg_namespace namespace ON namespace.oid=procedure.pronamespace
                JOIN LATERAL aclexplode(COALESCE(
                    procedure.proacl,
                    acldefault('f', procedure.proowner)
                )) acl ON TRUE
                WHERE acl.grantee=(SELECT oid FROM pg_roles WHERE rolname=current_user)
                  AND acl.privilege_type='EXECUTE'
                  AND namespace.nspname <> 'information_schema'
                  AND namespace.nspname NOT LIKE 'pg_%%'
                """
            )
            directly_granted_oids = {int(row[0]) for row in cursor.fetchall()}
            if directly_granted_oids != required_oids:
                raise DistributorRoleError(
                    "distributor has unexpected direct function privileges"
                )

        connection.rollback()
        return {
            "status": "verified",
            "database": database_name,
            "distributor_user": current_user,
            "function_count": len(required_oids),
            "relation_owner": False,
            "table_access": False,
            "sequence_access": False,
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
            result = provision_distributor_role(settings)
        else:
            result = check_distributor_role(settings)
    except DistributorRoleError as error:
        print(f"distributor role error: {error}", file=sys.stderr)
        return 2
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
