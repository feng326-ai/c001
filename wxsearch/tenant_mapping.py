#!/usr/bin/env python3
"""Strict, read-only planning for legacy user to tenant mappings.

This module deliberately has no apply path.  A production manifest is private
environment data and must live outside the repository.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import uuid
from dataclasses import dataclass, field, replace
from datetime import datetime
from pathlib import Path
from typing import Any, Mapping, Sequence


SCHEMA_VERSION = 1
MAX_MANIFEST_BYTES = 1024 * 1024
STATEMENT_TIMEOUT_MS = 5000
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
CODE_RE = re.compile(r"^[a-z][a-z0-9-]{1,62}$")
REFERENCE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{2,127}$")
REASON_RE = re.compile(r"^[a-z][a-z0-9_]{2,62}$")
TARGET_ENVIRONMENTS = {"production", "staging", "qa", "example"}
LEGACY_ROLES = {"admin", "member", "super"}
TENANT_ROLES = {
    "tenant_admin",
    "resource_reviewer",
    "sales",
    "readonly_manager",
}
CLASSIFICATIONS = {"platform_only", "legacy_only", "suspended"}


class TenantMappingError(RuntimeError):
    """A mapping input or database state is unsafe."""


@dataclass(frozen=True)
class MappingPolicy:
    expected_tenant_count: int
    expected_sales_per_tenant: int
    expected_reviewers_per_tenant: int
    allow_cross_tenant_users: bool


@dataclass(frozen=True)
class MappingMembership:
    membership_id: uuid.UUID
    legacy_user_id_observed: int
    user_public_id: uuid.UUID
    legacy_team_id_observed: int
    legacy_role_observed: str
    tenant_role: str
    membership_status: str
    mapping_action: str
    reason_code: str
    company_approval_reference: str


@dataclass(frozen=True)
class TenantEntry:
    tenant_id: uuid.UUID
    tenant_code: str
    tenant_name: str = field(repr=False)
    default_visibility_policy: str
    initial_status: str
    observed_legacy_team_id: int
    expected_sales_count: int
    expected_reviewer_count: int
    company_approval_reference: str
    memberships: tuple[MappingMembership, ...]


@dataclass(frozen=True)
class ExcludedUser:
    legacy_user_id_observed: int
    user_public_id: uuid.UUID
    legacy_team_id_observed: int | None
    legacy_role_observed: str
    classification: str
    reason_code: str
    approval_reference: str


@dataclass(frozen=True)
class MappingManifest:
    schema_version: int
    batch_id: uuid.UUID
    target_environment: str
    source_snapshot_at: datetime
    source_users_digest_sha256: str
    approval_reference: str
    policy: MappingPolicy
    tenants: tuple[TenantEntry, ...]
    excluded_users: tuple[ExcludedUser, ...]
    manifest_sha256: str = ""


def _strict_object(pairs: Sequence[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise TenantMappingError("manifest contains a duplicate JSON key")
        result[key] = value
    return result


def _expect_object(value: Any, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise TenantMappingError(f"{label} must be an object")
    return value


def _expect_keys(
    value: Mapping[str, Any], required: set[str], label: str
) -> None:
    actual = set(value)
    if actual != required:
        raise TenantMappingError(f"{label} fields invalid")


def _expect_int(value: Any, label: str, *, minimum: int = 1) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise TenantMappingError(f"{label} must be an integer >= {minimum}")
    return value


def _expect_bool(value: Any, label: str) -> bool:
    if not isinstance(value, bool):
        raise TenantMappingError(f"{label} must be a boolean")
    return value


def _expect_choice(value: Any, choices: set[str], label: str) -> str:
    if not isinstance(value, str) or value not in choices:
        raise TenantMappingError(f"{label} is not an allowed value")
    return value


def _expect_pattern(value: Any, pattern: re.Pattern[str], label: str) -> str:
    if not isinstance(value, str) or not pattern.fullmatch(value):
        raise TenantMappingError(f"{label} is not in canonical form")
    return value


def _expect_private_text(value: Any, label: str) -> str:
    if not isinstance(value, str) or value != value.strip():
        raise TenantMappingError(f"{label} must be a trimmed string")
    if not value or len(value) > 200 or any(ord(char) < 32 for char in value):
        raise TenantMappingError(f"{label} is invalid")
    return value


def _expect_uuid(value: Any, label: str) -> uuid.UUID:
    if not isinstance(value, str):
        raise TenantMappingError(f"{label} must be a canonical UUID")
    try:
        parsed = uuid.UUID(value)
    except (ValueError, AttributeError) as error:
        raise TenantMappingError(f"{label} must be a canonical UUID") from error
    if str(parsed) != value or parsed.version != 4:
        raise TenantMappingError(f"{label} must be a canonical UUIDv4")
    return parsed


def _expect_datetime(value: Any, label: str) -> datetime:
    if not isinstance(value, str):
        raise TenantMappingError(f"{label} must include an explicit timezone")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise TenantMappingError(f"{label} must include an explicit timezone") from error
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise TenantMappingError(f"{label} must include an explicit timezone")
    return parsed


def _parse_policy(raw: Any) -> MappingPolicy:
    value = _expect_object(raw, "policy")
    _expect_keys(
        value,
        {
            "expected_tenant_count",
            "expected_sales_per_tenant",
            "expected_reviewers_per_tenant",
            "allow_cross_tenant_users",
        },
        "policy",
    )
    return MappingPolicy(
        expected_tenant_count=_expect_int(
            value["expected_tenant_count"], "policy.expected_tenant_count"
        ),
        expected_sales_per_tenant=_expect_int(
            value["expected_sales_per_tenant"],
            "policy.expected_sales_per_tenant",
        ),
        expected_reviewers_per_tenant=_expect_int(
            value["expected_reviewers_per_tenant"],
            "policy.expected_reviewers_per_tenant",
        ),
        allow_cross_tenant_users=_expect_bool(
            value["allow_cross_tenant_users"],
            "policy.allow_cross_tenant_users",
        ),
    )


def _parse_membership(raw: Any, label: str) -> MappingMembership:
    value = _expect_object(raw, label)
    _expect_keys(
        value,
        {
            "membership_id",
            "legacy_user_id_observed",
            "user_public_id",
            "legacy_team_id_observed",
            "legacy_role_observed",
            "tenant_role",
            "membership_status",
            "mapping_action",
            "reason_code",
            "company_approval_reference",
        },
        label,
    )
    legacy_role = _expect_choice(
        value["legacy_role_observed"], LEGACY_ROLES, f"{label}.legacy_role_observed"
    )
    if legacy_role == "super":
        raise TenantMappingError(f"{label} cannot map a platform super")
    return MappingMembership(
        membership_id=_expect_uuid(value["membership_id"], f"{label}.membership_id"),
        legacy_user_id_observed=_expect_int(
            value["legacy_user_id_observed"], f"{label}.legacy_user_id_observed"
        ),
        user_public_id=_expect_uuid(
            value["user_public_id"], f"{label}.user_public_id"
        ),
        legacy_team_id_observed=_expect_int(
            value["legacy_team_id_observed"], f"{label}.legacy_team_id_observed"
        ),
        legacy_role_observed=legacy_role,
        tenant_role=_expect_choice(
            value["tenant_role"], TENANT_ROLES, f"{label}.tenant_role"
        ),
        membership_status=_expect_choice(
            value["membership_status"], {"active"}, f"{label}.membership_status"
        ),
        mapping_action=_expect_choice(
            value["mapping_action"], {"create", "keep"}, f"{label}.mapping_action"
        ),
        reason_code=_expect_pattern(
            value["reason_code"], REASON_RE, f"{label}.reason_code"
        ),
        company_approval_reference=_expect_pattern(
            value["company_approval_reference"],
            REFERENCE_RE,
            f"{label}.company_approval_reference",
        ),
    )


def _parse_tenant(raw: Any, index: int) -> TenantEntry:
    label = f"tenants[{index}]"
    value = _expect_object(raw, label)
    _expect_keys(
        value,
        {
            "tenant_id",
            "tenant_code",
            "tenant_name",
            "default_visibility_policy",
            "initial_status",
            "observed_legacy_team_id",
            "expected_sales_count",
            "expected_reviewer_count",
            "company_approval_reference",
            "memberships",
        },
        label,
    )
    raw_memberships = value["memberships"]
    if not isinstance(raw_memberships, list) or not raw_memberships:
        raise TenantMappingError(f"{label}.memberships must be a non-empty list")
    memberships = tuple(
        _parse_membership(item, f"{label}.memberships[{member_index}]")
        for member_index, item in enumerate(raw_memberships)
    )
    tenant = TenantEntry(
        tenant_id=_expect_uuid(value["tenant_id"], f"{label}.tenant_id"),
        tenant_code=_expect_pattern(
            value["tenant_code"], CODE_RE, f"{label}.tenant_code"
        ),
        tenant_name=_expect_private_text(value["tenant_name"], f"{label}.tenant_name"),
        default_visibility_policy=_expect_choice(
            value["default_visibility_policy"],
            {"shared_competition"},
            f"{label}.default_visibility_policy",
        ),
        initial_status=_expect_choice(
            value["initial_status"], {"disabled"}, f"{label}.initial_status"
        ),
        observed_legacy_team_id=_expect_int(
            value["observed_legacy_team_id"], f"{label}.observed_legacy_team_id"
        ),
        expected_sales_count=_expect_int(
            value["expected_sales_count"], f"{label}.expected_sales_count"
        ),
        expected_reviewer_count=_expect_int(
            value["expected_reviewer_count"], f"{label}.expected_reviewer_count"
        ),
        company_approval_reference=_expect_pattern(
            value["company_approval_reference"],
            REFERENCE_RE,
            f"{label}.company_approval_reference",
        ),
        memberships=memberships,
    )
    sales = sum(member.tenant_role == "sales" for member in memberships)
    reviewers = sum(
        member.tenant_role == "resource_reviewer" for member in memberships
    )
    if sales != tenant.expected_sales_count or reviewers != tenant.expected_reviewer_count:
        raise TenantMappingError(f"{label} membership counts do not match expectations")
    if any(
        member.legacy_team_id_observed != tenant.observed_legacy_team_id
        for member in memberships
    ):
        raise TenantMappingError(f"{label} contains a cross-team membership")
    return tenant


def _parse_excluded(raw: Any, index: int) -> ExcludedUser:
    label = f"excluded_users[{index}]"
    value = _expect_object(raw, label)
    _expect_keys(
        value,
        {
            "legacy_user_id_observed",
            "user_public_id",
            "legacy_team_id_observed",
            "legacy_role_observed",
            "classification",
            "reason_code",
            "approval_reference",
        },
        label,
    )
    team_id = value["legacy_team_id_observed"]
    if team_id is not None:
        team_id = _expect_int(team_id, f"{label}.legacy_team_id_observed")
    legacy_role = _expect_choice(
        value["legacy_role_observed"], LEGACY_ROLES, f"{label}.legacy_role_observed"
    )
    classification = _expect_choice(
        value["classification"], CLASSIFICATIONS, f"{label}.classification"
    )
    if legacy_role == "super" and classification != "platform_only":
        raise TenantMappingError(f"{label} must classify super as platform_only")
    return ExcludedUser(
        legacy_user_id_observed=_expect_int(
            value["legacy_user_id_observed"], f"{label}.legacy_user_id_observed"
        ),
        user_public_id=_expect_uuid(
            value["user_public_id"], f"{label}.user_public_id"
        ),
        legacy_team_id_observed=team_id,
        legacy_role_observed=legacy_role,
        classification=classification,
        reason_code=_expect_pattern(
            value["reason_code"], REASON_RE, f"{label}.reason_code"
        ),
        approval_reference=_expect_pattern(
            value["approval_reference"], REFERENCE_RE, f"{label}.approval_reference"
        ),
    )


def validate_manifest_data(data: Any) -> MappingManifest:
    value = _expect_object(data, "manifest")
    _expect_keys(
        value,
        {
            "schema_version",
            "batch_id",
            "target_environment",
            "source_snapshot_at",
            "source_users_digest_sha256",
            "approval_reference",
            "policy",
            "tenants",
            "excluded_users",
        },
        "manifest",
    )
    if value["schema_version"] != SCHEMA_VERSION:
        raise TenantMappingError("unsupported manifest schema_version")
    target_environment = _expect_choice(
        value["target_environment"], TARGET_ENVIRONMENTS, "target_environment"
    )
    policy = _parse_policy(value["policy"])
    raw_tenants = value["tenants"]
    raw_excluded = value["excluded_users"]
    if not isinstance(raw_tenants, list) or not raw_tenants:
        raise TenantMappingError("tenants must be a non-empty list")
    if not isinstance(raw_excluded, list):
        raise TenantMappingError("excluded_users must be a list")
    tenants = tuple(_parse_tenant(item, index) for index, item in enumerate(raw_tenants))
    excluded = tuple(
        _parse_excluded(item, index) for index, item in enumerate(raw_excluded)
    )
    if len(tenants) != policy.expected_tenant_count:
        raise TenantMappingError("tenant count does not match policy")
    if target_environment == "production" and (
        policy.expected_tenant_count != 3
        or policy.expected_sales_per_tenant != 8
        or policy.expected_reviewers_per_tenant != 1
        or policy.allow_cross_tenant_users
    ):
        raise TenantMappingError("production v1 policy must be 3 tenants, 8 sales, 1 reviewer")
    for index, tenant in enumerate(tenants):
        if (
            tenant.expected_sales_count != policy.expected_sales_per_tenant
            or tenant.expected_reviewer_count != policy.expected_reviewers_per_tenant
        ):
            raise TenantMappingError(f"tenants[{index}] expectations differ from policy")
        if target_environment == "production" and len(tenant.memberships) != (
            tenant.expected_sales_count + tenant.expected_reviewer_count
        ):
            raise TenantMappingError(
                f"tenants[{index}] contains an unapproved production role"
            )

    def _unique(items: Sequence[Any], label: str) -> None:
        if len(items) != len(set(items)):
            raise TenantMappingError(f"manifest contains duplicate {label}")

    _unique([tenant.tenant_id for tenant in tenants], "tenant_id")
    _unique([tenant.tenant_code for tenant in tenants], "tenant_code")
    _unique([tenant.observed_legacy_team_id for tenant in tenants], "legacy team")
    memberships = [member for tenant in tenants for member in tenant.memberships]
    _unique([member.membership_id for member in memberships], "membership_id")
    for index, tenant in enumerate(tenants):
        _unique(
            [member.legacy_user_id_observed for member in tenant.memberships],
            f"legacy user in tenants[{index}]",
        )
        _unique(
            [member.user_public_id for member in tenant.memberships],
            f"public user in tenants[{index}]",
        )
    member_legacy_ids = [member.legacy_user_id_observed for member in memberships]
    member_public_ids = [member.user_public_id for member in memberships]
    if not policy.allow_cross_tenant_users:
        _unique(member_legacy_ids, "mapped legacy user")
        _unique(member_public_ids, "mapped public user")
    excluded_legacy_ids = [item.legacy_user_id_observed for item in excluded]
    excluded_public_ids = [item.user_public_id for item in excluded]
    _unique(excluded_legacy_ids, "excluded legacy user")
    _unique(excluded_public_ids, "excluded public user")
    if set(member_legacy_ids) & set(excluded_legacy_ids):
        raise TenantMappingError("a legacy user is both mapped and excluded")
    if set(member_public_ids) & set(excluded_public_ids):
        raise TenantMappingError("a public user is both mapped and excluded")
    return MappingManifest(
        schema_version=SCHEMA_VERSION,
        batch_id=_expect_uuid(value["batch_id"], "batch_id"),
        target_environment=target_environment,
        source_snapshot_at=_expect_datetime(
            value["source_snapshot_at"], "source_snapshot_at"
        ),
        source_users_digest_sha256=_expect_pattern(
            value["source_users_digest_sha256"], SHA256_RE, "source_users_digest_sha256"
        ),
        approval_reference=_expect_pattern(
            value["approval_reference"], REFERENCE_RE, "approval_reference"
        ),
        policy=policy,
        tenants=tenants,
        excluded_users=excluded,
    )


def _inside(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


def load_manifest(
    path: str | os.PathLike[str], repo_root: str | os.PathLike[str] | None = None
) -> MappingManifest:
    candidate = Path(path)
    if not candidate.is_file():
        raise TenantMappingError("manifest path must be a regular file")
    if candidate.stat().st_size > MAX_MANIFEST_BYTES:
        raise TenantMappingError("manifest exceeds the size limit")
    was_symlink = candidate.is_symlink()
    try:
        raw = candidate.read_bytes()
        text = raw.decode("utf-8")
        data = json.loads(text, object_pairs_hook=_strict_object)
    except UnicodeDecodeError as error:
        raise TenantMappingError("manifest must be UTF-8 JSON") from error
    except json.JSONDecodeError as error:
        raise TenantMappingError("manifest is not valid JSON") from error
    manifest = validate_manifest_data(data)
    if manifest.target_environment == "production":
        root = (
            Path(repo_root).resolve()
            if repo_root is not None
            else Path(__file__).resolve().parents[1]
        )
        resolved = candidate.resolve()
        if was_symlink or _inside(resolved, root):
            raise TenantMappingError(
                "production manifest must be a non-symlink file outside the repository"
            )
    return replace(manifest, manifest_sha256=hashlib.sha256(raw).hexdigest())


def manifest_summary(manifest: MappingManifest) -> dict[str, Any]:
    memberships = [member for tenant in manifest.tenants for member in tenant.memberships]
    return {
        "status": "valid",
        "schema_version": manifest.schema_version,
        "target_environment": manifest.target_environment,
        "manifest_sha256": manifest.manifest_sha256,
        "tenant_count": len(manifest.tenants),
        "membership_count": len(memberships),
        "sales_count": sum(member.tenant_role == "sales" for member in memberships),
        "reviewer_count": sum(
            member.tenant_role == "resource_reviewer" for member in memberships
        ),
        "excluded_user_count": len(manifest.excluded_users),
    }


def _fingerprint(value: Any) -> str:
    return hashlib.sha256(str(value).encode("utf-8")).hexdigest()[:12]


def _users_digest(rows: Sequence[Sequence[Any]]) -> str:
    canonical = [
        {
            "id": int(row[0]),
            "public_id": "" if row[1] is None else str(row[1]),
            "username": str(row[2]),
            "enabled": bool(row[3]),
            "role": str(row[4]),
            "team_id": None if row[5] is None else int(row[5]),
        }
        for row in sorted(rows, key=lambda item: int(item[0]))
    ]
    payload = json.dumps(
        canonical, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _prepare_read_only(connection):
    if hasattr(connection, "set_session"):
        connection.set_session(readonly=True, autocommit=False)
    cursor = connection.cursor()
    cursor.execute(f"SET LOCAL statement_timeout = '{STATEMENT_TIMEOUT_MS}ms'")
    cursor.execute("SHOW transaction_read_only")
    row = cursor.fetchone()
    if not row or str(row[0]).lower() != "on":
        raise TenantMappingError("database transaction is not read only")
    return cursor


def _schema_facts(cursor) -> dict[str, Any]:
    cursor.execute(
        """
        SELECT to_regclass('public.schema_migrations') IS NOT NULL,
               to_regclass('public.teams') IS NOT NULL,
               to_regclass('public.users') IS NOT NULL,
               to_regclass('public.tenants') IS NOT NULL,
               to_regclass('public.tenant_memberships') IS NOT NULL,
               to_regprocedure('public.app_list_active_tenants(uuid)') IS NOT NULL,
               EXISTS (
                   SELECT 1 FROM information_schema.columns
                   WHERE table_schema='public' AND table_name='users'
                     AND column_name='public_id'
               )
        """
    )
    row = cursor.fetchone()
    if not row or not all(row[:3]):
        raise TenantMappingError("legacy schema prerequisites are missing")
    cursor.execute("SELECT version FROM public.schema_migrations ORDER BY version")
    versions = [str(item[0]) for item in cursor.fetchall()]
    cursor.execute(
        "SELECT COALESCE(rolsuper OR rolbypassrls, FALSE) "
        "FROM pg_catalog.pg_roles WHERE rolname=current_user"
    )
    identity_audit_capable = bool(cursor.fetchone()[0])
    return {
        "migration_count": len(versions),
        "migration_head": versions[-1] if versions else None,
        "tenants_ready": bool(row[3]),
        "memberships_ready": bool(row[4]),
        "discovery_ready": bool(row[5]),
        "users_public_id_ready": bool(row[6]),
        "identity_audit_capable": identity_audit_capable,
    }


def _read_users(cursor) -> list[tuple[Any, ...]]:
    cursor.execute(
        """
        SELECT id, NULL::text, username, enabled, role, team_id
        FROM public.users
        ORDER BY id
        """
    )
    rows = [tuple(row) for row in cursor.fetchall()]
    # The static query above remains valid before 021. Once public_id exists,
    # fetch it in a separate fixed query; never interpolate an identifier.
    cursor.execute(
        """
        SELECT EXISTS (
            SELECT 1 FROM information_schema.columns
            WHERE table_schema='public' AND table_name='users'
              AND column_name='public_id'
        )
        """
    )
    if cursor.fetchone()[0]:
        cursor.execute(
            "SELECT id, public_id::text, username, enabled, role, team_id "
            "FROM public.users ORDER BY id"
        )
        rows = [tuple(row) for row in cursor.fetchall()]
    return rows


def inventory_database(connection) -> dict[str, Any]:
    try:
        cursor = _prepare_read_only(connection)
        try:
            facts = _schema_facts(cursor)
            cursor.execute("SELECT COUNT(*) FROM public.teams")
            team_count = int(cursor.fetchone()[0])
            users = _read_users(cursor)
            role_counts: dict[str, int] = {}
            enabled_count = 0
            disabled_count = 0
            unassigned_count = 0
            for row in users:
                role_counts[str(row[4])] = role_counts.get(str(row[4]), 0) + 1
                enabled_count += int(bool(row[3]))
                disabled_count += int(not bool(row[3]))
                unassigned_count += int(row[5] is None)
            return {
                "status": "inventory",
                **facts,
                "mapping_ready": all(
                    (
                        facts["migration_head"] == "022",
                        facts["tenants_ready"],
                        facts["memberships_ready"],
                        facts["discovery_ready"],
                        facts["users_public_id_ready"],
                        facts["identity_audit_capable"],
                    )
                ),
                "team_count": team_count,
                "user_count": len(users),
                "enabled_user_count": enabled_count,
                "disabled_user_count": disabled_count,
                "unassigned_user_count": unassigned_count,
                "legacy_role_counts": dict(sorted(role_counts.items())),
                "source_users_digest_sha256": _users_digest(users),
            }
        finally:
            cursor.close()
    finally:
        connection.rollback()


def _issue(code: str, subject: Any = None) -> dict[str, str]:
    result = {"code": code}
    if subject is not None:
        result["subject_fingerprint"] = _fingerprint(subject)
    return result


def dry_run_database(
    connection, manifest: MappingManifest
) -> dict[str, Any]:
    try:
        cursor = _prepare_read_only(connection)
        try:
            facts = _schema_facts(cursor)
            if not all(
                (
                    facts["migration_head"] == "022",
                    facts["tenants_ready"],
                    facts["memberships_ready"],
                    facts["discovery_ready"],
                    facts["users_public_id_ready"],
                    facts["identity_audit_capable"],
                )
            ):
                raise TenantMappingError(
                    "tenant mapping prerequisites or controlled audit identity are missing"
                )
            users = _read_users(cursor)
            if _users_digest(users) != manifest.source_users_digest_sha256:
                raise TenantMappingError("source user snapshot digest does not match")
            cursor.execute("SELECT id FROM public.teams ORDER BY id")
            team_ids = {int(row[0]) for row in cursor.fetchall()}
            cursor.execute(
                "SELECT id::text, code, name, status, default_visibility_policy "
                "FROM public.tenants ORDER BY id"
            )
            current_tenants = [tuple(row) for row in cursor.fetchall()]
            cursor.execute(
                "SELECT id::text, tenant_id::text, user_id::text, role, status "
                "FROM public.tenant_memberships ORDER BY id"
            )
            current_memberships = [tuple(row) for row in cursor.fetchall()]

            users_by_id = {int(row[0]): row for row in users}
            classified_ids = {
                member.legacy_user_id_observed
                for tenant in manifest.tenants
                for member in tenant.memberships
            } | {item.legacy_user_id_observed for item in manifest.excluded_users}
            issues: list[dict[str, str]] = []
            for row in users:
                if bool(row[3]) and int(row[0]) not in classified_ids:
                    issues.append(_issue("enabled_user_unclassified", row[1] or row[0]))

            tenant_by_id = {row[0]: row for row in current_tenants}
            tenant_by_code = {row[1]: row for row in current_tenants}
            membership_by_pair = {
                (row[1], row[2]): row for row in current_memberships
            }
            expected_pairs: set[tuple[str, str]] = set()
            actions = {
                "tenant_create": 0,
                "tenant_noop": 0,
                "membership_create": 0,
                "membership_noop": 0,
            }

            for tenant in manifest.tenants:
                tenant_id = str(tenant.tenant_id)
                if tenant.observed_legacy_team_id not in team_ids:
                    issues.append(_issue("legacy_team_unknown", tenant.observed_legacy_team_id))
                by_id = tenant_by_id.get(tenant_id)
                by_code = tenant_by_code.get(tenant.tenant_code)
                if by_id is None and by_code is None:
                    actions["tenant_create"] += 1
                elif by_id is None or by_code is None or by_id != by_code:
                    issues.append(_issue("tenant_identity_conflict", tenant.tenant_id))
                elif (
                    by_id[2] != tenant.tenant_name
                    or by_id[3] != tenant.initial_status
                    or by_id[4] != tenant.default_visibility_policy
                ):
                    issues.append(_issue("tenant_state_drift", tenant.tenant_id))
                else:
                    actions["tenant_noop"] += 1

                for member in tenant.memberships:
                    row = users_by_id.get(member.legacy_user_id_observed)
                    if row is None:
                        issues.append(_issue("legacy_user_unknown", member.user_public_id))
                        continue
                    if (
                        str(row[1]) != str(member.user_public_id)
                        or row[5] != member.legacy_team_id_observed
                        or str(row[4]) != member.legacy_role_observed
                    ):
                        issues.append(_issue("legacy_user_snapshot_drift", member.user_public_id))
                        continue
                    if not bool(row[3]):
                        issues.append(_issue("legacy_user_disabled", member.user_public_id))
                        continue
                    pair = (tenant_id, str(member.user_public_id))
                    expected_pairs.add(pair)
                    existing = membership_by_pair.get(pair)
                    if existing is None:
                        if member.mapping_action != "create":
                            issues.append(
                                _issue(
                                    "membership_action_mismatch",
                                    member.membership_id,
                                )
                            )
                        else:
                            actions["membership_create"] += 1
                    elif (
                        existing[0] != str(member.membership_id)
                        or existing[3] != member.tenant_role
                        or existing[4] != member.membership_status
                    ):
                        issues.append(_issue("membership_state_drift", member.membership_id))
                    else:
                        actions["membership_noop"] += 1

            for item in manifest.excluded_users:
                row = users_by_id.get(item.legacy_user_id_observed)
                if row is None:
                    issues.append(_issue("excluded_user_unknown", item.user_public_id))
                elif (
                    str(row[1]) != str(item.user_public_id)
                    or row[5] != item.legacy_team_id_observed
                    or str(row[4]) != item.legacy_role_observed
                ):
                    issues.append(_issue("excluded_user_snapshot_drift", item.user_public_id))

            target_ids = {str(tenant.tenant_id) for tenant in manifest.tenants}
            mapped_public_ids = {
                str(member.user_public_id)
                for tenant in manifest.tenants
                for member in tenant.memberships
            }
            excluded_public_ids = {
                str(item.user_public_id) for item in manifest.excluded_users
            }
            for pair in sorted(membership_by_pair):
                if (
                    pair not in expected_pairs
                    and (pair[0] in target_ids or pair[1] in mapped_public_ids)
                ):
                    issues.append(_issue("unexpected_existing_membership", pair[1]))
                if pair[1] in excluded_public_ids:
                    issues.append(_issue("excluded_user_has_membership", pair[1]))

            issues = sorted(
                issues,
                key=lambda item: (item["code"], item.get("subject_fingerprint", "")),
            )
            canonical_plan = json.dumps(
                {
                    "batch_id": str(manifest.batch_id),
                    "manifest_sha256": manifest.manifest_sha256,
                    "source_users_digest_sha256": manifest.source_users_digest_sha256,
                    "tenant_ids": [
                        str(tenant.tenant_id) for tenant in manifest.tenants
                    ],
                    "membership_ids": [
                        str(member.membership_id)
                        for tenant in manifest.tenants
                        for member in tenant.memberships
                    ],
                    "actions": actions,
                    "issues": issues,
                },
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
            return {
                "status": "blocked" if issues else "ready",
                "manifest_sha256": manifest.manifest_sha256,
                "plan_sha256": hashlib.sha256(canonical_plan).hexdigest(),
                "actions": actions,
                "issue_count": len(issues),
                "issues": issues,
            }
        finally:
            cursor.close()
    finally:
        connection.rollback()


def _read_setting(name: str, environ: Mapping[str, str] | None = None) -> str:
    env = os.environ if environ is None else environ
    direct = env.get(name)
    file_name = env.get(f"{name}_FILE")
    if direct is not None and file_name is not None:
        raise TenantMappingError(f"configure exactly one of {name} or {name}_FILE")
    if file_name is not None:
        path = Path(file_name)
        if not path.is_file():
            raise TenantMappingError(f"{name}_FILE is not a regular file")
        value = path.read_text(encoding="utf-8").rstrip("\r\n")
    elif direct is not None:
        value = direct
    else:
        raise TenantMappingError(f"missing {name} or {name}_FILE")
    if not value or "\x00" in value or "\n" in value or "\r" in value:
        raise TenantMappingError(f"{name} must be one non-empty line")
    return value


def _connect_database():
    dsn = _read_setting("TENANT_MAPPING_DATABASE_URL")
    try:
        import psycopg2
    except ImportError as error:
        raise TenantMappingError("psycopg2 is required for database inventory") from error
    try:
        return psycopg2.connect(
            dsn, connect_timeout=10, application_name="tenant_mapping_readonly"
        )
    except Exception as error:
        raise TenantMappingError("cannot connect to tenant mapping database") from error


class _SafeArgumentParser(argparse.ArgumentParser):
    def error(self, _message: str) -> None:
        self.print_usage(sys.stderr)
        self.exit(2, "tenant mapping: invalid arguments\n")


def _parser() -> argparse.ArgumentParser:
    parser = _SafeArgumentParser(
        description="Strict read-only tenant mapping inventory and dry-run"
    )
    commands = parser.add_subparsers(
        dest="command", required=True, parser_class=_SafeArgumentParser
    )
    validate = commands.add_parser("validate", help="validate a private manifest")
    validate.add_argument("--manifest", required=True)
    commands.add_parser("inventory", help="read a sanitized database inventory")
    dry_run = commands.add_parser("dry-run", help="compare a manifest without writes")
    dry_run.add_argument("--manifest", required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    connection = None
    try:
        if args.command == "validate":
            result = manifest_summary(load_manifest(args.manifest))
        elif args.command == "inventory":
            target_environment = _read_setting("TENANT_MAPPING_TARGET_ENVIRONMENT")
            if target_environment not in TARGET_ENVIRONMENTS - {"example"}:
                raise TenantMappingError(
                    "TENANT_MAPPING_TARGET_ENVIRONMENT is not allowed"
                )
            connection = _connect_database()
            result = inventory_database(connection)
            result["target_environment"] = target_environment
        elif args.command == "dry-run":
            manifest = load_manifest(args.manifest)
            target_environment = _read_setting("TENANT_MAPPING_TARGET_ENVIRONMENT")
            if target_environment != manifest.target_environment:
                raise TenantMappingError("manifest target environment does not match")
            connection = _connect_database()
            result = dry_run_database(connection, manifest)
        else:  # pragma: no cover - argparse owns the command set.
            raise TenantMappingError("unsupported command")
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
        return 0 if result.get("status") != "blocked" else 2
    except TenantMappingError as error:
        print(
            json.dumps(
                {"status": "error", "error": str(error)},
                ensure_ascii=False,
                sort_keys=True,
            ),
            file=sys.stderr,
        )
        return 2
    except Exception:  # noqa: BLE001 - never expose driver/DSN details.
        print(
            json.dumps(
                {"status": "error", "error": "tenant mapping operation failed"},
                sort_keys=True,
            ),
            file=sys.stderr,
        )
        return 2
    finally:
        if connection is not None:
            connection.close()


if __name__ == "__main__":
    raise SystemExit(main())
