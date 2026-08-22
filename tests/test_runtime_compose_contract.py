from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[1]
RUNTIME_SERVICES = ("backend", "celery-worker", "celery-beat")


def _compose(name: str) -> dict:
    return yaml.safe_load((ROOT / name).read_text(encoding="utf-8"))


def _environment(service: dict) -> dict:
    value = service.get("environment", {})
    assert isinstance(value, dict)
    return value


def test_long_running_services_never_receive_migration_or_bootstrap_credentials():
    for compose_name in (
        "docker-compose.yml",
        "docker-compose.prod.yml",
        "docker-compose.staging.yml",
    ):
        services = _compose(compose_name)["services"]
        for service_name in RUNTIME_SERVICES:
            if service_name not in services:
                continue
            environment = _environment(services[service_name])
            database_url = str(environment.get("DATABASE_URL", ""))
            assert "APP_DATABASE" in database_url
            assert "admin:" not in database_url
            assert "MIGRATION_DATABASE_URL" not in environment
            assert "POSTGRES_PASSWORD" not in environment
            assert "STAGING_POSTGRES_PASSWORD" not in environment


def test_database_management_services_are_one_shot_admin_profile_only():
    for compose_name in (
        "docker-compose.yml",
        "docker-compose.prod.yml",
        "docker-compose.staging.yml",
    ):
        services = _compose(compose_name)["services"]
        for service_name in ("migrate", "provision-runtime-role"):
            service = services[service_name]
            assert service["profiles"] == ["admin"]
            assert str(service.get("restart", "")).lower() == "no"

        migrate_environment = _environment(services["migrate"])
        assert "MIGRATION_DATABASE_URL" in migrate_environment
        assert "DATABASE_URL" not in migrate_environment

        provision_environment = _environment(services["provision-runtime-role"])
        assert "MIGRATION_DATABASE_URL" in provision_environment
        assert "DATABASE_URL" in provision_environment
        assert "APP_DATABASE_USER" in provision_environment
        assert "APP_DATABASE_PASSWORD" in provision_environment


def test_runtime_credentials_are_required_placeholders_not_admin_fallbacks():
    environment_template = (ROOT / ".env.example").read_text(encoding="utf-8")
    staging_template = (ROOT / ".env.staging.example").read_text(
        encoding="utf-8"
    )

    assert "APP_DATABASE_USER=replace_" in environment_template
    assert "APP_DATABASE_PASSWORD=replace-" in environment_template
    assert "STAGING_APP_DATABASE_USER=replace_" in staging_template
    assert "STAGING_APP_DATABASE_PASSWORD=replace-" in staging_template


def test_qa_runner_exposes_explicit_migration_dsn_only_inside_disposable_qa():
    services = _compose("docker-compose.qa.yml")["services"]
    environment = _environment(services["test-runner"])

    assert environment["ENVIRONMENT"] == "qa"
    assert "_lease_qa_" in environment["DATABASE_URL"]
    assert environment["MIGRATION_DATABASE_URL"] == environment["DATABASE_URL"]
