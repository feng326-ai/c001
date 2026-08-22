import hashlib
import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MIGRATION = ROOT / "docs" / "migrations" / "024_review_ruleset.sql"


def _sql() -> str:
    return MIGRATION.read_text(encoding="utf-8").replace("\r\n", "\n")


def _normalized_sql() -> str:
    return re.sub(r"\s+", " ", _sql().lower()).strip()


def test_024_creates_rules_reasons_activation_and_score_tables() -> None:
    sql = _normalized_sql()
    for table in (
        "review_rulesets",
        "review_ruleset_completion_reasons",
        "review_ruleset_reopen_reasons",
        "tenant_review_ruleset_activations",
        "tenant_candidate_score_snapshots",
    ):
        assert f"create table public.{table}" in sql

    assert "unique (version, definition_sha256)" in sql
    assert "max_review_rounds between 2 and 10" in sql
    assert "unique ( ruleset_version, reason_code, review_decision, disposition )" in sql


def test_024_published_rule_rows_and_score_snapshots_are_immutable() -> None:
    sql = _normalized_sql()
    assert "function public.app_reject_immutable_review_rule()" in sql
    for trigger in (
        "trg_review_rulesets_immutable",
        "trg_review_ruleset_completion_immutable",
        "trg_review_ruleset_reopen_immutable",
        "trg_tenant_candidate_scores_immutable",
    ):
        assert f"create trigger {trigger}" in sql
    for table in (
        "review_rulesets",
        "review_ruleset_completion_reasons",
        "review_ruleset_reopen_reasons",
        "tenant_candidate_score_snapshots",
    ):
        assert f"before update or delete on public.{table}" in sql

    assert "function public.app_guard_review_ruleset_activation()" in sql
    assert "if tg_op = 'delete' then" in sql
    assert "old.deactivated_at is null" in sql
    assert "new.deactivated_at is not null" in sql
    assert "completion reason differs from ruleset definition" in sql
    assert "reopen reason differs from ruleset definition" in sql
    assert "review ruleset cannot activate before complete publish" in sql
    assert "before insert or update or delete on public.tenant_review_ruleset_activations" in sql


def test_024_forces_rls_on_both_tenant_owned_tables() -> None:
    sql = _normalized_sql()
    for table in (
        "tenant_review_ruleset_activations",
        "tenant_candidate_score_snapshots",
    ):
        assert f"alter table public.{table} enable row level security" in sql
        assert f"alter table public.{table} force row level security" in sql
        policy = re.search(
            rf"create policy [a-z0-9_]+ on public\.{table} .*?;",
            sql,
        )
        assert policy is not None
        assert policy.group(0).count(
            "tenant_id = public.app_current_tenant_id()"
        ) == 2


def test_024_active_ruleset_lock_is_narrow_and_public_execute_is_revoked() -> None:
    sql = _normalized_sql()
    function = sql.split(
        "create or replace function public.app_lock_active_review_ruleset(", 1
    )[1].split("revoke all on function", 1)[0]
    assert "requested_tenant_id uuid" in function
    assert "language sql volatile security definer set search_path = pg_catalog" in function
    assert "requested_tenant_id = public.app_current_tenant_id()" in function
    assert "activation.deactivated_at is null" in function
    assert "for share of activation" in function
    assert "execute " not in function
    assert (
        "revoke all on function public.app_lock_active_review_ruleset(uuid) "
        "from public"
    ) in sql


def test_024_review_snapshot_is_complete_and_bound_by_composite_fks() -> None:
    sql = _normalized_sql()
    for column in (
        "rule_activation_id uuid null",
        "rule_definition_sha256 text null",
        "rule_snapshot jsonb null",
        "reopen_reason_code text null",
        "reopen_trigger_source_document_id uuid null",
        "reopen_not_before timestamptz null",
    ):
        assert column in sql

    assert "foreign key ( rule_activation_id, tenant_id, rule_version, rule_definition_sha256 )" in sql
    assert "references public.tenant_review_ruleset_activations( id, tenant_id, ruleset_version, ruleset_sha256 )" in sql
    assert "foreign key (rule_version, rule_definition_sha256) references public.review_rulesets(version, definition_sha256)" in sql
    assert "foreign key ( rule_version, reason_code, review_decision, disposition )" in sql
    assert "references public.review_ruleset_completion_reasons( ruleset_version, reason_code, review_decision, disposition )" in sql
    assert "rule_activation_id is null and rule_version is null" in sql
    assert "rule_activation_id is not null and rule_version is not null" in sql
    assert "jsonb_typeof(rule_snapshot) = 'object'" in sql


def test_024_database_trigger_guards_new_and_completed_review_rules() -> None:
    sql = _normalized_sql()
    assert "function public.app_validate_versioned_tenant_review()" in sql
    assert "new tenant review requires a versioned ruleset snapshot" in sql
    assert "if tg_op = 'insert' and new.rule_activation_id is null" in sql
    assert "tenant review ruleset snapshot does not match catalog" in sql
    assert "legacy unversioned review cannot be newly completed" in sql
    assert "completed review requires a versioned primary reason" in sql
    assert "completed review reason matrix is invalid" in sql
    assert "review completion capability is not available" in sql
    assert "tenant review rule and reopen provenance are immutable" in sql
    assert "before insert or update on public.tenant_reviews" in sql
    assert "on public.tenant_reviews" in sql


def test_024_score_snapshots_are_explainable_and_idempotent() -> None:
    sql = _normalized_sql()
    assert "scoring_method_version text not null" in sql
    assert "scoring_method_version, input_hash" in sql
    assert "foreign key (candidate_id, tenant_id, event_edition_id, grant_id)" in sql
    assert "foreign key (ruleset_version, ruleset_sha256)" in sql
    assert "total_score between 0 and 100" in sql
    assert "priority_band in ('urgent', 'high', 'normal', 'low')" in sql
    assert "jsonb_typeof(component_scores) = 'object'" in sql
    assert "jsonb_typeof(evidence_refs) = 'array'" in sql
    assert "function public.app_validate_candidate_score_snapshot()" in sql
    assert "score snapshot envelope differs from ruleset" in sql
    assert "score snapshot total or band differs from components" in sql


def test_024_forward_fix_only_withdraws_unfinished_work() -> None:
    sql = _normalized_sql()
    forward_fix = sql.split("-- forward-fix 023:", 1)[1].split(
        "-- published repository ruleset", 1
    )[0]
    assert "review.review_status in ('pending', 'in_review')" in forward_fix
    assert "review_status in ('pending', 'in_review')" in forward_fix
    assert "candidate_status in ('open', 'in_review')" in forward_fix
    assert "candidate_status <> 'withdrawn'" not in forward_fix
    assert "candidate_status in ('open', 'in_review', 'closed')" not in forward_fix


def test_024_seeds_rules_but_never_activates_a_real_tenant() -> None:
    raw_sql = _sql()
    sql = _normalized_sql()
    assert "insert into public.review_rulesets" in sql
    assert "insert into public.review_ruleset_completion_reasons" in sql
    assert "insert into public.review_ruleset_reopen_reasons" in sql
    assert not re.search(
        r"\binsert\s+into\s+public\.tenant_review_ruleset_activations\b",
        sql,
    )
    assert not re.search(r"\binsert\s+into\s+public\.(tenants|tenant_memberships)\b", sql)

    seed = re.search(
        r"'([0-9a-f]{64})',\s*\$ruleset\$(\{.*?\})\$ruleset\$::JSONB",
        raw_sql,
        re.DOTALL,
    )
    assert seed is not None
    declared_hash, definition_text = seed.groups()
    definition = json.loads(definition_text)
    canonical = json.dumps(
        definition,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    assert hashlib.sha256(canonical).hexdigest() == declared_hash
