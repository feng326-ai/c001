from pathlib import Path

import pytest

from wxsearch.runtime_db_role import (
    RuntimeRoleError,
    RuntimeRoleSettings,
    _runtime_connection_parameters,
    _validate_role_name,
    _validate_settings,
    load_settings,
    main,
)

MIGRATION_URL = "postgresql://migration-admin:qa-management-secret@db:5432/app"
RUNTIME_PASSWORD = "qa-runtime-password-0123456789"


@pytest.mark.parametrize(
    "role_name",
    ("xiansuo_runtime", "qa_runtime_0123abcd", "backend_runtime_login"),
)
def test_runtime_role_allowlist_accepts_scoped_lowercase_names(role_name: str):
    assert _validate_role_name(role_name) == role_name


@pytest.mark.parametrize(
    "role_name",
    (
        "admin",
        "postgres",
        "public",
        "pg_runtime",
        "Runtime",
        "runtime;drop_role",
        "application_user",
        "x" * 64,
    ),
)
def test_runtime_role_allowlist_rejects_reserved_or_ambiguous_names(
    role_name: str,
):
    with pytest.raises(RuntimeRoleError, match="runtime role identifier"):
        _validate_role_name(role_name)


def test_settings_support_secret_files_without_exposing_values(tmp_path: Path):
    migration_file = tmp_path / "migration-url"
    user_file = tmp_path / "runtime-user"
    password_file = tmp_path / "runtime-password"
    migration_file.write_text(f"{MIGRATION_URL}\n", encoding="utf-8")
    user_file.write_text("qa_runtime_file\n", encoding="utf-8")
    password_file.write_text(f"{RUNTIME_PASSWORD}\n", encoding="utf-8")

    settings = load_settings(
        {
            "MIGRATION_DATABASE_URL_FILE": str(migration_file),
            "APP_DATABASE_USER_FILE": str(user_file),
            "APP_DATABASE_PASSWORD_FILE": str(password_file),
        }
    )

    assert settings.app_database_user == "qa_runtime_file"
    assert settings.app_database_password == RUNTIME_PASSWORD
    assert settings.migration_database_url == MIGRATION_URL
    representation = repr(settings)
    assert RUNTIME_PASSWORD not in representation
    assert "management-secret" not in representation


def test_settings_reject_ambiguous_direct_and_file_values(tmp_path: Path):
    password_file = tmp_path / "runtime-password"
    password_file.write_text(RUNTIME_PASSWORD, encoding="utf-8")
    env = {
        "MIGRATION_DATABASE_URL": MIGRATION_URL,
        "APP_DATABASE_USER": "qa_runtime_conflict",
        "APP_DATABASE_PASSWORD": RUNTIME_PASSWORD,
        "APP_DATABASE_PASSWORD_FILE": str(password_file),
    }

    with pytest.raises(RuntimeRoleError, match="exactly one"):
        load_settings(env)


@pytest.mark.parametrize(
    "password",
    ("short", "sixteen-chars-ok\nextra", "sixteen-chars-ok\x00bad"),
)
def test_settings_reject_weak_or_multiline_passwords(password: str):
    env = {
        "MIGRATION_DATABASE_URL": MIGRATION_URL,
        "APP_DATABASE_USER": "qa_runtime_password",
        "APP_DATABASE_PASSWORD": password,
    }
    with pytest.raises(RuntimeRoleError, match="PASSWORD"):
        load_settings(env)


def test_constructed_settings_are_revalidated_before_database_use():
    unsafe = RuntimeRoleSettings(
        migration_database_url=MIGRATION_URL,
        app_database_user="admin",
        app_database_password=RUNTIME_PASSWORD,
    )
    with pytest.raises(RuntimeRoleError):
        _validate_settings(unsafe)


def test_runtime_connection_reuses_target_but_overrides_identity():
    settings = RuntimeRoleSettings(
        migration_database_url=(
            "postgresql://migration-admin:qa-management-secret@db:5433/app"
            "?sslmode=require&connect_timeout=7"
        ),
        app_database_user="qa_runtime_dsn",
        app_database_password=RUNTIME_PASSWORD,
    )

    parameters = _runtime_connection_parameters(settings)

    assert parameters["host"] == "db"
    assert parameters["port"] == "5433"
    assert parameters["dbname"] == "app"
    assert parameters["sslmode"] == "require"
    assert parameters["connect_timeout"] == "7"
    assert parameters["user"] == "qa_runtime_dsn"
    assert parameters["password"] == RUNTIME_PASSWORD


def test_cli_json_output_never_contains_connection_secrets(
    monkeypatch, capsys
):
    settings = RuntimeRoleSettings(
        migration_database_url=MIGRATION_URL,
        app_database_user="qa_runtime_output",
        app_database_password=RUNTIME_PASSWORD,
    )
    monkeypatch.setattr(
        "wxsearch.runtime_db_role.load_settings", lambda: settings
    )
    monkeypatch.setattr(
        "wxsearch.runtime_db_role.check_runtime_role",
        lambda _settings: {
            "status": "verified",
            "runtime_user": _settings.app_database_user,
        },
    )

    assert main(["check"]) == 0
    output = capsys.readouterr().out
    assert "qa_runtime_output" in output
    assert RUNTIME_PASSWORD not in output
    assert "management-secret" not in output


def test_permission_contract_keeps_tenant_identity_read_only_and_functions_explicit():
    source = (
        Path("wxsearch/runtime_db_role.py")
        .read_text(encoding="utf-8")
        .replace("\n", " ")
    )
    normalized = " ".join(source.split())

    assert "REVOKE INSERT, UPDATE, DELETE, TRUNCATE, REFERENCES," in normalized
    assert "TRIGGER ON TABLE public.tenants," in normalized
    assert "public.tenant_memberships FROM {}" in normalized
    assert "GRANT SELECT ON TABLE public.tenants," in normalized
    assert "public.tenant_memberships TO {}" in normalized
    assert (
        "'schema_migrations', 'tenants', 'tenant_memberships'"
    ) in normalized
    assert "tenant identity tables must be runtime SELECT-only" in normalized
    assert "INSERT INTO public.{table_name} SELECT *" in normalized
    assert "UPDATE public.{table_name} SET id=id WHERE FALSE" in normalized

    # Future public application tables retain compatibility DML, but an
    # identity/control-plane table must never rely on that broad default.
    assert "GRANT SELECT, INSERT, UPDATE ON TABLES TO {}" in normalized
    assert "Identity/control-plane tables require an explicit" in source
    assert "must never inherit this write grant silently" in source
    assert "REVOKE EXECUTE ON FUNCTIONS FROM PUBLIC" in normalized
    assert "public.app_list_active_tenants(uuid) TO {}" in normalized
    assert "public.app_authorize_tenant_write(integer,uuid)" in normalized
    assert "runtime tenant write authorization grant is missing" in source
    assert "public.app_lock_active_review_grant(uuid,uuid)" in normalized
    assert "runtime active grant lock function grant is missing" in source
    assert "public.app_lock_active_review_ruleset(uuid)" in normalized
    assert "runtime active ruleset lock function grant is missing" in source
    for table_name in (
        "review_rulesets",
        "review_ruleset_completion_reasons",
        "review_ruleset_reopen_reasons",
        "tenant_review_ruleset_activations",
        "tenant_candidate_score_snapshots",
    ):
        assert table_name in source
