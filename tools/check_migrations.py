#!/usr/bin/env python3
"""Offline migration filename and append-only history gate."""

import argparse
import hashlib
import json
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from wxsearch.migrations.run import (
    CHECKSUM_BASELINE,
    MigrationIntegrityError,
    get_checksum,
    get_migration_files,
    load_checksum_baseline,
)


MIGRATION_REPOSITORY_PATH = "docs/migrations"
MIGRATION_PATH = re.compile(
    r"^docs/migrations/(?P<version>\d{3})_[a-z0-9][a-z0-9_]*\.sql$"
)


def _canonical_sha256(raw: bytes) -> str:
    """Hash Git blob bytes with the same UTF-8/LF contract as the runner."""
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError as error:
        raise MigrationIntegrityError(
            "base migration is not valid UTF-8"
        ) from error
    canonical = text.replace("\r\n", "\n").replace("\r", "\n").encode(
        "utf-8"
    )
    return hashlib.sha256(canonical).hexdigest()


def _git(root: Path, *args: str) -> bytes:
    result = subprocess.run(
        ["git", *args],
        cwd=root,
        check=True,
        capture_output=True,
    )
    return result.stdout


def _resolve_base_ref(root: Path, base_ref: str) -> str:
    candidate = str(base_ref or "").strip()
    if not candidate or candidate.startswith("-"):
        raise MigrationIntegrityError("invalid migration comparison base ref")
    try:
        resolved = _git(
            root, "rev-parse", "--verify", f"{candidate}^{{commit}}"
        ).decode("ascii").strip()
    except (OSError, subprocess.CalledProcessError, UnicodeDecodeError) as error:
        raise MigrationIntegrityError(
            f"migration comparison base ref is unavailable: {candidate}"
        ) from error
    if not re.fullmatch(r"[0-9a-f]{40}", resolved):
        raise MigrationIntegrityError("invalid resolved migration base commit")
    return resolved


def load_base_history(
    root: Path, base_ref: str
) -> tuple[
    dict[str, tuple[str, str]],
    dict[str, dict] | None,
    dict[str, str] | None,
]:
    """Read migration filenames/checksums and optional manifest from Git."""
    resolved = _resolve_base_ref(root, base_ref)
    try:
        names = _git(
            root,
            "ls-tree",
            "-r",
            "--name-only",
            "-z",
            resolved,
            "--",
            MIGRATION_REPOSITORY_PATH,
        )
    except (OSError, subprocess.CalledProcessError) as error:
        raise MigrationIntegrityError(
            "failed to list migrations from comparison base"
        ) from error

    migrations: dict[str, tuple[str, str]] = {}
    for item in names.split(b"\0"):
        if not item:
            continue
        try:
            repository_path = item.decode("utf-8")
        except UnicodeDecodeError as error:
            raise MigrationIntegrityError(
                "base migration path is not valid UTF-8"
            ) from error
        match = MIGRATION_PATH.fullmatch(repository_path)
        if not match:
            continue
        version = match.group("version")
        if version in migrations:
            raise MigrationIntegrityError(
                f"duplicate migration version in comparison base: {version}"
            )
        try:
            raw = _git(root, "show", f"{resolved}:{repository_path}")
        except (OSError, subprocess.CalledProcessError) as error:
            raise MigrationIntegrityError(
                f"failed to read base migration: {repository_path}"
            ) from error
        migrations[version] = (
            Path(repository_path).name,
            _canonical_sha256(raw),
        )

    baseline_path = f"{MIGRATION_REPOSITORY_PATH}/checksum_baseline.json"
    try:
        baseline_raw = _git(root, "show", f"{resolved}:{baseline_path}")
    except subprocess.CalledProcessError:
        baseline_entries = None
        baseline_policy = None
    except OSError as error:
        raise MigrationIntegrityError(
            "failed to read checksum baseline from comparison base"
        ) from error
    else:
        try:
            payload = json.loads(baseline_raw.decode("utf-8-sig"))
            baseline_entries = payload["migrations"]
            baseline_policy = {
                "algorithm": payload["algorithm"],
                "legacy_md5_through": payload["legacy_md5_through"],
            }
        except (UnicodeDecodeError, json.JSONDecodeError, KeyError, TypeError) as error:
            raise MigrationIntegrityError(
                "invalid checksum baseline in comparison base"
            ) from error
        if not isinstance(baseline_entries, dict):
            raise MigrationIntegrityError(
                "base checksum baseline migrations must be an object"
            )
    return migrations, baseline_entries, baseline_policy


def load_current_policy() -> dict[str, str]:
    """Return top-level policy fields already validated by the runner."""
    try:
        payload = json.loads(CHECKSUM_BASELINE.read_text(encoding="utf-8"))
        return {
            "algorithm": payload["algorithm"],
            "legacy_md5_through": payload["legacy_md5_through"],
        }
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, KeyError, TypeError) as error:
        raise MigrationIntegrityError(
            "invalid current checksum baseline policy"
        ) from error


def validate_append_only_history(
    files: list[Path],
    entries: dict[str, dict],
    base_migrations: dict[str, tuple[str, str]],
    base_entries: dict[str, dict] | None,
    current_policy: dict[str, str] | None = None,
    base_policy: dict[str, str] | None = None,
) -> int:
    """Forbid deleting, renaming or changing any migration in the base."""
    current = {
        path.name.split("_", 1)[0]: (path.name, get_checksum(path))
        for path in files
    }
    for version, (base_name, base_checksum) in base_migrations.items():
        current_item = current.get(version)
        if current_item is None:
            raise MigrationIntegrityError(
                f"published migration was deleted: {version}"
            )
        current_name, current_checksum = current_item
        if current_name != base_name:
            raise MigrationIntegrityError(
                f"published migration was renamed: {base_name} -> {current_name}"
            )
        if current_checksum != base_checksum:
            raise MigrationIntegrityError(
                f"published migration was modified: {version}"
            )

    if base_entries is not None:
        if set(base_entries) != set(base_migrations):
            raise MigrationIntegrityError(
                "comparison base checksum entries differ from its migrations"
            )
        for version, base_entry in base_entries.items():
            if entries.get(version) != base_entry:
                raise MigrationIntegrityError(
                    f"published checksum baseline entry was modified: {version}"
                )
        if base_policy is None or current_policy != base_policy:
            raise MigrationIntegrityError(
                "published checksum baseline policy was modified"
            )

    new_versions = set(current) - set(base_migrations)
    legacy_keys = {
        "accepted_legacy_md5",
        "legacy_drift_md5",
        "legacy_final_state",
    }
    for version in new_versions:
        entry = entries[version]
        if legacy_keys.intersection(entry):
            raise MigrationIntegrityError(
                f"new migration cannot declare legacy checksum compatibility: {version}"
            )
    return len(base_migrations)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--base-ref",
        help=(
            "Git commit/ref to compare against; published migrations and "
            "baseline entries in that commit become immutable"
        ),
    )
    args = parser.parse_args(argv)
    files = get_migration_files()
    entries = load_checksum_baseline(files)
    current_policy = load_current_policy()
    frozen = 0
    if args.base_ref:
        base_migrations, base_entries, base_policy = load_base_history(
            ROOT, args.base_ref
        )
        frozen = validate_append_only_history(
            files,
            entries,
            base_migrations,
            base_entries,
            current_policy,
            base_policy,
        )
    print(
        f"Migration repository gate OK: {len(files)} current file(s), "
        f"{frozen} base file(s) immutable"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
