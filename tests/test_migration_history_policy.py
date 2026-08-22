from copy import deepcopy
from pathlib import Path

import pytest

from tools.check_migrations import validate_append_only_history
from wxsearch.migrations.run import MigrationIntegrityError, get_checksum


POLICY = {
    "algorithm": "sha256-canonical-lf-v1",
    "legacy_md5_through": "020",
}


def _migration(directory: Path, name: str, sql: str = "SELECT 1;\n") -> Path:
    path = directory / name
    path.write_text(sql, encoding="utf-8", newline="")
    return path


def _entry(path: Path) -> dict:
    return {"sha256": get_checksum(path)}


def test_published_sql_cannot_change_even_when_baseline_is_bootstrapped(
    tmp_path: Path,
):
    first = _migration(tmp_path, "001_one.sql")
    base_migrations = {"001": (first.name, get_checksum(first))}
    first.write_text("SELECT 2;\n", encoding="utf-8", newline="")

    with pytest.raises(MigrationIntegrityError, match="was modified"):
        validate_append_only_history(
            [first], {"001": _entry(first)}, base_migrations, None
        )


def test_published_baseline_entry_cannot_change(tmp_path: Path):
    first = _migration(tmp_path, "001_one.sql")
    entries = {"001": _entry(first)}
    base_entries = deepcopy(entries)
    entries["001"]["accepted_legacy_md5"] = ["0" * 32]

    with pytest.raises(MigrationIntegrityError, match="entry was modified"):
        validate_append_only_history(
            [first],
            entries,
            {"001": (first.name, get_checksum(first))},
            base_entries,
            deepcopy(POLICY),
            deepcopy(POLICY),
        )


def test_new_migration_may_only_append_without_legacy_compatibility(
    tmp_path: Path,
):
    first = _migration(tmp_path, "001_one.sql")
    second = _migration(tmp_path, "002_two.sql")
    entries = {"001": _entry(first), "002": _entry(second)}

    assert validate_append_only_history(
        [first, second],
        entries,
        {"001": (first.name, get_checksum(first))},
        {"001": deepcopy(entries["001"])},
        deepcopy(POLICY),
        deepcopy(POLICY),
    ) == 1

    entries["002"]["accepted_legacy_md5"] = []
    with pytest.raises(MigrationIntegrityError, match="cannot declare legacy"):
        validate_append_only_history(
            [first, second],
            entries,
            {"001": (first.name, get_checksum(first))},
            {"001": deepcopy(entries["001"])},
            deepcopy(POLICY),
            deepcopy(POLICY),
        )


@pytest.mark.parametrize("mode", ["delete", "rename"])
def test_published_migration_cannot_be_deleted_or_renamed(
    tmp_path: Path, mode: str
):
    first = _migration(tmp_path, "001_one.sql")
    base_migrations = {"001": (first.name, get_checksum(first))}
    entries = {"001": _entry(first)}

    if mode == "delete":
        files = []
    else:
        renamed = tmp_path / "001_renamed.sql"
        first.rename(renamed)
        files = [renamed]
        entries = {"001": _entry(renamed)}

    with pytest.raises(MigrationIntegrityError, match=f"was {mode}d"):
        validate_append_only_history(
            files,
            entries,
            base_migrations,
            deepcopy(entries),
            deepcopy(POLICY),
            deepcopy(POLICY),
        )


def test_legacy_cutoff_policy_cannot_be_increased(tmp_path: Path):
    first = _migration(tmp_path, "001_one.sql")
    entries = {"001": _entry(first)}
    changed_policy = {**POLICY, "legacy_md5_through": "021"}

    with pytest.raises(MigrationIntegrityError, match="policy was modified"):
        validate_append_only_history(
            [first],
            entries,
            {"001": (first.name, get_checksum(first))},
            deepcopy(entries),
            changed_policy,
            deepcopy(POLICY),
        )
