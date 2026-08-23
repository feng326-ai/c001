from pathlib import Path

import pytest

from wxsearch.distributor_db_role import (
    FUNCTION_SIGNATURES,
    DistributorRoleError,
    DistributorRoleSettings,
    _distributor_connection_parameters,
    _validate_role_name,
    _validate_settings,
    load_settings,
    main,
)

MIGRATION_URL = "postgresql://migration-admin:qa-management-secret@db:5432/app"
DISTRIBUTOR_PASSWORD = "qa-distributor-password-0123456789"


@pytest.mark.parametrize(
    "role_name",
    (
        "xiansuo_distributor",
        "qa_distributor_0123abcd",
        "review_distributor_login",
    ),
)
def test_distributor_role_allowlist_accepts_scoped_names(role_name: str):
    assert _validate_role_name(role_name) == role_name


@pytest.mark.parametrize(
    "role_name",
    (
        "admin",
        "postgres",
        "public",
        "pg_distributor",
        "Distributor",
        "distributor;drop_role",
        "review_distributorx",
        "application_runtime",
        "x" * 64,
    ),
)
def test_distributor_role_allowlist_rejects_unsafe_names(role_name: str):
    with pytest.raises(DistributorRoleError, match="distributor role identifier"):
        _validate_role_name(role_name)


def test_settings_support_secret_files_without_exposing_values(tmp_path: Path):
    migration_file = tmp_path / "migration-url"
    user_file = tmp_path / "distributor-user"
    password_file = tmp_path / "distributor-password"
    migration_file.write_text(f"{MIGRATION_URL}\n", encoding="utf-8")
    user_file.write_text("qa_distributor_file\n", encoding="utf-8")
    password_file.write_text(f"{DISTRIBUTOR_PASSWORD}\n", encoding="utf-8")

    settings = load_settings(
        {
            "MIGRATION_DATABASE_URL_FILE": str(migration_file),
            "DISTRIBUTOR_DATABASE_USER_FILE": str(user_file),
            "DISTRIBUTOR_DATABASE_PASSWORD_FILE": str(password_file),
        }
    )

    assert settings.migration_database_url == MIGRATION_URL
    assert settings.distributor_database_user == "qa_distributor_file"
    assert settings.distributor_database_password == DISTRIBUTOR_PASSWORD
    representation = repr(settings)
    assert DISTRIBUTOR_PASSWORD not in representation
    assert "management-secret" not in representation


def test_settings_reject_ambiguous_direct_and_file_values(tmp_path: Path):
    password_file = tmp_path / "password"
    password_file.write_text(DISTRIBUTOR_PASSWORD, encoding="utf-8")
    environment = {
        "MIGRATION_DATABASE_URL": MIGRATION_URL,
        "DISTRIBUTOR_DATABASE_USER": "qa_distributor_conflict",
        "DISTRIBUTOR_DATABASE_PASSWORD": DISTRIBUTOR_PASSWORD,
        "DISTRIBUTOR_DATABASE_PASSWORD_FILE": str(password_file),
    }
    with pytest.raises(DistributorRoleError, match="exactly one"):
        load_settings(environment)


@pytest.mark.parametrize(
    "password",
    ("short", "sixteen-chars-ok\nextra", "sixteen-chars-ok\x00bad"),
)
def test_settings_reject_weak_or_multiline_passwords(password: str):
    environment = {
        "MIGRATION_DATABASE_URL": MIGRATION_URL,
        "DISTRIBUTOR_DATABASE_USER": "qa_distributor_password",
        "DISTRIBUTOR_DATABASE_PASSWORD": password,
    }
    with pytest.raises(DistributorRoleError, match="PASSWORD"):
        load_settings(environment)


def test_constructed_settings_are_revalidated_before_database_use():
    settings = DistributorRoleSettings(
        migration_database_url=MIGRATION_URL,
        distributor_database_user="admin",
        distributor_database_password=DISTRIBUTOR_PASSWORD,
    )
    with pytest.raises(DistributorRoleError):
        _validate_settings(settings)


def test_distributor_role_cannot_overlap_runtime_identity():
    settings = DistributorRoleSettings(
        migration_database_url=MIGRATION_URL,
        distributor_database_user="qa_runtime_distributor",
        distributor_database_password=DISTRIBUTOR_PASSWORD,
    )
    with pytest.raises(DistributorRoleError, match="role identifier"):
        _validate_settings(settings)


@pytest.mark.parametrize(
    "role_name,password",
    [
        ("replace_with_distributor_role", DISTRIBUTOR_PASSWORD),
        ("qa_distributor_placeholder", "replace-with-a-separate-password"),
    ],
)
def test_distributor_settings_reject_placeholders(role_name, password):
    settings = DistributorRoleSettings(
        migration_database_url=MIGRATION_URL,
        distributor_database_user=role_name,
        distributor_database_password=password,
    )
    with pytest.raises(DistributorRoleError):
        _validate_settings(settings)


def test_distributor_connection_reuses_target_but_overrides_identity():
    settings = DistributorRoleSettings(
        migration_database_url=(
            "postgresql://migration-admin:qa-management-secret@db:5433/app"
            "?sslmode=require&connect_timeout=7"
        ),
        distributor_database_user="qa_distributor_dsn",
        distributor_database_password=DISTRIBUTOR_PASSWORD,
    )

    parameters = _distributor_connection_parameters(settings)

    assert parameters["host"] == "db"
    assert parameters["port"] == "5433"
    assert parameters["dbname"] == "app"
    assert parameters["sslmode"] == "require"
    assert parameters["connect_timeout"] == "7"
    assert parameters["user"] == "qa_distributor_dsn"
    assert parameters["password"] == DISTRIBUTOR_PASSWORD


def test_cli_output_never_contains_database_secrets(monkeypatch, capsys):
    settings = DistributorRoleSettings(
        migration_database_url=MIGRATION_URL,
        distributor_database_user="qa_distributor_output",
        distributor_database_password=DISTRIBUTOR_PASSWORD,
    )
    monkeypatch.setattr("wxsearch.distributor_db_role.load_settings", lambda: settings)
    monkeypatch.setattr(
        "wxsearch.distributor_db_role.check_distributor_role",
        lambda value: {
            "status": "verified",
            "distributor_user": value.distributor_database_user,
        },
    )

    assert main(["check"]) == 0
    output = capsys.readouterr().out
    assert "qa_distributor_output" in output
    assert DISTRIBUTOR_PASSWORD not in output
    assert "management-secret" not in output


def test_distributor_capability_is_exactly_four_narrow_functions():
    assert FUNCTION_SIGNATURES == (
        "public.app_expand_review_distribution(uuid)",
        "public.app_claim_review_distribution_target(text,integer)",
        "public.app_apply_review_distribution_target(uuid,uuid)",
        "public.app_report_review_distribution_failure(uuid,uuid,text)",
    )
    assert all("tenant" not in signature for signature in FUNCTION_SIGNATURES)


def test_permission_contract_has_no_direct_data_access_or_unsafe_definer():
    source = Path("wxsearch/distributor_db_role.py").read_text(encoding="utf-8")
    normalized = " ".join(source.split())

    assert "LOGIN NOSUPERUSER NOCREATEDB" in normalized
    assert "NOCREATEROLE NOINHERIT NOREPLICATION NOBYPASSRLS" in normalized
    assert "REVOKE ALL PRIVILEGES ON ALL TABLES IN SCHEMA {}" in normalized
    assert "REVOKE ALL PRIVILEGES ON ALL SEQUENCES IN SCHEMA {}" in normalized
    assert "REVOKE EXECUTE ON ALL FUNCTIONS IN SCHEMA {} FROM {}" in normalized
    assert "GRANT USAGE ON SCHEMA public TO {}" in normalized
    assert "GRANT CONNECT ON DATABASE {} TO {}" in normalized
    assert "GRANT EXECUTE ON FUNCTION {} TO {}" in normalized
    assert "search_path=pg_catalog" in source
    assert "owner.rolsuper OR owner.rolbypassrls" in source
    assert "acl.grantee=0 AND acl.privilege_type='EXECUTE'" in normalized
    assert "procedure.prosecdef" in source
    assert "has_function_privilege(" in source
    assert "distributor can execute unexpected privileged functions" in source
    assert "distributor has unexpected direct function privileges" in source
    assert "distributor must not have table privileges" in source
    assert "distributor must not have sequence privileges" in source
