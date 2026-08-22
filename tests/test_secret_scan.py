from pathlib import Path

from tools.check_secrets import scan_file, tracked_file_policy


def test_real_literals_are_reported_without_storing_value(tmp_path: Path):
    candidate = tmp_path / "bad.env"
    candidate.write_text(
        "SERVICE_" + "PASSWORD='" + "actual-value-123'\n",
        encoding="utf-8",
    )

    findings = scan_file(candidate)

    assert [(finding.line, finding.rule) for finding in findings] == [
        (1, "QUOTED_SECRET"),
        (1, "CONFIG_SECRET"),
    ]
    assert all(not hasattr(finding, "value") for finding in findings)


def test_placeholders_and_runtime_expressions_are_allowed(tmp_path: Path):
    candidate = tmp_path / "safe.env"
    candidate.write_text(
        "PASSWORD=replace-with-a-secret\n"
        "TOKEN=${TOKEN}\n"
        "secret = os.getenv('SERVICE_SECRET')\n"
        "DATABASE_URL=postgresql://${APP_DATABASE_USER:?user_required}:"
        "${APP_DATABASE_PASSWORD:?password_required}@postgres/app\n",
        encoding="utf-8",
    )

    assert scan_file(candidate) == []


def test_literal_url_password_is_still_reported(tmp_path: Path):
    candidate = tmp_path / "unsafe.yml"
    candidate.write_text(
        "DATABASE_URL: postgresql://service_user:"
        + "actual-value-123@db/app\n",
        encoding="utf-8",
    )

    assert [(finding.line, finding.rule) for finding in scan_file(candidate)] == [
        (1, "URL_CREDENTIAL")
    ]


def test_private_key_and_provider_tokens_are_detected(tmp_path: Path):
    candidate = tmp_path / "credentials.txt"
    candidate.write_text(
        "-----BEGIN " + "PRIVATE KEY-----\n"
        "ghp_" + "abcdefghijklmnopqrstuvwxyz123456\n",
        encoding="utf-8",
    )

    assert {finding.rule for finding in scan_file(candidate)} == {
        "PRIVATE_KEY",
        "GITHUB_TOKEN",
    }


def test_sensitive_runtime_filenames_cannot_be_tracked(tmp_path: Path):
    paths = [tmp_path / "config.json", tmp_path / ".env", tmp_path / "safe.env.example"]

    assert [finding.path.name for finding in tracked_file_policy(paths)] == [
        "config.json",
        ".env",
    ]
