import hashlib
import json
import os
from pathlib import Path
from urllib.parse import urlsplit

import pytest

from wxsearch.migrations.run import (
    DOCUMENTED_LEGACY_DRIFT_MD5,
    KW_STAT_VALUE_PG15_VIEWDEF_SHA256,
    MigrationIntegrityError,
    get_checksum,
    get_migration_files,
    load_checksum_baseline,
    validate_applied_migrations,
)


class FakeCursor:
    def __init__(self, rows):
        self.rows = rows

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def execute(self, *_args):
        return None

    def fetchall(self):
        return self.rows


class FakeConnection:
    def __init__(self, rows):
        self.rows = rows

    def cursor(self):
        return FakeCursor(self.rows)


def _migration(directory: Path, name: str, text: str = "SELECT 1;\n") -> Path:
    path = directory / name
    path.write_text(text, encoding="utf-8", newline="")
    return path


def _write_baseline(
    path: Path,
    file_path: Path,
    *,
    accepted: list[str] | None,
    drift: list[str] | None = None,
    final_state: str | None = None,
    cutoff: str = "020",
) -> None:
    version = file_path.name.split("_", 1)[0]
    entry = {
        "sha256": get_checksum(file_path),
    }
    if accepted is not None:
        entry["accepted_legacy_md5"] = accepted
    if drift is not None:
        entry["legacy_drift_md5"] = drift
    if final_state is not None:
        entry["legacy_final_state"] = final_state
    path.write_text(
        json.dumps(
            {
                "algorithm": "sha256-canonical-lf-v1",
                "legacy_md5_through": cutoff,
                "migrations": {version: entry},
            }
        ),
        encoding="utf-8",
    )


def test_checksum_is_stable_across_lf_and_crlf(tmp_path: Path):
    lf = _migration(tmp_path, "001_one.sql", "SELECT 1;\n")
    crlf = _migration(tmp_path, "002_two.sql", "SELECT 1;\r\n")

    assert get_checksum(lf) == get_checksum(crlf)


def test_migration_names_must_be_contiguous(tmp_path: Path):
    _migration(tmp_path, "001_one.sql")
    _migration(tmp_path, "003_three.sql")

    with pytest.raises(MigrationIntegrityError, match="contiguous"):
        get_migration_files(tmp_path)


def test_null_or_changed_checksum_fails_closed(tmp_path: Path):
    file_path = _migration(tmp_path, "001_one.sql")
    for checksum in (None, "0" * 64):
        conn = FakeConnection([("001", checksum)])
        with pytest.raises(MigrationIntegrityError, match="checksum"):
            validate_applied_migrations(conn, [file_path])


def test_legacy_md5_and_sha256_are_both_readable(tmp_path: Path):
    file_path = _migration(tmp_path, "001_one.sql")
    canonical = file_path.read_text(encoding="utf-8").encode("utf-8")
    legacy_md5 = hashlib.md5(canonical).hexdigest()
    sha256 = hashlib.sha256(canonical).hexdigest()

    assert validate_applied_migrations(
        FakeConnection([("001", sha256)]), [file_path]
    ) == 1
    assert validate_applied_migrations(
        FakeConnection([("001", legacy_md5)]),
        [file_path],
        {"001": {"accepted_legacy_md5": [legacy_md5]}},
    ) == 1


def test_manifest_cannot_relabel_old_md5_as_current(tmp_path: Path):
    file_path = _migration(tmp_path, "001_one.sql")
    old_md5 = get_checksum(file_path, "md5")
    file_path.write_text("SELECT 2;\n", encoding="utf-8", newline="")
    baseline_path = tmp_path / "baseline.json"
    _write_baseline(baseline_path, file_path, accepted=[old_md5])

    with pytest.raises(MigrationIntegrityError, match="canonical MD5"):
        load_checksum_baseline([file_path], baseline_path)


def test_manifest_allows_only_the_hard_coded_018_drift(tmp_path: Path):
    file_path = _migration(tmp_path, "018_kw_stat_value.sql")
    current_md5 = get_checksum(file_path, "md5")
    baseline_path = tmp_path / "baseline.json"
    unknown_drift = hashlib.md5(b"unknown historical file").hexdigest()
    _write_baseline(
        baseline_path,
        file_path,
        accepted=[current_md5, unknown_drift],
        drift=[unknown_drift],
        final_state="018",
    )

    with pytest.raises(
        MigrationIntegrityError, match="undocumented legacy drift"
    ):
        load_checksum_baseline([file_path], baseline_path)

    documented_drift = next(iter(DOCUMENTED_LEGACY_DRIFT_MD5["018"]))
    _write_baseline(
        baseline_path,
        file_path,
        accepted=[current_md5, documented_drift],
        drift=[documented_drift],
        final_state="018",
    )
    assert load_checksum_baseline([file_path], baseline_path)["018"]


def test_manifest_forbids_legacy_fields_after_frozen_cutoff(tmp_path: Path):
    file_path = _migration(tmp_path, "021_new_work.sql")
    baseline_path = tmp_path / "baseline.json"
    _write_baseline(baseline_path, file_path, accepted=None)
    assert load_checksum_baseline([file_path], baseline_path)["021"]

    _write_baseline(
        baseline_path,
        file_path,
        accepted=[get_checksum(file_path, "md5")],
    )
    with pytest.raises(MigrationIntegrityError, match="after legacy cutoff"):
        load_checksum_baseline([file_path], baseline_path)

    _write_baseline(baseline_path, file_path, accepted=None, cutoff="021")
    with pytest.raises(MigrationIntegrityError, match="remain frozen"):
        load_checksum_baseline([file_path], baseline_path)


def test_unlisted_md5_is_rejected(tmp_path: Path):
    file_path = _migration(tmp_path, "001_one.sql")
    conn = FakeConnection([("001", hashlib.md5(b"other").hexdigest())])

    with pytest.raises(MigrationIntegrityError, match="checksum"):
        validate_applied_migrations(conn, [file_path], {"001": {}})


def test_read_only_check_rejects_pending_migration(tmp_path: Path):
    first = _migration(tmp_path, "001_one.sql")
    second = _migration(tmp_path, "002_two.sql")
    conn = FakeConnection([("001", get_checksum(first))])

    with pytest.raises(MigrationIntegrityError, match="pending"):
        validate_applied_migrations(
            conn, [first, second], require_all=True
        )


def test_applied_history_must_be_a_contiguous_prefix(tmp_path: Path):
    first = _migration(tmp_path, "001_one.sql")
    second = _migration(tmp_path, "002_two.sql")
    third = _migration(tmp_path, "003_three.sql")
    conn = FakeConnection(
        [("001", get_checksum(first)), ("003", get_checksum(third))]
    )

    with pytest.raises(MigrationIntegrityError, match="contiguous"):
        validate_applied_migrations(conn, [first, second, third])


def test_repository_checksum_baseline_is_current():
    files = get_migration_files()
    assert len(load_checksum_baseline(files)) == len(files)


def _guarded_qa_database_url() -> str:
    if (
        os.getenv("ENVIRONMENT") != "qa"
        or os.getenv("ALLOW_DESTRUCTIVE_TESTS") != "1"
    ):
        raise RuntimeError("real migration test requires guarded QA environment")
    database_url = os.environ["DATABASE_URL"]
    if "_lease_qa_" not in urlsplit(database_url).path:
        raise RuntimeError("real migration test refused unsafe database")
    return database_url


@pytest.mark.skipif(
    os.getenv("RUN_MIGRATION_INTEGRATION") != "1",
    reason="requires disposable QA PostgreSQL",
)
def test_real_database_detects_checksum_tampering():
    database_url = _guarded_qa_database_url()

    import psycopg2

    conn = psycopg2.connect(database_url)
    files = get_migration_files()
    baseline = load_checksum_baseline(files)
    try:
        assert validate_applied_migrations(
            conn, files, baseline, require_all=True
        ) == len(files)
        with conn.cursor() as cursor:
            cursor.execute(
                "UPDATE schema_migrations SET checksum=%s WHERE version='001'",
                ("0" * 64,),
            )
        with pytest.raises(MigrationIntegrityError, match="checksum"):
            validate_applied_migrations(
                conn, files, baseline, require_all=True
            )
        conn.rollback()
    finally:
        conn.close()


@pytest.mark.skipif(
    os.getenv("RUN_MIGRATION_INTEGRATION") != "1",
    reason="requires disposable QA PostgreSQL 15",
)
def test_real_pg15_accepts_only_exact_018_historical_ledger_state():
    database_url = _guarded_qa_database_url()

    import psycopg2

    conn = psycopg2.connect(database_url)
    files = get_migration_files()
    baseline = load_checksum_baseline(files)
    migration_018 = next(path for path in files if path.name.startswith("018_"))
    historical_sql = (
        migration_018.read_text(encoding="utf-8")
        .replace("数据不足", "试用期")
        .replace("优质·建议高频", "优质·建议提频")
        .replace("观察·可能老化", "观察·近期哑火")
    )
    historical_md5 = next(iter(DOCUMENTED_LEGACY_DRIFT_MD5["018"]))
    try:
        with conn.cursor() as cursor:
            cursor.execute(
                "UPDATE schema_migrations SET checksum=%s WHERE version='018'",
                (historical_md5,),
            )
        with pytest.raises(MigrationIntegrityError, match="checksum"):
            validate_applied_migrations(
                conn, files, baseline, require_all=True
            )

        with conn.cursor() as cursor:
            cursor.execute(historical_sql)
            cursor.execute(
                "SELECT pg_get_viewdef('kw_stat_value'::regclass, true)"
            )
            definition = cursor.fetchone()[0]
        assert hashlib.sha256(definition.encode("utf-8")).hexdigest() == (
            KW_STAT_VALUE_PG15_VIEWDEF_SHA256
        )
        assert validate_applied_migrations(
            conn, files, baseline, require_all=True
        ) == len(files)

        with conn.cursor() as cursor:
            cursor.execute(historical_sql.replace("'正常'", "'需复核'"))
        with pytest.raises(MigrationIntegrityError, match="checksum"):
            validate_applied_migrations(
                conn, files, baseline, require_all=True
            )
        conn.rollback()
    finally:
        conn.close()
