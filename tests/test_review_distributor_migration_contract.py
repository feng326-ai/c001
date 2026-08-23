"""Static security contract for migration 025 review distribution."""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MIGRATION = ROOT / "docs" / "migrations" / "025_review_distributor.sql"

CONTROL_TABLES = (
    "review_distribution_tenant_settings",
    "review_distribution_inbox",
    "review_distribution_batches",
    "review_distribution_targets",
)
PUBLIC_FUNCTIONS = {
    "app_expand_review_distribution": "uuid",
    "app_claim_review_distribution_target": "text, integer",
    "app_apply_review_distribution_target": "uuid, uuid",
    "app_report_review_distribution_failure": "uuid, uuid, text",
}


def _sql() -> str:
    return MIGRATION.read_text(encoding="utf-8").replace("\r\n", "\n")


def _normalized_sql() -> str:
    return re.sub(r"\s+", " ", _sql().lower()).strip()


def _function_body(name: str) -> str:
    match = re.search(
        rf"create\s+or\s+replace\s+function\s+public\.{name}\s*\("
        rf"(?P<arguments>.*?)\)\s*returns\s+(?:table\s*\(.*?\)|[^$]+?)"
        rf"\s+language\s+\w+.*?\bas\s+\$\$(?P<body>.*?)\$\$;",
        _sql(),
        re.IGNORECASE | re.DOTALL,
    )
    assert match is not None, f"missing function public.{name}"
    return re.sub(r"\s+", " ", match.group("body").lower()).strip()


def _function_definition(name: str) -> str:
    match = re.search(
        rf"create\s+or\s+replace\s+function\s+public\.{name}\s*\(.*?\)"
        rf".*?\$\$.*?\$\$;",
        _sql(),
        re.IGNORECASE | re.DOTALL,
    )
    assert match is not None, f"missing function public.{name}"
    return re.sub(r"\s+", " ", match.group(0).lower()).strip()


def _control_function_body(name: str) -> str:
    match = re.search(
        rf"create\s+or\s+replace\s+function\s+platform_control\.{name}"
        rf"\s*\(.*?\).*?\bas\s+\$\$(?P<body>.*?)\$\$;",
        _sql(),
        re.IGNORECASE | re.DOTALL,
    )
    assert match is not None, f"missing function platform_control.{name}"
    return re.sub(r"\s+", " ", match.group("body").lower()).strip()


def _top_level_sql_without_dollar_bodies() -> str:
    return re.sub(
        r"(?:\bas\s+)?\$\$.*?\$\$;",
        "",
        _sql(),
        flags=re.IGNORECASE | re.DOTALL,
    )


def test_025_creates_an_isolated_control_plane_with_force_rls() -> None:
    sql = _normalized_sql()
    assert "create schema if not exists platform_control" in sql
    assert "revoke all on schema platform_control from public" in sql
    for table in CONTROL_TABLES:
        assert f"create table platform_control.{table}" in sql
        assert f"alter table platform_control.{table} enable row level security" in sql
        assert f"alter table platform_control.{table} force row level security" in sql
    assert "revoke all on all tables in schema platform_control from public" in sql
    assert "revoke all on all sequences in schema platform_control from public" in sql


def test_025_exposes_only_four_narrow_security_definer_functions() -> None:
    sql = _normalized_sql()
    for name, signature in PUBLIC_FUNCTIONS.items():
        definition = _function_definition(name)
        assert "security definer" in definition
        assert "set search_path = pg_catalog" in definition
        assert "execute " not in _function_body(name)
        assert f"revoke all on function public.{name}({signature}) from public" in sql

    assert "p_tenant_id" not in sql
    assert "p_tenant_ids" not in sql
    assert "p_policy" not in sql
    assert "p_collection_mode" not in sql
    assert "p_score" not in sql


def test_025_inbox_is_immutable_and_bound_to_an_exact_source_link() -> None:
    sql = _normalized_sql()
    assert "upstream_message_id uuid not null unique" in sql
    assert "input_sha256 text not null" in sql
    assert "input_sha256 ~ '^[0-9a-f]{64}$'" in sql
    assert "unique (id, event_edition_id, trigger_source_document_id)" in sql
    assert (
        "before update or delete on platform_control.review_distribution_inbox" in sql
    )
    assert "review distribution inbox is immutable" in sql
    assert (
        "foreign key ( event_edition_id, trigger_source_document_id, "
        "trigger_collection_mode ) references public.event_sources( "
        "event_edition_id, source_document_id, collection_mode )" in sql
    )


def test_025_freezes_server_derived_eligibility_and_target_snapshots() -> None:
    sql = _normalized_sql()
    expand = _function_body("app_expand_review_distribution")
    for snapshot in (
        "policy_version text not null",
        "eligibility_version text not null",
        "setting_version bigint not null",
        "approval_reference text not null",
        "ruleset_activation_id uuid not null",
        "ruleset_version text not null",
        "ruleset_sha256 text not null",
    ):
        assert snapshot in sql

    assert "setting.status = 'enabled'" in expand
    assert "setting.revoked_at is null" in expand
    assert "setting.effective_at <= now()" in expand
    assert "setting.policy = 'shared_competition'" in expand
    assert "setting.version as setting_version" in expand
    assert "setting.approval_reference" in expand
    assert "tenant.status = 'active'" in expand
    assert "tenant.default_visibility_policy = 'shared_competition'" in expand
    assert "activation.deactivated_at is null" in expand
    assert "for share of setting, tenant, activation" in expand
    assert "tenant_memberships" not in expand
    assert " teams " not in f" {expand} "

    assert "unique (batch_id, tenant_id)" in sql
    assert "unique (tenant_id, event_edition_id)" in sql
    assert (
        "foreign key ( ruleset_activation_id, tenant_id, ruleset_version, "
        "ruleset_sha256 ) references "
        "public.tenant_review_ruleset_activations" in sql
    )


def test_025_freezes_batch_and_target_state_machines_and_lease_shape() -> None:
    sql = _normalized_sql()
    assert "status in ( 'queued', 'running', 'completed', 'partial', 'dead' )" in sql
    assert (
        "status in ( 'pending', 'leased', 'retry', 'succeeded', 'skipped', "
        "'blocked', 'dead' )" in sql
    )
    assert "attempt_count between 0 and 5" in sql
    assert (
        "status = 'leased' and claim_token is not null and claimed_by is not null "
        "and lease_expires_at is not null" in sql
    )
    assert (
        "status = 'retry' and claim_token is null and claimed_by is null and "
        "lease_expires_at is null and next_attempt_at is not null" in sql
    )
    assert (
        "status in ('succeeded', 'skipped', 'blocked', 'dead') and "
        "outcome_code is not null" in sql
    )


def test_025_batch_projection_distinguishes_completed_partial_and_dead() -> None:
    refresh = _control_function_body("refresh_review_distribution_batch")
    assert "for update of batch" in refresh
    assert refresh.index("for update of batch") < refresh.index("count(*) filter")
    counts = re.search(
        r"select count\(\*\) filter \(where status in "
        r"\('pending', 'leased', 'retry'\)\), "
        r"count\(\*\) filter \(where status = 'succeeded'\), "
        r"count\(\*\) filter \(where status in \('blocked', 'dead'\)\) "
        r"into (?P<unfinished>[a-z_]+), (?P<success>[a-z_]+), "
        r"(?P<failed>[a-z_]+)",
        refresh,
    )
    assert counts is not None
    unfinished = counts.group("unfinished")
    success = counts.group("success")
    failed = counts.group("failed")
    assert f"when {unfinished} > 0 then 'running'" in refresh

    completed = refresh.index(f"when {failed} = 0 then 'completed'")
    partial = refresh.index(f"when {success} > 0 then 'partial'")
    dead = refresh.index("else 'dead'")
    assert completed < partial < dead
    assert "status = 'skipped'" not in refresh


def test_025_distribution_setting_is_one_way_revocable() -> None:
    sql = _normalized_sql()
    guard = _control_function_body("guard_distribution_setting_update")
    assert "old.status <> 'enabled'" in guard
    assert "new.status <> 'disabled'" in guard
    assert "new.eligibility_version is distinct from old.eligibility_version" in guard
    assert "new.version <> old.version + 1" in guard
    assert "trg_guard_review_distribution_setting_update" in sql
    assert "platform_control.guard_distribution_setting_update() from public" in sql


def test_025_rejects_historical_and_terminal_or_merged_sources_twice() -> None:
    sql = _normalized_sql()
    expand = _function_body("app_expand_review_distribution")
    apply = _function_body("app_apply_review_distribution_target")

    assert "check (trigger_collection_mode = 'realtime_signal')" in sql
    assert "inbox_row.trigger_collection_mode <> 'realtime_signal'" in expand
    assert "'result_published', 'ended', 'cancelled'" in expand
    assert "'merged', 'tombstoned'" in expand
    assert "source_link.collection_mode = 'realtime_signal'" in apply
    assert "'result_published', 'ended', 'cancelled'" in apply
    assert "'merged', 'tombstoned'" in apply
    assert "outcome_code = 'source_ineligible'" in apply


def test_025_claim_and_failure_paths_use_database_fencing_and_bounded_retry() -> None:
    claim = _function_body("app_claim_review_distribution_target")
    apply = _function_body("app_apply_review_distribution_target")
    failure = _function_body("app_report_review_distribution_failure")

    assert "for update of target skip locked" in claim
    assert "claim_token = gen_random_uuid()" in claim
    assert "target.lease_expires_at <= now()" in claim
    assert "p_lease_seconds not between 5 and 300" in claim
    assert "target.attempt_count < 5" in claim
    assert "target.attempt_count >= 5" in claim
    assert "last_error_code = 'lease_expired'" in claim

    for body in (apply, failure):
        assert "target_row.status <> 'leased'" in body
        assert "target_row.claim_token is distinct from p_claim_token" in body
        assert "target_row.lease_expires_at <= now()" in body
        assert "stale review distribution claim" in body
        assert "errcode = 'serialization_failure'" in body

    assert "if target_row.attempt_count >= 5" in failure
    assert "new_status := 'dead'" in failure
    assert "new_status := 'retry'" in failure
    assert "least(300, (2 ^ target_row.attempt_count)::integer)" in failure


def test_025_apply_is_one_atomic_candidate_outbox_target_transition() -> None:
    apply = _function_body("app_apply_review_distribution_target")
    grant_insert = apply.index("insert into public.tenant_resource_grants")
    candidate_insert = apply.index("insert into public.tenant_candidates")
    outbox_insert = apply.index("insert into public.domain_outbox")
    success_update = apply.rindex("update platform_control.review_distribution_targets")
    assert grant_insert < candidate_insert < outbox_insert < success_update
    assert "'candidate.created.v1'" in apply
    assert "'tenant_candidate'" in apply
    assert "commit" not in apply
    assert "rollback" not in apply
    assert "pg_advisory_xact_lock" in apply
    assert (
        "target_row.tenant_id::text || ':' || "
        "target_row.event_edition_id::text" in apply
    )

    assert "outcome_code = 'already_exists'" in apply
    assert "outcome_code = 'blocked_regrant_required'" in apply
    assert "candidate_row.candidate_status in ('open', 'in_review')" in apply
    assert "grant_row.status = 'active'" in apply
    assert "grant_row.policy_version = target_row.policy_version" in apply
    assert (
        "grant_row.trigger_source_document_id = "
        "target_row.trigger_source_document_id" in apply
    )


def test_025_contains_no_environment_seed_roles_or_destructive_ddl() -> None:
    top_level = _top_level_sql_without_dollar_bodies()
    normalized = re.sub(r"\s+", " ", top_level.lower()).strip()
    assert not re.search(r"\binsert\s+into\b", normalized)
    assert not re.search(r"\b(create|alter)\s+role\b", normalized)
    assert not re.search(r"\bgrant\s+", normalized)
    assert not re.search(r"\bdelete\s+from\b", normalized)
    assert not re.search(r"\bdrop\s+(table|schema|column)\b", normalized)
    assert not re.search(
        r"\b(update|insert\s+into)\s+(public\.)?(tenants|tenant_memberships|teams)\b",
        normalized,
    )
