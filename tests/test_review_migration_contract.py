import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
MIGRATION = ROOT / "docs" / "migrations" / "023_review_vertical_slice.sql"


def _sql() -> str:
    return MIGRATION.read_text(encoding="utf-8").replace("\r\n", "\n")


def _normalized_sql() -> str:
    return re.sub(r"\s+", " ", _sql().lower()).strip()


def test_023_creates_only_the_review_slice_domain_tables() -> None:
    sql = _normalized_sql()
    for table in (
        "source_documents",
        "organizations",
        "event_series",
        "event_editions",
        "event_sources",
        "tenant_resource_grants",
        "tenant_candidates",
        "tenant_reviews",
        "domain_outbox",
        "tenant_command_receipts",
    ):
        assert f"create table public.{table}" in sql

    assert "create table public.opportunities" not in sql
    assert "create table public.user_sessions" not in sql


def test_023_keeps_collection_paths_separate_at_the_database_boundary() -> None:
    sql = _normalized_sql()
    assert "collection_mode text not null" in sql
    assert "collection_mode in ('realtime_signal', 'historical_backfill')" in sql
    assert "unique (id, collection_mode)" in sql
    assert "foreign key (source_document_id, collection_mode) references public.source_documents(id, collection_mode)" in sql
    assert "foreign key ( event_edition_id, trigger_source_document_id, trigger_collection_mode ) references public.event_sources( event_edition_id, source_document_id, collection_mode )" in sql
    assert "check (trigger_collection_mode = 'realtime_signal')" in sql
    event_editions = sql.split("create table public.event_editions", 1)[1].split(
        "create table public.event_sources", 1
    )[0]
    assert "collection_mode" not in event_editions


def test_023_freezes_shared_competition_without_exclusive() -> None:
    sql = _normalized_sql()
    assert "policy text not null default 'shared_competition'" in sql
    assert "'shared_competition', 'tenant_private', 'selected_tenants'" in sql
    assert "exclusive" not in sql


def test_023_enforces_tenant_candidate_and_grant_consistency() -> None:
    sql = _normalized_sql()
    assert "unique (tenant_id, event_edition_id)" in sql
    assert "foreign key (grant_id, tenant_id, event_edition_id) references public.tenant_resource_grants(id, tenant_id, event_edition_id)" in sql
    assert "unique (id, tenant_id, event_edition_id, policy_version)" in sql
    assert "where status = 'active' and revoked_at is null" in sql
    assert "candidate_status in ('open', 'in_review', 'closed', 'withdrawn')" in sql


def test_023_review_history_has_composite_scope_and_snapshot_keys() -> None:
    sql = _normalized_sql()
    assert "unique (tenant_id, candidate_id, review_round)" in sql
    assert "foreign key (candidate_id, tenant_id, event_edition_id, grant_id) references public.tenant_candidates(id, tenant_id, event_edition_id, grant_id)" in sql
    assert "foreign key (grant_id, tenant_id, event_edition_id, grant_policy_version) references public.tenant_resource_grants( id, tenant_id, event_edition_id, policy_version )" in sql
    assert "foreign key (tenant_id, reviewer_user_id) references public.tenant_memberships(tenant_id, user_id)" in sql
    assert "where review_status in ('pending', 'in_review')" in sql


def test_023_review_status_shape_and_completed_immutability_are_structural() -> None:
    sql = _normalized_sql()
    assert "review_status in ('pending', 'in_review', 'completed', 'cancelled')" in sql
    assert "review_decision in ('qualified', 'rejected', 'needs_more_info')" in sql
    assert "disposition in ('sales_handoff', 'nurture', 'competitor_watch', 'archive')" in sql
    assert "review_status = 'completed'" in sql
    assert "reviewer_user_id is not null" in sql
    assert "completed_at is not null" in sql
    assert "if old.review_status in ('completed', 'cancelled')" in sql
    assert "before update or delete on public.tenant_reviews" in sql


def test_023_revoking_a_grant_cancels_active_review_without_touching_completed() -> None:
    sql = _normalized_sql()
    assert "after update of status on public.tenant_resource_grants" in sql
    assert "old.status = 'active'" in sql
    assert "new.status in ('revoked', 'superseded')" in sql
    assert "from public.tenant_candidates as candidate" in sql
    assert "from public.tenant_reviews as review" in sql
    assert "for update" in sql
    assert "set review_status = 'cancelled'" in sql
    assert "cancel_reason = 'grant_revoked'" in sql
    assert "set candidate_status = 'withdrawn'" in sql
    assert "review_status in ('pending', 'in_review')" in sql


def test_023_has_a_locked_fail_closed_write_authorizer() -> None:
    sql = _normalized_sql()
    assert "function public.app_authorize_tenant_write( p_legacy_user_id integer, p_tenant_id uuid )" in sql
    assert "returns table ( user_public_id uuid, membership_id uuid, membership_role text )" in sql
    assert "language sql volatile security definer set search_path = pg_catalog" in sql
    assert "u.enabled = true" in sql
    assert "tm.status = 'active'" in sql
    assert "t.status = 'active'" in sql
    assert "for share of u, t, tm" in sql
    assert "revoke all on function public.app_authorize_tenant_write(integer, uuid) from public" in sql


def test_023_locks_active_grants_without_runtime_control_table_update() -> None:
    sql = _normalized_sql()
    assert "function public.app_lock_active_review_grant(" in sql
    assert "p_tenant_id = public.app_current_tenant_id()" in sql
    assert "grant_row.status = 'active'" in sql
    assert "grant_row.revoked_at is null" in sql
    assert "for share of grant_row" in sql
    assert (
        "revoke all on function "
        "public.app_lock_active_review_grant(uuid, uuid) from public"
    ) in sql


def test_023_outbox_has_envelope_and_business_idempotency_key() -> None:
    sql = _normalized_sql()
    for column in (
        "message_id uuid primary key",
        "tenant_id uuid not null",
        "event_type text not null",
        "schema_version text not null",
        "aggregate_id uuid not null",
        "aggregate_version bigint not null",
        "correlation_id uuid not null",
        "payload jsonb not null",
    ):
        assert column in sql
    assert "unique (tenant_id, event_type, aggregate_id, aggregate_version)" in sql
    assert "status in ('pending', 'published', 'failed')" in sql


def test_023_persists_tenant_scoped_http_command_idempotency() -> None:
    sql = _normalized_sql()
    assert "create table public.tenant_command_receipts" in sql
    assert "request_hash text not null" in sql
    assert "response_json jsonb null" in sql
    assert "request_hash ~ '^[0-9a-f]{64}$'" in sql
    assert "unique (tenant_id, command_name, idempotency_key)" in sql


def test_023_forces_rls_on_every_tenant_owned_table() -> None:
    sql = _normalized_sql()
    for table in (
        "tenant_resource_grants",
        "tenant_candidates",
        "tenant_reviews",
        "domain_outbox",
        "tenant_command_receipts",
    ):
        assert f"alter table public.{table} enable row level security" in sql
        assert f"alter table public.{table} force row level security" in sql
        assert f"on public.{table}" in sql
    assert sql.count(
        "using (tenant_id = public.app_current_tenant_id())"
    ) == 5
    assert sql.count(
        "with check (tenant_id = public.app_current_tenant_id())"
    ) == 5


def test_023_is_expand_only_and_contains_no_environment_data_or_roles() -> None:
    sql = _normalized_sql()
    assert not re.search(r"\binsert\s+into\b", sql)
    assert not re.search(r"\bupdate\s+(public\.)?(users|teams|articles_core|qualified_leads)\b", sql)
    assert not re.search(r"\bdelete\s+from\b", sql)
    assert not re.search(r"\bdrop\s+(table|column|constraint)\b", sql)
    assert not re.search(r"\bcreate\s+role\b", sql)
    assert not re.search(r"(?m)^\s*grant\s+", _sql(), re.IGNORECASE)
