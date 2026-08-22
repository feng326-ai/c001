"""Strict, read-only contract tests for TENANT-MAPPING-003."""

from __future__ import annotations

import copy
import json
import re
import uuid
from pathlib import Path

import pytest

from tools.check_secrets import scan_file
from wxsearch.tenant_mapping import (
    TenantMappingError,
    dry_run_database,
    inventory_database,
    load_manifest,
    main,
    manifest_summary,
    validate_manifest_data,
)


ROOT = Path(__file__).resolve().parents[1]
EXAMPLE_MANIFEST = ROOT / "docs" / "租户映射清单.example.json"
HEX_64 = re.compile(r"^[0-9a-f]{64}$")


def _stable_uuid(namespace: int, sequence: int) -> str:
    value = (namespace << 96) | sequence
    return str(uuid.UUID(int=value, version=4))


def _example_data() -> dict:
    return json.loads(EXAMPLE_MANIFEST.read_text(encoding="utf-8"))


def _production_data() -> dict:
    tenants = []
    next_user_id = 1000
    next_membership_id = 1
    for tenant_number in range(1, 4):
        team_id = 100 + tenant_number
        memberships = []
        for member_number in range(9):
            next_user_id += 1
            tenant_role = "resource_reviewer" if member_number == 0 else "sales"
            memberships.append(
                {
                    "membership_id": _stable_uuid(0x30000000, next_membership_id),
                    "legacy_user_id_observed": next_user_id,
                    "user_public_id": _stable_uuid(0x40000000, next_user_id),
                    "legacy_team_id_observed": team_id,
                    "legacy_role_observed": (
                        "admin" if tenant_role == "resource_reviewer" else "member"
                    ),
                    "tenant_role": tenant_role,
                    "membership_status": "active",
                    "mapping_action": "create",
                    "reason_code": "company_roster_confirmed",
                    "company_approval_reference": (
                        f"private-company-{tenant_number}-member-{member_number}"
                    ),
                }
            )
            next_membership_id += 1
        tenants.append(
            {
                "tenant_id": _stable_uuid(0x20000000, tenant_number),
                "tenant_code": f"company-{tenant_number}",
                "tenant_name": f"Private company {tenant_number}",
                "default_visibility_policy": "shared_competition",
                "initial_status": "disabled",
                "observed_legacy_team_id": team_id,
                "expected_sales_count": 8,
                "expected_reviewer_count": 1,
                "company_approval_reference": f"private-company-{tenant_number}",
                "memberships": memberships,
            }
        )
    return {
        "schema_version": 1,
        "batch_id": _stable_uuid(0x10000000, 1),
        "target_environment": "production",
        "source_snapshot_at": "2026-08-23T12:00:00+08:00",
        "source_users_digest_sha256": "a" * 64,
        "approval_reference": "private-production-approval",
        "policy": {
            "expected_tenant_count": 3,
            "expected_sales_per_tenant": 8,
            "expected_reviewers_per_tenant": 1,
            "allow_cross_tenant_users": False,
        },
        "tenants": tenants,
        "excluded_users": [
            {
                "legacy_user_id_observed": 1,
                "user_public_id": _stable_uuid(0x40000000, 9999),
                "legacy_team_id_observed": None,
                "legacy_role_observed": "super",
                "classification": "platform_only",
                "reason_code": "platform_administration_only",
                "approval_reference": "private-platform-approval",
            }
        ],
    }


def _set_path(data: dict, path: tuple, value) -> None:
    current = data
    for item in path[:-1]:
        current = current[item]
    current[path[-1]] = value


def test_synthetic_repository_example_is_strictly_valid_and_redacted():
    data = _example_data()
    manifest = load_manifest(EXAMPLE_MANIFEST, repo_root=ROOT)
    summary = manifest_summary(manifest)
    rendered = json.dumps(summary, ensure_ascii=False, sort_keys=True)

    assert data["target_environment"] == "example"
    assert data["tenants"][0]["tenant_name"] == "合成示例公司甲"
    assert data["approval_reference"] == "synthetic-example-only"
    assert scan_file(EXAMPLE_MANIFEST) == []
    assert summary["tenant_count"] == 1
    assert summary["membership_count"] == 2
    assert summary["sales_count"] == 1
    assert summary["reviewer_count"] == 1
    assert summary["excluded_user_count"] == 1
    assert "合成示例公司甲" not in rendered
    assert "synthetic-company-approval" not in rendered
    assert not any(
        forbidden in json.dumps(data, ensure_ascii=False).lower()
        for forbidden in ('"username"', '"password"', '"email"', '"phone"')
    )


def test_production_v1_requires_exact_three_by_nine_roster():
    manifest = validate_manifest_data(_production_data())
    summary = manifest_summary(manifest)

    assert summary["tenant_count"] == 3
    assert summary["membership_count"] == 27
    assert summary["sales_count"] == 24
    assert summary["reviewer_count"] == 3
    assert summary["excluded_user_count"] == 1


@pytest.mark.parametrize(
    ("path", "value"),
    (
        (("policy", "expected_tenant_count"), 2),
        (("policy", "expected_sales_per_tenant"), 7),
        (("policy", "expected_reviewers_per_tenant"), 2),
        (("policy", "allow_cross_tenant_users"), True),
        (("tenants", 0, "expected_sales_count"), 7),
        (("tenants", 0, "expected_reviewer_count"), 2),
        (("tenants", 0, "initial_status"), "active"),
        (
            ("tenants", 0, "default_visibility_policy"),
            "tenant_private",
        ),
    ),
)
def test_production_policy_or_roster_drift_fails_closed(path, value):
    data = _production_data()
    _set_path(data, path, value)
    with pytest.raises(TenantMappingError):
        validate_manifest_data(data)


@pytest.mark.parametrize(
    "location",
    ("top", "policy", "tenant", "membership", "excluded"),
)
def test_unknown_fields_are_rejected_without_echoing_values(location):
    data = _production_data()
    canary = f"private-canary-{location}-must-not-leak"
    target = {
        "top": data,
        "policy": data["policy"],
        "tenant": data["tenants"][0],
        "membership": data["tenants"][0]["memberships"][0],
        "excluded": data["excluded_users"][0],
    }[location]
    target["unexpected_secret"] = canary

    with pytest.raises(TenantMappingError) as exc_info:
        validate_manifest_data(data)
    assert canary not in str(exc_info.value)


@pytest.mark.parametrize(
    ("path", "value"),
    (
        (("batch_id",), "{10000000-0000-4000-8000-000000000001}"),
        (
            ("tenants", 0, "tenant_id"),
            "AAAAAAAA-AAAA-4AAA-8AAA-AAAAAAAAAAAA",
        ),
        (
            ("tenants", 0, "memberships", 0, "membership_id"),
            "30000000000040008000000000000001",
        ),
        (
            ("tenants", 0, "memberships", 0, "user_public_id"),
            "{40000000-0000-4000-8000-000000000001}",
        ),
        (("tenants", 0, "tenant_code"), " Company_1 "),
        (("source_snapshot_at",), "2026-08-23T12:00:00"),
        (("source_users_digest_sha256",), "A" * 64),
    ),
)
def test_noncanonical_identifiers_and_timestamp_are_rejected(path, value):
    data = _production_data()
    _set_path(data, path, value)
    with pytest.raises(TenantMappingError):
        validate_manifest_data(data)


def test_duplicate_json_keys_are_rejected_at_every_depth(tmp_path: Path):
    data = _example_data()
    raw = json.dumps(data, ensure_ascii=False)
    raw = raw.replace(
        '"schema_version": 1,',
        '"schema_version": 1, "schema_version": 1,',
        1,
    ).replace(
        '"tenant_role": "resource_reviewer",',
        '"tenant_role": "resource_reviewer", "tenant_role": "sales",',
        1,
    )
    path = tmp_path / "duplicate-keys.json"
    path.write_text(raw, encoding="utf-8")

    with pytest.raises(TenantMappingError, match="duplicate"):
        load_manifest(path, repo_root=tmp_path / "different-repository")


@pytest.mark.parametrize(
    "mutation",
    (
        "membership_id",
        "legacy_user_id",
        "public_user_id",
        "cross_tenant_user",
        "tenant_id",
        "tenant_code",
        "member_and_excluded",
    ),
)
def test_duplicate_or_cross_tenant_identities_fail_closed(mutation):
    data = _production_data()
    first = data["tenants"][0]["memberships"][0]
    second = data["tenants"][1]["memberships"][0]
    if mutation == "membership_id":
        second["membership_id"] = first["membership_id"]
    elif mutation == "legacy_user_id":
        second["legacy_user_id_observed"] = first["legacy_user_id_observed"]
    elif mutation in {"public_user_id", "cross_tenant_user"}:
        second["user_public_id"] = first["user_public_id"]
    elif mutation == "tenant_id":
        data["tenants"][1]["tenant_id"] = data["tenants"][0]["tenant_id"]
    elif mutation == "tenant_code":
        data["tenants"][1]["tenant_code"] = data["tenants"][0]["tenant_code"]
    else:
        data["excluded_users"][0]["legacy_user_id_observed"] = first[
            "legacy_user_id_observed"
        ]

    with pytest.raises(TenantMappingError):
        validate_manifest_data(data)


@pytest.mark.parametrize(
    "bad_role",
    ("admin", "member", "super", "tenant_admin", "readonly_manager", "Sales", " sales "),
)
def test_production_memberships_only_allow_reviewer_or_sales(bad_role):
    data = _production_data()
    data["tenants"][0]["memberships"][0]["tenant_role"] = bad_role
    with pytest.raises(TenantMappingError):
        validate_manifest_data(data)


def test_nonproduction_manifest_accepts_the_full_frozen_role_whitelist():
    data = _example_data()
    data["target_environment"] = "qa"
    tenant = data["tenants"][0]
    for sequence, tenant_role in enumerate(
        ("tenant_admin", "readonly_manager"),
        start=10,
    ):
        tenant["memberships"].append(
            {
                "membership_id": _stable_uuid(0x30000000, sequence),
                "legacy_user_id_observed": 1000 + sequence,
                "user_public_id": _stable_uuid(0x40000000, 1000 + sequence),
                "legacy_team_id_observed": tenant["observed_legacy_team_id"],
                "legacy_role_observed": "admin",
                "tenant_role": tenant_role,
                "membership_status": "active",
                "mapping_action": "create",
                "reason_code": "company_roster_confirmed",
                "company_approval_reference": "synthetic-management-approval",
            }
        )

    manifest = validate_manifest_data(data)
    assert {
        member.tenant_role for member in manifest.tenants[0].memberships
    } == {
        "tenant_admin",
        "resource_reviewer",
        "sales",
        "readonly_manager",
    }


def test_platform_super_must_be_excluded_as_platform_only():
    data = _production_data()
    super_user = data["excluded_users"].pop()
    super_user.update(
        {
            "membership_id": _stable_uuid(0x30000000, 9999),
            "tenant_role": "sales",
            "membership_status": "active",
            "mapping_action": "create",
            "company_approval_reference": "private-invalid-super-membership",
        }
    )
    super_user.pop("classification")
    super_user.pop("approval_reference")
    data["tenants"][0]["memberships"].append(super_user)
    data["tenants"][0]["expected_sales_count"] = 9
    with pytest.raises(TenantMappingError):
        validate_manifest_data(data)

    data = _production_data()
    data["excluded_users"][0]["classification"] = "legacy_only"
    with pytest.raises(TenantMappingError):
        validate_manifest_data(data)


def test_production_manifest_must_be_external_regular_file(tmp_path: Path):
    repository = tmp_path / "repository"
    private = tmp_path / "private"
    repository.mkdir()
    private.mkdir()
    raw = json.dumps(_production_data(), ensure_ascii=False)

    inside = repository / "tenant-mapping.private.production.json"
    inside.write_text(raw, encoding="utf-8")
    with pytest.raises(TenantMappingError, match="repository"):
        load_manifest(inside, repo_root=repository)

    outside = private / "tenant-mapping.private.production.json"
    outside.write_text(raw, encoding="utf-8")
    loaded = load_manifest(outside, repo_root=repository)
    assert manifest_summary(loaded)["membership_count"] == 27


def test_production_manifest_rejects_a_symbolic_link(tmp_path: Path):
    repository = tmp_path / "repository"
    private = tmp_path / "private"
    repository.mkdir()
    private.mkdir()
    outside = private / "tenant-mapping.private.production.json"
    outside.write_text(
        json.dumps(_production_data(), ensure_ascii=False),
        encoding="utf-8",
    )
    link = private / "tenant-mapping.private.production-link.json"
    try:
        link.symlink_to(outside)
    except OSError as error:
        pytest.skip(f"symlink creation is unavailable: {error}")
    with pytest.raises(TenantMappingError, match="symlink"):
        load_manifest(link, repo_root=repository)


def _cli_exit_code(argv: list[str]) -> int:
    try:
        result = main(argv)
    except SystemExit as error:
        return int(error.code or 0)
    return int(result)


def test_cli_has_no_apply_or_dsn_argument_and_never_echoes_dsn(capsys):
    assert _cli_exit_code(["apply"]) != 0
    assert _cli_exit_code(["dry-run", "--apply"]) != 0

    secret_canary = "".join(("actual", "-", "secret"))
    dsn_canary = "".join(
        ("postgresql://", "mapping:", secret_canary, "@db.invalid/private")
    )
    assert _cli_exit_code(["inventory", "--database-url", dsn_canary]) != 0
    captured = capsys.readouterr()
    assert secret_canary not in captured.out
    assert secret_canary not in captured.err


class HybridRow:
    def __init__(self, **values):
        self.values = values

    def __getitem__(self, item):
        if isinstance(item, slice):
            return tuple(self.values.values())[item]
        if isinstance(item, int):
            return tuple(self.values.values())[item]
        return self.values[item]

    def __iter__(self):
        return iter(self.values.values())

    def __len__(self):
        return len(self.values)


class GuardCursor:
    WRITE_SQL = re.compile(
        r"\b(insert|update|delete|merge|create|alter|drop|truncate|copy|call|do|"
        r"grant|revoke|vacuum|analyze|refresh)\b",
        re.IGNORECASE,
    )

    def __init__(self, connection):
        self.connection = connection
        self.rows = []
        self.description = []

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def execute(self, statement, params=None):
        sql = " ".join(str(statement).split())
        self.connection.statements.append((sql, params))
        if self.WRITE_SQL.search(sql):
            raise AssertionError(f"dry-run attempted write SQL: {sql.split()[0]}")
        lowered = sql.lower()
        if "count(*)" in lowered and re.search(
            r"\bfrom\s+(?:public\.)?teams\b", lowered
        ):
            self.rows = [HybridRow(count=len(self.connection.teams))]
        elif "schema_migrations" in lowered and "to_regclass" not in lowered:
            self.rows = [HybridRow(version="021"), HybridRow(version="022")]
        elif "to_regclass" in lowered or "to_regprocedure" in lowered:
            self.rows = [
                HybridRow(
                    schema_migrations=True,
                    teams=True,
                    users=True,
                    tenants=True,
                    tenant_memberships=True,
                    discovery=True,
                    users_public_id=True,
                )
            ]
        elif "information_schema.columns" in lowered:
            self.rows = [HybridRow(column_name=name) for name in self.connection.columns]
        elif re.search(r"\bfrom\s+(?:public\.)?tenant_memberships\b", lowered):
            self.rows = [HybridRow(**row) for row in self.connection.memberships]
        elif re.search(r"\bfrom\s+(?:public\.)?tenants\b", lowered):
            self.rows = [HybridRow(**row) for row in self.connection.tenants]
        elif re.search(r"\bfrom\s+(?:public\.)?users\b", lowered):
            self.rows = [HybridRow(**row) for row in self.connection.users]
        elif re.search(r"\bfrom\s+(?:public\.)?teams\b", lowered):
            self.rows = [HybridRow(**row) for row in self.connection.teams]
        elif "transaction_read_only" in lowered:
            self.rows = [HybridRow(transaction_read_only="on")]
        elif "statement_timeout" in lowered and lowered.startswith(("show", "select")):
            self.rows = [HybridRow(statement_timeout="5s")]
        elif lowered.startswith("select"):
            self.rows = [HybridRow(ok=True)]
        else:
            self.rows = []
        names = list(self.rows[0].values) if self.rows else []
        self.description = [(name, None, None, None, None, None, None) for name in names]

    def fetchall(self):
        return list(self.rows)

    def fetchone(self):
        return self.rows[0] if self.rows else None

    def close(self):
        return None


class GuardConnection:
    def __init__(self, *, users=None, teams=None, tenants=None, memberships=None):
        self.users = list(users or [])
        self.teams = list(teams or [])
        self.tenants = list(tenants or [])
        self.memberships = list(memberships or [])
        self.columns = {
            "id",
            "public_id",
            "username",
            "enabled",
            "role",
            "team_id",
            "code",
            "name",
            "status",
            "default_visibility_policy",
            "tenant_id",
            "user_id",
        }
        self.statements = []
        self.set_session_calls = []
        self.rollback_count = 0
        self.commit_count = 0
        self.autocommit = False

    def cursor(self, *_args, **_kwargs):
        return GuardCursor(self)

    def set_session(self, *args, **kwargs):
        self.set_session_calls.append((args, kwargs))

    def rollback(self):
        self.rollback_count += 1

    def commit(self):
        self.commit_count += 1


def _fake_users(*, disabled_member=False, extra_enabled=False):
    data = _example_data()
    rows = []
    for membership in data["tenants"][0]["memberships"]:
        rows.append(
            {
                "id": membership["legacy_user_id_observed"],
                "public_id": membership["user_public_id"],
                "username": f"private-user-{membership['legacy_user_id_observed']}",
                "enabled": not disabled_member,
                "role": membership["legacy_role_observed"],
                "team_id": membership["legacy_team_id_observed"],
            }
        )
    excluded = data["excluded_users"][0]
    rows.append(
        {
            "id": excluded["legacy_user_id_observed"],
            "public_id": excluded["user_public_id"],
            "username": "private-platform-user",
            "enabled": True,
            "role": "super",
            "team_id": excluded["legacy_team_id_observed"],
        }
    )
    if extra_enabled:
        rows.append(
            {
                "id": 9090,
                "public_id": _stable_uuid(0x40000000, 9090),
                "username": "private-omitted-user",
                "enabled": True,
                "role": "member",
                "team_id": 101,
            }
        )
    return rows


def _guard_connection(*, disabled_member=False, extra_enabled=False):
    return GuardConnection(
        users=_fake_users(
            disabled_member=disabled_member,
            extra_enabled=extra_enabled,
        ),
        teams=[{"id": 101, "name": "Private legacy team"}],
    )


def _assert_read_only_protocol(connection: GuardConnection):
    statements = [sql.lower() for sql, _params in connection.statements]
    used_read_only_sql = any("transaction read only" in sql for sql in statements)
    used_read_only_api = any(
        call_kwargs.get("readonly") is True
        for _call_args, call_kwargs in connection.set_session_calls
    )
    assert used_read_only_sql or used_read_only_api
    assert any(sql.startswith("set local statement_timeout") for sql in statements)
    assert connection.rollback_count == 1
    assert connection.commit_count == 0


def _manifest_for_inventory(data: dict, inventory: dict):
    updated = copy.deepcopy(data)
    updated["target_environment"] = "qa"
    updated["source_users_digest_sha256"] = inventory[
        "source_users_digest_sha256"
    ]
    return validate_manifest_data(updated)


def test_inventory_and_dry_run_enforce_read_only_timeout_and_rollback():
    inventory_connection = _guard_connection()
    inventory = inventory_database(inventory_connection)
    _assert_read_only_protocol(inventory_connection)
    assert HEX_64.fullmatch(inventory["source_users_digest_sha256"])
    assert inventory["migration_head"] == "022"
    assert inventory["mapping_ready"] is True

    manifest = _manifest_for_inventory(_example_data(), inventory)
    dry_run_connection = _guard_connection()
    result = dry_run_database(dry_run_connection, manifest)
    _assert_read_only_protocol(dry_run_connection)
    assert result["status"] == "ready"
    assert result["issue_count"] == 0
    assert result["actions"] == {
        "tenant_create": 1,
        "tenant_noop": 0,
        "membership_create": 2,
        "membership_noop": 0,
    }


def test_dry_run_blocks_keep_action_when_membership_does_not_exist():
    inventory_connection = _guard_connection()
    inventory = inventory_database(inventory_connection)
    data = _example_data()
    data["target_environment"] = "qa"
    data["source_users_digest_sha256"] = inventory[
        "source_users_digest_sha256"
    ]
    data["tenants"][0]["memberships"][0]["mapping_action"] = "keep"
    manifest = validate_manifest_data(data)
    dry_run_connection = _guard_connection()

    result = dry_run_database(dry_run_connection, manifest)

    _assert_read_only_protocol(dry_run_connection)
    assert result["status"] == "blocked"
    assert "membership_action_mismatch" in {
        issue["code"] for issue in result["issues"]
    }


@pytest.mark.parametrize(
    ("subject", "expected_code"),
    (
        ("mapped", "unexpected_existing_membership"),
        ("excluded", "excluded_user_has_membership"),
    ),
)
def test_dry_run_blocks_cross_company_or_excluded_existing_membership(
    subject, expected_code
):
    inventory_connection = _guard_connection()
    inventory = inventory_database(inventory_connection)
    data = _example_data()
    data["target_environment"] = "qa"
    data["source_users_digest_sha256"] = inventory[
        "source_users_digest_sha256"
    ]
    manifest = validate_manifest_data(data)
    public_id = (
        data["tenants"][0]["memberships"][0]["user_public_id"]
        if subject == "mapped"
        else data["excluded_users"][0]["user_public_id"]
    )
    dry_run_connection = _guard_connection()
    dry_run_connection.memberships = [
        {
            "id": _stable_uuid(0x50000000, 1),
            "tenant_id": _stable_uuid(0x60000000, 1),
            "user_id": public_id,
            "role": "sales",
            "status": "active",
        }
    ]

    result = dry_run_database(dry_run_connection, manifest)

    _assert_read_only_protocol(dry_run_connection)
    assert result["status"] == "blocked"
    assert expected_code in {issue["code"] for issue in result["issues"]}


@pytest.mark.parametrize("scenario", ("unknown", "disabled", "omitted"))
def test_dry_run_rejects_unknown_disabled_or_omitted_enabled_users(scenario):
    disabled = scenario == "disabled"
    omitted = scenario == "omitted"
    inventory_connection = _guard_connection(
        disabled_member=disabled,
        extra_enabled=omitted,
    )
    inventory = inventory_database(inventory_connection)
    manifest_data = _example_data()
    manifest_data["target_environment"] = "qa"
    manifest_data["source_users_digest_sha256"] = inventory[
        "source_users_digest_sha256"
    ]
    if scenario == "unknown":
        manifest_data["tenants"][0]["memberships"][0][
            "legacy_user_id_observed"
        ] = 999999
    manifest = validate_manifest_data(manifest_data)
    dry_run_connection = _guard_connection(
        disabled_member=disabled,
        extra_enabled=omitted,
    )

    result = dry_run_database(dry_run_connection, manifest)
    _assert_read_only_protocol(dry_run_connection)
    assert result["status"] == "blocked"
    assert result["issue_count"] >= 1
    expected_code = {
        "unknown": "legacy_user_unknown",
        "disabled": "legacy_user_disabled",
        "omitted": "enabled_user_unclassified",
    }[scenario]
    assert expected_code in {issue["code"] for issue in result["issues"]}
