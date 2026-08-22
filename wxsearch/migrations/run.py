#!/usr/bin/env python3
"""Apply and verify ordered SQL migrations.

The runner is deliberately fail-closed: every already-applied migration must
still exist in the repository and its recorded checksum must match before a
new migration can run. Historical MD5 rows remain readable; new rows use
SHA-256.
"""

import argparse
import hashlib
import json
import os
import re
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent.parent
MIGRATIONS_DIR = BASE_DIR / "docs" / "migrations"
CHECKSUM_BASELINE = MIGRATIONS_DIR / "checksum_baseline.json"
MIGRATION_NAME = re.compile(
    r"^(?P<version>\d{3})_(?P<description>[a-z0-9][a-z0-9_]*)\.sql$"
)
DOCUMENTED_LEGACY_DRIFT_MD5 = {
    "018": frozenset({"bbaae3b66fb9b6f1fe7be94b46391093"}),
}
LEGACY_MD5_THROUGH = "020"
LEGACY_FINAL_STATE_IDS = {"018": "018"}
# PostgreSQL 15, pg_get_viewdef(..., true), UTF-8 bytes. This is the exact
# historical schema state paired with the documented 018 drift checksum.
KW_STAT_VALUE_PG15_VIEWDEF_SHA256 = (
    "a569ce60bdb797c694f1bef5d5195b8ab8f4d635cadc6d1ac347cc0df07af2da"
)


class MigrationIntegrityError(RuntimeError):
    """The repository and database migration histories do not agree."""


def get_db_connection():
    """Use the one-shot migration DSN, with legacy fallback for old stacks."""
    import psycopg2

    migration_url = os.getenv("MIGRATION_DATABASE_URL")
    if migration_url:
        return psycopg2.connect(migration_url)

    from wxsearch.tasks import _db_config

    return psycopg2.connect(**_db_config())


def get_checksum(file_path: Path, algorithm: str = "sha256") -> str:
    """Hash canonical UTF-8/LF content so Windows checkout mode is irrelevant."""
    text = file_path.read_text(encoding="utf-8-sig")
    canonical = text.replace("\r\n", "\n").replace("\r", "\n").encode("utf-8")
    return hashlib.new(algorithm, canonical).hexdigest()


def get_migration_files(directory: Path = MIGRATIONS_DIR) -> list[Path]:
    """Return a validated, contiguous migration sequence."""
    if not directory.exists():
        raise MigrationIntegrityError(
            f"migration directory is missing: {directory}"
        )

    files = sorted(
        path
        for path in directory.glob("*.sql")
        if path.name != "schema_migrations.sql"
    )
    if not files:
        raise MigrationIntegrityError("no migration files found")

    versions: list[int] = []
    seen: set[str] = set()
    for file_path in files:
        match = MIGRATION_NAME.fullmatch(file_path.name)
        if not match:
            raise MigrationIntegrityError(
                f"invalid migration filename: {file_path.name}; "
                "expected NNN_description.sql"
            )
        version = match.group("version")
        if version in seen:
            raise MigrationIntegrityError(
                f"duplicate migration version: {version}"
            )
        seen.add(version)
        versions.append(int(version))

    expected = list(range(1, len(versions) + 1))
    if versions != expected:
        raise MigrationIntegrityError(
            f"migration versions must be contiguous from 001: found {versions}"
        )
    return files


def get_applied_migrations(conn) -> dict[str, str | None]:
    """Return the database migration history including its checksums."""
    with conn.cursor() as cursor:
        cursor.execute(
            "SELECT version, checksum FROM schema_migrations ORDER BY version"
        )
        return {
            str(version): checksum for version, checksum in cursor.fetchall()
        }


def get_applied_versions(conn) -> set[str]:
    """Compatibility helper retained for existing callers."""
    return set(get_applied_migrations(conn))


def checksum_matches(file_path: Path, recorded_checksum: str | None) -> bool:
    """New-format history accepts only the current canonical SHA-256."""
    checksum = str(recorded_checksum or "").strip().lower()
    return bool(
        len(checksum) == 64
        and checksum == get_checksum(file_path, "sha256")
    )


def load_checksum_baseline(
    files: list[Path], baseline_path: Path = CHECKSUM_BASELINE
) -> dict[str, dict]:
    """Validate the immutable repository-side SHA-256 manifest."""
    if not baseline_path.exists():
        raise MigrationIntegrityError(
            f"checksum baseline is missing: {baseline_path}"
        )
    try:
        payload = json.loads(baseline_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise MigrationIntegrityError(
            f"invalid checksum baseline: {error}"
        ) from error
    if payload.get("algorithm") != "sha256-canonical-lf-v1":
        raise MigrationIntegrityError("unsupported checksum baseline algorithm")
    legacy_md5_through = str(payload.get("legacy_md5_through") or "")
    if legacy_md5_through != LEGACY_MD5_THROUGH:
        raise MigrationIntegrityError(
            f"legacy MD5 cutoff must remain frozen at {LEGACY_MD5_THROUGH}"
        )

    entries = payload.get("migrations")
    if not isinstance(entries, dict):
        raise MigrationIntegrityError("checksum baseline migrations must be an object")
    repository = {path.name.split("_", 1)[0]: path for path in files}
    if set(entries) != set(repository):
        raise MigrationIntegrityError(
            "checksum baseline versions differ from repository migrations"
        )

    for version, file_path in repository.items():
        entry = entries.get(version)
        if not isinstance(entry, dict):
            raise MigrationIntegrityError(
                f"invalid checksum baseline entry: {version}"
            )
        expected = str(entry.get("sha256") or "").lower()
        if not re.fullmatch(r"[0-9a-f]{64}", expected):
            raise MigrationIntegrityError(
                f"invalid SHA-256 baseline for migration {version}"
            )
        if get_checksum(file_path, "sha256") != expected:
            raise MigrationIntegrityError(
                f"repository migration differs from frozen baseline: {version}"
            )
        is_legacy_version = version <= legacy_md5_through
        legacy_fields = {
            "accepted_legacy_md5",
            "legacy_drift_md5",
            "legacy_final_state",
        }
        if not is_legacy_version:
            if legacy_fields.intersection(entry):
                raise MigrationIntegrityError(
                    f"migration after legacy cutoff declares legacy "
                    f"compatibility: {version}"
                )
            continue

        raw_accepted = entry.get("accepted_legacy_md5")
        raw_drift = entry.get("legacy_drift_md5", [])
        if not isinstance(raw_accepted, list) or not isinstance(raw_drift, list):
            raise MigrationIntegrityError(
                f"legacy MD5 baselines must be arrays for migration {version}"
            )
        for legacy_checksum in [*raw_accepted, *raw_drift]:
            if not re.fullmatch(r"[0-9a-f]{32}", str(legacy_checksum)):
                raise MigrationIntegrityError(
                    f"invalid legacy MD5 baseline for migration {version}"
                )
        accepted_checksums = set(raw_accepted)
        drift_checksums = set(raw_drift)
        if (
            len(accepted_checksums) != len(raw_accepted)
            or len(drift_checksums) != len(raw_drift)
        ):
            raise MigrationIntegrityError(
                f"duplicate legacy MD5 baseline for migration {version}"
            )
        documented_drift = DOCUMENTED_LEGACY_DRIFT_MD5.get(
            version, frozenset()
        )
        if drift_checksums != documented_drift:
            raise MigrationIntegrityError(
                f"undocumented legacy drift baseline for migration {version}"
            )
        current_md5 = get_checksum(file_path, "md5")
        if accepted_checksums != {current_md5, *documented_drift}:
            raise MigrationIntegrityError(
                f"accepted legacy MD5 must equal the canonical MD5 plus "
                f"documented drift for migration {version}"
            )
        expected_final_state = LEGACY_FINAL_STATE_IDS.get(version)
        if entry.get("legacy_final_state") != expected_final_state:
            raise MigrationIntegrityError(
                f"invalid legacy final-state marker for migration {version}"
            )
    return entries


def _legacy_final_state_is_valid(conn, version: str) -> bool:
    """Verify the exact schema state behind a documented legacy checksum."""
    with conn.cursor() as cursor:
        if version == "018":
            cursor.execute(
                "SELECT pg_get_viewdef('kw_stat_value'::regclass, true)"
            )
            row = cursor.fetchone()
            if not row or not isinstance(row[0], str):
                return False
            definition_hash = hashlib.sha256(
                row[0].encode("utf-8")
            ).hexdigest()
            return definition_hash == KW_STAT_VALUE_PG15_VIEWDEF_SHA256
        if version == "019":
            cursor.execute(
                """
                SELECT data_type, column_default
                FROM information_schema.columns
                WHERE table_schema=current_schema()
                  AND table_name='articles_core'
                  AND column_name='channels_jsonb_legacy'
                """
            )
            row = cursor.fetchone()
            return bool(
                row
                and row[0] == "jsonb"
                and "wechat_pc" in str(row[1] or "")
            )
        if version == "020":
            cursor.execute(
                """
                SELECT COUNT(*) FROM pg_class
                WHERE oid IN (
                    to_regclass('production_sync_settings'),
                    to_regclass('production_sync_outbox'),
                    to_regclass('production_sync_receipts'),
                    to_regclass('production_sync_entity_versions'),
                    to_regclass('production_sync_article_keys')
                )
                """
            )
            table_count = cursor.fetchone()[0]
            cursor.execute(
                """
                SELECT COUNT(*) FROM pg_trigger
                WHERE NOT tgisinternal AND tgenabled <> 'D'
                  AND tgname IN (
                    'trg_articles_production_sync',
                    'trg_leads_production_sync'
                  )
                """
            )
            trigger_count = cursor.fetchone()[0]
            cursor.execute(
                "SELECT to_regprocedure('enqueue_production_sync_event()') IS NOT NULL"
            )
            function_exists = cursor.fetchone()[0]
            return bool(
                table_count == 5
                and trigger_count == 2
                and function_exists
            )
    return False


def validate_applied_migrations(
    conn,
    files: list[Path],
    baseline: dict[str, dict] | None = None,
    *,
    require_all: bool = False,
) -> int:
    """Fail if an applied migration is absent, unhashed, or modified."""
    repository = {path.name.split("_", 1)[0]: path for path in files}
    applied = get_applied_migrations(conn)

    missing = sorted(set(applied) - set(repository))
    if missing:
        raise MigrationIntegrityError(
            "applied migrations missing from repository: "
            + ", ".join(missing)
        )

    repository_order = list(repository)
    applied_order = list(applied)
    expected_prefix = repository_order[: len(applied_order)]
    if applied_order != expected_prefix:
        raise MigrationIntegrityError(
            "applied migration history is not a contiguous repository prefix: "
            + ", ".join(applied_order)
        )

    if require_all:
        pending = sorted(set(repository) - set(applied))
        if pending:
            raise MigrationIntegrityError(
                "pending migrations: " + ", ".join(pending)
            )

    invalid = []
    for version, checksum in applied.items():
        if checksum_matches(repository[version], checksum):
            continue
        entry = (baseline or {}).get(version, {})
        accepted = {
            str(item).lower()
            for item in entry.get("accepted_legacy_md5", [])
        }
        drift = {
            str(item).lower()
            for item in entry.get("legacy_drift_md5", [])
        }
        normalized = str(checksum or "").strip().lower()
        if normalized in accepted:
            current_md5 = get_checksum(repository[version], "md5")
            if normalized == current_md5 and normalized not in drift:
                continue
            if (
                normalized
                in DOCUMENTED_LEGACY_DRIFT_MD5.get(version, frozenset())
                and drift
                == DOCUMENTED_LEGACY_DRIFT_MD5.get(version, frozenset())
                and entry.get("legacy_final_state")
                == LEGACY_FINAL_STATE_IDS.get(version)
                and _legacy_final_state_is_valid(conn, version)
            ):
                continue
        invalid.append(version)
    if invalid:
        raise MigrationIntegrityError(
            "migration checksum mismatch or missing checksum: "
            + ", ".join(sorted(invalid))
        )
    return len(applied)


def ensure_history_table(conn, *, create: bool) -> None:
    """Require the history table, optionally creating it for a new database."""
    try:
        with conn.cursor() as cursor:
            cursor.execute("SELECT 1 FROM schema_migrations LIMIT 1")
        conn.rollback()
        return
    except Exception as error:
        conn.rollback()
        if getattr(error, "pgcode", None) != "42P01":
            raise
        if not create:
            raise MigrationIntegrityError(
                "schema_migrations table is missing"
            )

    schema_file = MIGRATIONS_DIR / "schema_migrations.sql"
    if not schema_file.exists():
        raise MigrationIntegrityError(
            f"migration history schema is missing: {schema_file}"
        )
    with conn.cursor() as cursor:
        cursor.execute(schema_file.read_text(encoding="utf-8"))
    conn.commit()


def apply_migration(
    conn, version: str, file_path: Path, description: str
) -> None:
    """Apply one migration and atomically record its SHA-256 checksum."""
    print(f"[+] Applying migration {version}: {description}")
    sql = file_path.read_text(encoding="utf-8")
    checksum = get_checksum(file_path, "sha256")

    try:
        with conn.cursor() as cursor:
            cursor.execute(sql)
            cursor.execute(
                """
                INSERT INTO schema_migrations(version, description, checksum)
                VALUES (%s, %s, %s)
                """,
                (version, description, checksum),
            )
        conn.commit()
        print(f"    Applied {version}")
    except Exception:
        conn.rollback()
        raise


def run(*, check_only: bool = False) -> tuple[int, int]:
    """Verify history and optionally apply pending files."""
    files = get_migration_files()
    baseline = load_checksum_baseline(files)
    conn = get_db_connection()
    lock_held = False
    try:
        if not check_only:
            with conn.cursor() as cursor:
                cursor.execute(
                    "SELECT pg_advisory_lock(hashtext(%s))",
                    ("wxsearch_schema_migrations",),
                )
            lock_held = True
        ensure_history_table(conn, create=not check_only)
        verified = validate_applied_migrations(
            conn, files, baseline, require_all=check_only
        )
        if check_only:
            print(
                f"Migration integrity OK: {verified} applied file(s) verified"
            )
            return 0, verified

        applied = get_applied_versions(conn)
        new_count = 0
        for file_path in files:
            version, description = file_path.stem.split("_", 1)
            if version in applied:
                print(f"[i] Skipped {version}: already applied and verified")
                continue
            apply_migration(conn, version, file_path, description)
            new_count += 1

        verified = validate_applied_migrations(
            conn, files, baseline, require_all=True
        )
        print(
            f"Migration complete. New: {new_count}, "
            f"Verified history: {verified}"
        )
        return new_count, verified
    finally:
        if lock_held:
            try:
                with conn.cursor() as cursor:
                    cursor.execute(
                        "SELECT pg_advisory_unlock(hashtext(%s))",
                        ("wxsearch_schema_migrations",),
                    )
            except Exception:
                conn.rollback()
        conn.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--check",
        action="store_true",
        help="read-only verification; never create tables or apply migrations",
    )
    args = parser.parse_args(argv)
    try:
        run(check_only=args.check)
    except MigrationIntegrityError as error:
        print(f"Migration integrity check failed: {error}")
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
