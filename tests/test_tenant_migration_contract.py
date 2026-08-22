import hashlib
import json
import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
MIGRATION = ROOT / "docs" / "migrations" / "021_tenant_identity_rls.sql"
MEMBERSHIP_DISCOVERY_MIGRATION = (
    ROOT / "docs" / "migrations" / "022_tenant_membership_discovery.sql"
)
BASELINE = ROOT / "docs" / "migrations" / "checksum_baseline.json"


def _sql() -> str:
    return MIGRATION.read_text(encoding="utf-8-sig")


def _normalized_sql() -> str:
    return re.sub(r"\s+", " ", _sql()).strip().lower()


def _discovery_sql() -> str:
    return MEMBERSHIP_DISCOVERY_MIGRATION.read_text(encoding="utf-8-sig")


def _normalized_discovery_sql() -> str:
    return re.sub(r"\s+", " ", _discovery_sql()).strip().lower()


def test_users_gain_stable_public_identity_without_replacing_legacy_id():
    sql = _normalized_sql()

    assert "alter table users add column if not exists public_id uuid" in sql
    assert "alter column public_id set default gen_random_uuid()" in sql
    assert re.search(
        r"update users set public_id = gen_random_uuid\(\) "
        r"where public_id is null",
        sql,
    )
    assert "alter column public_id set not null" in sql
    assert re.search(
        r"create unique index if not exists \w+ "
        r"on users \(public_id\)",
        sql,
    )

    assert not re.search(r"drop\s+(table|column)", sql)
    assert not re.search(r"alter\s+column\s+id\b", sql)
    assert not re.search(r"alter\s+table\s+teams\b", sql)


def test_tenant_and_membership_fields_match_the_frozen_contract():
    sql = _normalized_sql()

    required_tenant_fragments = (
        "id uuid primary key default gen_random_uuid()",
        "code text not null unique",
        "name text not null",
        "status text not null default 'active'",
        "default_visibility_policy text not null default 'shared_competition'",
        "version bigint not null default 1",
        "created_at timestamptz not null default now()",
        "updated_at timestamptz not null default now()",
        "check (status in ('active', 'disabled'))",
        "'shared_competition'",
        "'tenant_private'",
        "'selected_tenants'",
    )
    assert "create table tenants" in sql
    for fragment in required_tenant_fragments:
        assert fragment in sql
    assert "'exclusive'" not in sql

    required_membership_fragments = (
        "tenant_id uuid not null references tenants(id)",
        "user_id uuid not null references users(public_id)",
        "unique (tenant_id, user_id)",
        "'tenant_admin'",
        "'resource_reviewer'",
        "'sales'",
        "'readonly_manager'",
        "check (status in ('active', 'disabled'))",
        "version bigint not null default 1",
    )
    assert "create table tenant_memberships" in sql
    for fragment in required_membership_fragments:
        assert fragment in sql

    assert "on tenant_memberships (user_id, status)" in sql
    assert "on tenant_memberships (tenant_id, status)" in sql


def test_tenant_context_parser_is_transaction_scoped_and_fails_closed():
    sql = _normalized_sql()

    assert "create or replace function app_current_tenant_id()" in sql
    assert "current_setting('app.tenant_id', true)" in sql
    assert "security invoker" in sql
    assert "set search_path = pg_catalog" in sql
    assert "return null" in sql
    assert re.search(
        r"\^\[0-9a-f\]\{8\}-\[0-9a-f\]\{4\}-"
        r"\[0-9a-f\]\{4\}-\[0-9a-f\]\{4\}-"
        r"\[0-9a-f\]\{12\}\$",
        sql,
    )
    assert "set local" in sql
    assert "set_config(..., true)" in sql


def test_new_tenant_tables_enable_and_force_symmetric_rls():
    sql = _normalized_sql()

    for table in ("tenants", "tenant_memberships"):
        assert f"alter table {table} enable row level security" in sql
        assert f"alter table {table} force row level security" in sql

    assert re.search(
        r"create policy tenants_tenant_isolation on tenants .*?"
        r"using \(id = public\.app_current_tenant_id\(\)\) .*?"
        r"with check \(id = public\.app_current_tenant_id\(\)\)",
        sql,
    )
    assert re.search(
        r"create policy tenant_memberships_tenant_isolation "
        r"on tenant_memberships .*?"
        r"using \(tenant_id = public\.app_current_tenant_id\(\)\) .*?"
        r"with check \(tenant_id = public\.app_current_tenant_id\(\)\)",
        sql,
    )


def test_migration_stays_inside_expand_only_boundary():
    sql = _normalized_sql()

    forbidden_tables = (
        "tenant_resource_grants",
        "tenant_candidates",
        "tenant_reviews",
        "opportunities",
    )
    for table in forbidden_tables:
        assert not re.search(rf"create\s+table\s+{table}\b", sql)

    assert not re.search(r"insert\s+into\s+(tenants|tenant_memberships)\b", sql)
    assert not re.search(r"\b(create|alter)\s+role\b", sql)
    assert not re.search(r"\bgrant\b", sql)
    assert "bypassrls" not in sql
    assert "alter table users enable row level security" not in sql
    assert "alter table teams enable row level security" not in sql


def test_021_baseline_is_sha256_only_and_keeps_legacy_cutoff_frozen():
    payload = json.loads(BASELINE.read_text(encoding="utf-8"))
    canonical = (
        _sql().replace("\r\n", "\n").replace("\r", "\n").encode("utf-8")
    )

    assert payload["legacy_md5_through"] == "020"
    assert payload["migrations"]["021"] == {
        "sha256": hashlib.sha256(canonical).hexdigest()
    }


def test_022_membership_discovery_is_narrow_and_security_hardened():
    sql = _normalized_discovery_sql()

    assert "create or replace function public.app_list_active_tenants" in sql
    assert "security definer" in sql
    assert "set search_path = pg_catalog" in sql
    assert "from public.users as u" in sql
    assert "join public.tenant_memberships as tm" in sql
    assert "join public.tenants as t" in sql
    assert "u.enabled = true" in sql
    assert "tm.status = 'active'" in sql
    assert "t.status = 'active'" in sql
    assert (
        "revoke all on function public.app_list_active_tenants(uuid) from public"
        in sql
    )
    assert "rolsuper or rolbypassrls" in sql

    assert not re.search(r"\bgrant\b", sql)
    assert not re.search(r"\b(create|alter)\s+role\b", sql)
    assert not re.search(r"create\s+table\b", sql)
    assert "create policy" not in sql
    assert "set_config(" not in sql


def test_022_baseline_is_sha256_only_and_keeps_legacy_cutoff_frozen():
    payload = json.loads(BASELINE.read_text(encoding="utf-8"))
    canonical = (
        _discovery_sql()
        .replace("\r\n", "\n")
        .replace("\r", "\n")
        .encode("utf-8")
    )

    assert payload["legacy_md5_through"] == "020"
    assert payload["migrations"]["022"] == {
        "sha256": hashlib.sha256(canonical).hexdigest()
    }
