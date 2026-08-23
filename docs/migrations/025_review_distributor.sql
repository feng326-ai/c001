-- 025: dormant, controlled shared-competition review distribution.
-- No real tenant eligibility or inbox rows are seeded by this migration.

DO $$
DECLARE
    owner_can_bypass_rls BOOLEAN;
BEGIN
    SELECT rolsuper OR rolbypassrls
    INTO owner_can_bypass_rls
    FROM pg_catalog.pg_roles
    WHERE rolname = CURRENT_USER;

    IF owner_can_bypass_rls IS DISTINCT FROM TRUE THEN
        RAISE EXCEPTION
            '025 requires a controlled migration owner that can bypass FORCE RLS';
    END IF;
END;
$$;

CREATE SCHEMA IF NOT EXISTS platform_control;
REVOKE ALL ON SCHEMA platform_control FROM PUBLIC;

CREATE TABLE platform_control.review_distribution_tenant_settings (
    tenant_id              UUID PRIMARY KEY REFERENCES public.tenants(id),
    status                 TEXT NOT NULL DEFAULT 'enabled',
    policy                 TEXT NOT NULL DEFAULT 'shared_competition',
    policy_version         TEXT NOT NULL,
    eligibility_version    TEXT NOT NULL,
    approval_reference     TEXT NOT NULL,
    effective_at           TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    revoked_at             TIMESTAMPTZ NULL,
    version                BIGINT NOT NULL DEFAULT 1,
    created_at             TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at             TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT ck_review_distribution_setting_status
        CHECK (status IN ('enabled', 'disabled')),
    CONSTRAINT ck_review_distribution_setting_policy
        CHECK (policy = 'shared_competition'),
    CONSTRAINT ck_review_distribution_setting_policy_version
        CHECK (policy_version = 'shared-competition/1.0.0'),
    CONSTRAINT ck_review_distribution_setting_eligibility_version
        CHECK (BTRIM(eligibility_version) <> ''),
    CONSTRAINT ck_review_distribution_setting_approval
        CHECK (BTRIM(approval_reference) <> ''),
    CONSTRAINT ck_review_distribution_setting_revocation_shape
        CHECK (
            (status = 'enabled' AND revoked_at IS NULL)
            OR (status = 'disabled' AND revoked_at IS NOT NULL)
        ),
    CONSTRAINT ck_review_distribution_setting_time_order
        CHECK (revoked_at IS NULL OR effective_at <= revoked_at),
    CONSTRAINT ck_review_distribution_setting_version
        CHECK (version >= 1)
);

CREATE OR REPLACE FUNCTION platform_control.guard_distribution_setting_update()
RETURNS TRIGGER
LANGUAGE plpgsql
SECURITY INVOKER
SET search_path = pg_catalog
AS $$
BEGIN
    IF OLD.status <> 'enabled'
       OR NEW.status <> 'disabled'
       OR NEW.revoked_at IS NULL
       OR NEW.version <> OLD.version + 1
       OR NEW.tenant_id IS DISTINCT FROM OLD.tenant_id
       OR NEW.policy IS DISTINCT FROM OLD.policy
       OR NEW.policy_version IS DISTINCT FROM OLD.policy_version
       OR NEW.eligibility_version IS DISTINCT FROM OLD.eligibility_version
       OR NEW.approval_reference IS DISTINCT FROM OLD.approval_reference
       OR NEW.effective_at IS DISTINCT FROM OLD.effective_at
       OR NEW.created_at IS DISTINCT FROM OLD.created_at
    THEN
        RAISE EXCEPTION 'review distribution setting only permits one-way revocation'
            USING ERRCODE = 'check_violation';
    END IF;
    NEW.updated_at := NOW();
    RETURN NEW;
END;
$$;

CREATE TRIGGER trg_guard_review_distribution_setting_update
    BEFORE UPDATE
    ON platform_control.review_distribution_tenant_settings
    FOR EACH ROW
    EXECUTE FUNCTION platform_control.guard_distribution_setting_update();

CREATE TABLE platform_control.review_distribution_inbox (
    id                         UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    upstream_message_id        UUID NOT NULL UNIQUE,
    input_sha256               TEXT NOT NULL,
    event_edition_id           UUID NOT NULL,
    trigger_source_document_id UUID NOT NULL,
    trigger_collection_mode    TEXT NOT NULL,
    received_at                TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    created_at                 TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT uq_review_distribution_inbox_identity
        UNIQUE (id, event_edition_id, trigger_source_document_id),
    CONSTRAINT fk_review_distribution_inbox_source
        FOREIGN KEY (
            event_edition_id,
            trigger_source_document_id,
            trigger_collection_mode
        ) REFERENCES public.event_sources(
            event_edition_id,
            source_document_id,
            collection_mode
        ),
    CONSTRAINT ck_review_distribution_inbox_hash
        CHECK (input_sha256 ~ '^[0-9a-f]{64}$'),
    CONSTRAINT ck_review_distribution_inbox_mode
        CHECK (trigger_collection_mode IN (
            'realtime_signal', 'historical_backfill'
        ))
);

CREATE OR REPLACE FUNCTION platform_control.reject_distribution_inbox_mutation()
RETURNS TRIGGER
LANGUAGE plpgsql
SECURITY INVOKER
SET search_path = pg_catalog
AS $$
BEGIN
    RAISE EXCEPTION 'review distribution inbox is immutable'
        USING ERRCODE = 'check_violation';
END;
$$;

CREATE TRIGGER trg_review_distribution_inbox_immutable
    BEFORE UPDATE OR DELETE
    ON platform_control.review_distribution_inbox
    FOR EACH ROW
    EXECUTE FUNCTION platform_control.reject_distribution_inbox_mutation();

CREATE TABLE platform_control.review_distribution_batches (
    id                         UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    inbox_id                   UUID NOT NULL UNIQUE
        REFERENCES platform_control.review_distribution_inbox(id),
    event_edition_id           UUID NOT NULL,
    trigger_source_document_id UUID NOT NULL,
    policy                     TEXT NOT NULL DEFAULT 'shared_competition',
    policy_version             TEXT NOT NULL,
    status                     TEXT NOT NULL DEFAULT 'queued',
    target_count               INTEGER NOT NULL DEFAULT 0,
    created_at                 TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at                 TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    completed_at               TIMESTAMPTZ NULL,
    CONSTRAINT uq_review_distribution_batch_snapshot
        UNIQUE (
            id, event_edition_id, trigger_source_document_id, policy_version
        ),
    CONSTRAINT fk_review_distribution_batch_inbox_snapshot
        FOREIGN KEY (
            inbox_id, event_edition_id, trigger_source_document_id
        ) REFERENCES platform_control.review_distribution_inbox(
            id, event_edition_id, trigger_source_document_id
        ),
    CONSTRAINT ck_review_distribution_batch_policy
        CHECK (policy = 'shared_competition'),
    CONSTRAINT ck_review_distribution_batch_policy_version
        CHECK (policy_version = 'shared-competition/1.0.0'),
    CONSTRAINT ck_review_distribution_batch_status
        CHECK (status IN (
            'queued', 'running', 'completed', 'partial', 'dead'
        )),
    CONSTRAINT ck_review_distribution_batch_target_count
        CHECK (target_count >= 0),
    CONSTRAINT ck_review_distribution_batch_completion
        CHECK (
            (status IN ('completed', 'partial', 'dead')
                AND completed_at IS NOT NULL)
            OR (status IN ('queued', 'running') AND completed_at IS NULL)
        )
);

CREATE TABLE platform_control.review_distribution_targets (
    id                         UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    batch_id                   UUID NOT NULL,
    tenant_id                  UUID NOT NULL REFERENCES public.tenants(id),
    event_edition_id           UUID NOT NULL,
    trigger_source_document_id UUID NOT NULL,
    trigger_collection_mode    TEXT NOT NULL DEFAULT 'realtime_signal',
    policy                     TEXT NOT NULL DEFAULT 'shared_competition',
    policy_version             TEXT NOT NULL,
    eligibility_version        TEXT NOT NULL,
    setting_version            BIGINT NOT NULL,
    approval_reference         TEXT NOT NULL,
    ruleset_activation_id      UUID NOT NULL,
    ruleset_version            TEXT NOT NULL,
    ruleset_sha256             TEXT NOT NULL,
    status                     TEXT NOT NULL DEFAULT 'pending',
    claim_token                UUID NULL,
    claimed_by                 TEXT NULL,
    lease_expires_at           TIMESTAMPTZ NULL,
    attempt_count              INTEGER NOT NULL DEFAULT 0,
    next_attempt_at            TIMESTAMPTZ NULL,
    result_grant_id            UUID NULL,
    result_candidate_id        UUID NULL,
    outcome_code               TEXT NULL,
    last_error_code            TEXT NULL,
    created_at                 TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at                 TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    completed_at               TIMESTAMPTZ NULL,
    CONSTRAINT uq_review_distribution_target_batch_tenant
        UNIQUE (batch_id, tenant_id),
    CONSTRAINT uq_review_distribution_target_tenant_edition
        UNIQUE (tenant_id, event_edition_id),
    CONSTRAINT fk_review_distribution_target_batch
        FOREIGN KEY (
            batch_id,
            event_edition_id,
            trigger_source_document_id,
            policy_version
        ) REFERENCES platform_control.review_distribution_batches(
            id,
            event_edition_id,
            trigger_source_document_id,
            policy_version
        ),
    CONSTRAINT fk_review_distribution_target_source
        FOREIGN KEY (
            event_edition_id,
            trigger_source_document_id,
            trigger_collection_mode
        ) REFERENCES public.event_sources(
            event_edition_id,
            source_document_id,
            collection_mode
        ),
    CONSTRAINT fk_review_distribution_target_ruleset
        FOREIGN KEY (
            ruleset_activation_id,
            tenant_id,
            ruleset_version,
            ruleset_sha256
        ) REFERENCES public.tenant_review_ruleset_activations(
            id, tenant_id, ruleset_version, ruleset_sha256
        ),
    CONSTRAINT fk_review_distribution_target_result_grant
        FOREIGN KEY (result_grant_id, tenant_id, event_edition_id)
        REFERENCES public.tenant_resource_grants(
            id, tenant_id, event_edition_id
        ),
    CONSTRAINT fk_review_distribution_target_result_candidate
        FOREIGN KEY (
            result_candidate_id,
            tenant_id,
            event_edition_id,
            result_grant_id
        ) REFERENCES public.tenant_candidates(
            id, tenant_id, event_edition_id, grant_id
        ),
    CONSTRAINT ck_review_distribution_target_mode
        CHECK (trigger_collection_mode = 'realtime_signal'),
    CONSTRAINT ck_review_distribution_target_policy
        CHECK (policy = 'shared_competition'),
    CONSTRAINT ck_review_distribution_target_policy_version
        CHECK (policy_version = 'shared-competition/1.0.0'),
    CONSTRAINT ck_review_distribution_target_eligibility_version
        CHECK (BTRIM(eligibility_version) <> ''),
    CONSTRAINT ck_review_distribution_target_setting_version
        CHECK (setting_version >= 1),
    CONSTRAINT ck_review_distribution_target_approval
        CHECK (BTRIM(approval_reference) <> ''),
    CONSTRAINT ck_review_distribution_target_ruleset_hash
        CHECK (ruleset_sha256 ~ '^[0-9a-f]{64}$'),
    CONSTRAINT ck_review_distribution_target_status
        CHECK (status IN (
            'pending', 'leased', 'retry', 'succeeded',
            'skipped', 'blocked', 'dead'
        )),
    CONSTRAINT ck_review_distribution_target_attempts
        CHECK (attempt_count BETWEEN 0 AND 5),
    CONSTRAINT ck_review_distribution_target_result_shape
        CHECK (
            (result_grant_id IS NULL AND result_candidate_id IS NULL)
            OR (result_grant_id IS NOT NULL AND result_candidate_id IS NOT NULL)
        ),
    CONSTRAINT ck_review_distribution_target_lease_shape
        CHECK (
            (status = 'leased'
                AND claim_token IS NOT NULL
                AND claimed_by IS NOT NULL
                AND lease_expires_at IS NOT NULL
                AND next_attempt_at IS NULL
                AND completed_at IS NULL)
            OR (status = 'retry'
                AND claim_token IS NULL
                AND claimed_by IS NULL
                AND lease_expires_at IS NULL
                AND next_attempt_at IS NOT NULL
                AND completed_at IS NULL)
            OR (status = 'pending'
                AND claim_token IS NULL
                AND claimed_by IS NULL
                AND lease_expires_at IS NULL
                AND next_attempt_at IS NULL
                AND completed_at IS NULL)
            OR (status IN ('succeeded', 'skipped', 'blocked', 'dead')
                AND claim_token IS NULL
                AND claimed_by IS NULL
                AND lease_expires_at IS NULL
                AND next_attempt_at IS NULL
                AND completed_at IS NOT NULL)
        ),
    CONSTRAINT ck_review_distribution_target_outcome
        CHECK (
            (status IN ('pending', 'leased', 'retry') AND outcome_code IS NULL)
            OR (status IN ('succeeded', 'skipped', 'blocked', 'dead')
                AND outcome_code IS NOT NULL
                AND BTRIM(outcome_code) <> '')
        ),
    CONSTRAINT ck_review_distribution_target_error
        CHECK (
            last_error_code IS NULL
            OR last_error_code ~ '^[a-z0-9_]{1,64}$'
        )
);

CREATE INDEX idx_review_distribution_targets_claim
    ON platform_control.review_distribution_targets(
        status, next_attempt_at, lease_expires_at, created_at, id
    )
    WHERE status IN ('pending', 'leased', 'retry');

ALTER TABLE platform_control.review_distribution_tenant_settings
    ENABLE ROW LEVEL SECURITY;
ALTER TABLE platform_control.review_distribution_tenant_settings
    FORCE ROW LEVEL SECURITY;
ALTER TABLE platform_control.review_distribution_inbox ENABLE ROW LEVEL SECURITY;
ALTER TABLE platform_control.review_distribution_inbox FORCE ROW LEVEL SECURITY;
ALTER TABLE platform_control.review_distribution_batches ENABLE ROW LEVEL SECURITY;
ALTER TABLE platform_control.review_distribution_batches FORCE ROW LEVEL SECURITY;
ALTER TABLE platform_control.review_distribution_targets ENABLE ROW LEVEL SECURITY;
ALTER TABLE platform_control.review_distribution_targets FORCE ROW LEVEL SECURITY;

CREATE OR REPLACE FUNCTION platform_control.refresh_review_distribution_batch(
    p_batch_id UUID
)
RETURNS VOID
LANGUAGE plpgsql
VOLATILE
SECURITY INVOKER
SET search_path = pg_catalog
AS $$
DECLARE
    unfinished_count INTEGER;
    success_count INTEGER;
    failed_count INTEGER;
BEGIN
    PERFORM batch.id
    FROM platform_control.review_distribution_batches AS batch
    WHERE batch.id = p_batch_id
    FOR UPDATE OF batch;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'review distribution batch not found'
            USING ERRCODE = 'no_data_found';
    END IF;

    SELECT
        COUNT(*) FILTER (WHERE status IN ('pending', 'leased', 'retry')),
        COUNT(*) FILTER (WHERE status = 'succeeded'),
        COUNT(*) FILTER (WHERE status IN ('blocked', 'dead'))
    INTO unfinished_count, success_count, failed_count
    FROM platform_control.review_distribution_targets
    WHERE batch_id = p_batch_id;

    UPDATE platform_control.review_distribution_batches
        SET status = CASE
            WHEN unfinished_count > 0 THEN 'running'
            WHEN failed_count = 0 THEN 'completed'
            WHEN success_count > 0 THEN 'partial'
            ELSE 'dead'
        END,
        completed_at = CASE
            WHEN unfinished_count > 0 THEN NULL
            ELSE NOW()
        END,
        updated_at = NOW()
    WHERE id = p_batch_id;
END;
$$;

CREATE OR REPLACE FUNCTION public.app_expand_review_distribution(
    p_inbox_id UUID
)
RETURNS TABLE (
    batch_id UUID,
    batch_status TEXT,
    target_count INTEGER,
    replayed BOOLEAN
)
LANGUAGE plpgsql
VOLATILE
SECURITY DEFINER
SET search_path = pg_catalog
AS $$
DECLARE
    inbox_row RECORD;
    existing_batch RECORD;
    target_plan RECORD;
    new_batch_id UUID;
    inserted_target UUID;
    planned_count INTEGER := 0;
BEGIN
    SELECT inbox.id,
           inbox.event_edition_id,
           inbox.trigger_source_document_id,
           inbox.trigger_collection_mode,
           edition.activity_stage,
           edition.resolution_status
    INTO inbox_row
    FROM platform_control.review_distribution_inbox AS inbox
    JOIN public.event_sources AS source_link
      ON source_link.event_edition_id = inbox.event_edition_id
     AND source_link.source_document_id = inbox.trigger_source_document_id
     AND source_link.collection_mode = inbox.trigger_collection_mode
    JOIN public.source_documents AS source_document
      ON source_document.id = source_link.source_document_id
     AND source_document.collection_mode = source_link.collection_mode
    JOIN public.event_editions AS edition
      ON edition.id = source_link.event_edition_id
    WHERE inbox.id = p_inbox_id
    FOR SHARE OF inbox, source_link, source_document, edition;

    IF NOT FOUND THEN
        RAISE EXCEPTION 'review distribution inbox not found'
            USING ERRCODE = 'no_data_found';
    END IF;
    IF inbox_row.trigger_collection_mode <> 'realtime_signal'
       OR inbox_row.activity_stage IN (
            'result_published', 'ended', 'cancelled'
       )
       OR inbox_row.resolution_status IN ('merged', 'tombstoned')
    THEN
        RAISE EXCEPTION 'review distribution inbox is not eligible'
            USING ERRCODE = 'invalid_parameter_value';
    END IF;

    INSERT INTO platform_control.review_distribution_batches(
        inbox_id, event_edition_id, trigger_source_document_id,
        policy, policy_version
    ) VALUES (
        inbox_row.id,
        inbox_row.event_edition_id,
        inbox_row.trigger_source_document_id,
        'shared_competition',
        'shared-competition/1.0.0'
    )
    ON CONFLICT (inbox_id) DO NOTHING
    RETURNING id INTO new_batch_id;

    IF new_batch_id IS NULL THEN
        SELECT batch.id, batch.status, batch.target_count
        INTO existing_batch
        FROM platform_control.review_distribution_batches AS batch
        WHERE batch.inbox_id = inbox_row.id
        FOR UPDATE OF batch;
        RETURN QUERY SELECT existing_batch.id,
                            existing_batch.status,
                            existing_batch.target_count,
                            TRUE;
        RETURN;
    END IF;

    FOR target_plan IN
        SELECT setting.tenant_id,
               setting.eligibility_version,
               setting.version AS setting_version,
               setting.approval_reference,
               activation.id AS activation_id,
               activation.ruleset_version,
               activation.ruleset_sha256
        FROM platform_control.review_distribution_tenant_settings AS setting
        JOIN public.tenants AS tenant
          ON tenant.id = setting.tenant_id
        JOIN public.tenant_review_ruleset_activations AS activation
          ON activation.tenant_id = setting.tenant_id
         AND activation.deactivated_at IS NULL
        WHERE setting.status = 'enabled'
          AND setting.revoked_at IS NULL
          AND setting.effective_at <= NOW()
          AND setting.policy = 'shared_competition'
          AND setting.policy_version = 'shared-competition/1.0.0'
          AND tenant.status = 'active'
          AND tenant.default_visibility_policy = 'shared_competition'
        ORDER BY setting.tenant_id
        FOR SHARE OF setting, tenant, activation
    LOOP
        inserted_target := NULL;
        INSERT INTO platform_control.review_distribution_targets(
            batch_id, tenant_id, event_edition_id,
            trigger_source_document_id, trigger_collection_mode,
            policy, policy_version, eligibility_version,
            setting_version, approval_reference,
            ruleset_activation_id, ruleset_version, ruleset_sha256
        ) VALUES (
            new_batch_id,
            target_plan.tenant_id,
            inbox_row.event_edition_id,
            inbox_row.trigger_source_document_id,
            'realtime_signal',
            'shared_competition',
            'shared-competition/1.0.0',
            target_plan.eligibility_version,
            target_plan.setting_version,
            target_plan.approval_reference,
            target_plan.activation_id,
            target_plan.ruleset_version,
            target_plan.ruleset_sha256
        )
        ON CONFLICT (tenant_id, event_edition_id) DO NOTHING
        RETURNING id INTO inserted_target;
        IF inserted_target IS NOT NULL THEN
            planned_count := planned_count + 1;
        END IF;
    END LOOP;

    UPDATE platform_control.review_distribution_batches
    SET target_count = planned_count,
        status = CASE WHEN planned_count = 0 THEN 'completed' ELSE 'queued' END,
        completed_at = CASE WHEN planned_count = 0 THEN NOW() ELSE NULL END,
        updated_at = NOW()
    WHERE id = new_batch_id;

    RETURN QUERY SELECT new_batch_id,
                        CASE WHEN planned_count = 0 THEN 'completed' ELSE 'queued' END,
                        planned_count,
                        FALSE;
END;
$$;

CREATE OR REPLACE FUNCTION public.app_claim_review_distribution_target(
    p_worker_id TEXT,
    p_lease_seconds INTEGER
)
RETURNS TABLE (
    target_id UUID,
    claim_token UUID,
    lease_expires_at TIMESTAMPTZ
)
LANGUAGE plpgsql
VOLATILE
SECURITY DEFINER
SET search_path = pg_catalog
AS $$
DECLARE
    claimed RECORD;
    expired_batch_id UUID;
BEGIN
    IF p_worker_id !~ '^[a-zA-Z0-9][a-zA-Z0-9._:-]{2,63}$'
       OR p_lease_seconds NOT BETWEEN 5 AND 300
    THEN
        RAISE EXCEPTION 'invalid review distributor claim request'
            USING ERRCODE = 'invalid_parameter_value';
    END IF;

    FOR expired_batch_id IN
        WITH expired_target AS (
            SELECT target.id
            FROM platform_control.review_distribution_targets AS target
            WHERE target.status = 'leased'
              AND target.lease_expires_at <= NOW()
              AND target.attempt_count >= 5
            ORDER BY target.lease_expires_at, target.id
            FOR UPDATE OF target SKIP LOCKED
            LIMIT 100
        ), terminalized AS (
            UPDATE platform_control.review_distribution_targets AS target
            SET status = 'dead', outcome_code = 'retry_exhausted',
                claim_token = NULL, claimed_by = NULL,
                lease_expires_at = NULL, next_attempt_at = NULL,
                last_error_code = 'lease_expired',
                completed_at = NOW(), updated_at = NOW()
            FROM expired_target
            WHERE target.id = expired_target.id
            RETURNING target.batch_id
        )
        SELECT DISTINCT terminalized.batch_id FROM terminalized
    LOOP
        PERFORM platform_control.refresh_review_distribution_batch(
            expired_batch_id
        );
    END LOOP;

    WITH next_target AS (
        SELECT target.id
        FROM platform_control.review_distribution_targets AS target
        WHERE (
                target.status = 'pending'
                OR (target.status = 'retry'
                    AND target.next_attempt_at <= NOW())
                OR (target.status = 'leased'
                    AND target.lease_expires_at <= NOW())
              )
          AND target.attempt_count < 5
        ORDER BY COALESCE(target.next_attempt_at, target.created_at), target.id
        FOR UPDATE OF target SKIP LOCKED
        LIMIT 1
    )
    UPDATE platform_control.review_distribution_targets AS target
    SET status = 'leased',
        claim_token = gen_random_uuid(),
        claimed_by = p_worker_id,
        lease_expires_at = NOW() + make_interval(secs => p_lease_seconds),
        attempt_count = target.attempt_count + 1,
        next_attempt_at = NULL,
        last_error_code = NULL,
        updated_at = NOW()
    FROM next_target
    WHERE target.id = next_target.id
    RETURNING target.id, target.claim_token, target.lease_expires_at,
              target.batch_id
    INTO claimed;

    IF NOT FOUND THEN
        RETURN;
    END IF;

    UPDATE platform_control.review_distribution_batches
    SET status = 'running', updated_at = NOW()
    WHERE id = claimed.batch_id
      AND status = 'queued';

    RETURN QUERY SELECT claimed.id, claimed.claim_token,
                        claimed.lease_expires_at;
END;
$$;

CREATE OR REPLACE FUNCTION public.app_apply_review_distribution_target(
    p_target_id UUID,
    p_claim_token UUID
)
RETURNS TABLE (
    target_id UUID,
    target_status TEXT,
    outcome_code TEXT,
    grant_id UUID,
    candidate_id UUID
)
LANGUAGE plpgsql
VOLATILE
SECURITY DEFINER
SET search_path = pg_catalog
AS $$
DECLARE
    target_row RECORD;
    candidate_lookup RECORD;
    candidate_row RECORD;
    grant_row RECORD;
    new_grant_id UUID;
    new_candidate_id UUID;
    upstream_message_id UUID;
    eligibility_ok BOOLEAN := FALSE;
    source_ok BOOLEAN := FALSE;
BEGIN
    SELECT target.*, inbox.upstream_message_id
    INTO target_row
    FROM platform_control.review_distribution_targets AS target
    JOIN platform_control.review_distribution_batches AS batch
      ON batch.id = target.batch_id
    JOIN platform_control.review_distribution_inbox AS inbox
      ON inbox.id = batch.inbox_id
    WHERE target.id = p_target_id
    FOR UPDATE OF target;

    IF NOT FOUND
       OR target_row.status <> 'leased'
       OR target_row.claim_token IS DISTINCT FROM p_claim_token
       OR target_row.lease_expires_at <= NOW()
    THEN
        RAISE EXCEPTION 'stale review distribution claim'
            USING ERRCODE = 'serialization_failure';
    END IF;
    upstream_message_id := target_row.upstream_message_id;

    SELECT TRUE
    INTO eligibility_ok
    FROM platform_control.review_distribution_tenant_settings AS setting
    JOIN public.tenants AS tenant
      ON tenant.id = setting.tenant_id
    JOIN public.tenant_review_ruleset_activations AS activation
      ON activation.id = target_row.ruleset_activation_id
     AND activation.tenant_id = target_row.tenant_id
     AND activation.ruleset_version = target_row.ruleset_version
     AND activation.ruleset_sha256 = target_row.ruleset_sha256
    WHERE setting.tenant_id = target_row.tenant_id
      AND setting.status = 'enabled'
      AND setting.revoked_at IS NULL
      AND setting.effective_at <= NOW()
      AND setting.policy = target_row.policy
      AND setting.policy_version = target_row.policy_version
      AND setting.eligibility_version = target_row.eligibility_version
      AND setting.version = target_row.setting_version
      AND setting.approval_reference = target_row.approval_reference
      AND tenant.status = 'active'
      AND tenant.default_visibility_policy = 'shared_competition'
      AND activation.deactivated_at IS NULL
    FOR SHARE OF setting, tenant, activation;

    IF NOT COALESCE(eligibility_ok, FALSE) THEN
        UPDATE platform_control.review_distribution_targets
        SET status = 'skipped', outcome_code = 'tenant_or_ruleset_ineligible',
            claim_token = NULL, claimed_by = NULL, lease_expires_at = NULL,
            completed_at = NOW(), updated_at = NOW()
        WHERE id = target_row.id;
        PERFORM platform_control.refresh_review_distribution_batch(
            target_row.batch_id
        );
        RETURN QUERY SELECT target_row.id, 'skipped'::TEXT,
                            'tenant_or_ruleset_ineligible'::TEXT,
                            NULL::UUID, NULL::UUID;
        RETURN;
    END IF;

    SELECT TRUE
    INTO source_ok
    FROM public.event_sources AS source_link
    JOIN public.source_documents AS source_document
      ON source_document.id = source_link.source_document_id
     AND source_document.collection_mode = source_link.collection_mode
    JOIN public.event_editions AS edition
      ON edition.id = source_link.event_edition_id
    WHERE source_link.event_edition_id = target_row.event_edition_id
      AND source_link.source_document_id = target_row.trigger_source_document_id
      AND source_link.collection_mode = 'realtime_signal'
      AND edition.activity_stage NOT IN (
          'result_published', 'ended', 'cancelled'
      )
      AND edition.resolution_status NOT IN ('merged', 'tombstoned')
    FOR SHARE OF source_link, source_document, edition;

    IF NOT COALESCE(source_ok, FALSE) THEN
        UPDATE platform_control.review_distribution_targets
        SET status = 'skipped', outcome_code = 'source_ineligible',
            claim_token = NULL, claimed_by = NULL, lease_expires_at = NULL,
            completed_at = NOW(), updated_at = NOW()
        WHERE id = target_row.id;
        PERFORM platform_control.refresh_review_distribution_batch(
            target_row.batch_id
        );
        RETURN QUERY SELECT target_row.id, 'skipped'::TEXT,
                            'source_ineligible'::TEXT,
                            NULL::UUID, NULL::UUID;
        RETURN;
    END IF;

    PERFORM pg_advisory_xact_lock(
        hashtextextended(
            target_row.tenant_id::TEXT || ':' ||
            target_row.event_edition_id::TEXT,
            0
        )
    );

    SELECT candidate.id, candidate.grant_id
    INTO candidate_lookup
    FROM public.tenant_candidates AS candidate
    WHERE candidate.tenant_id = target_row.tenant_id
      AND candidate.event_edition_id = target_row.event_edition_id;

    IF FOUND THEN
        SELECT grant_record.id, grant_record.status, grant_record.policy,
               grant_record.policy_version,
               grant_record.trigger_source_document_id,
               grant_record.trigger_collection_mode
        INTO grant_row
        FROM public.tenant_resource_grants AS grant_record
        WHERE grant_record.id = candidate_lookup.grant_id
          AND grant_record.tenant_id = target_row.tenant_id
          AND grant_record.event_edition_id = target_row.event_edition_id
        FOR UPDATE OF grant_record;
        IF NOT FOUND THEN
            RAISE EXCEPTION 'candidate grant disappeared during distribution'
                USING ERRCODE = 'serialization_failure';
        END IF;

        SELECT candidate.id, candidate.grant_id, candidate.candidate_status
        INTO candidate_row
        FROM public.tenant_candidates AS candidate
        WHERE candidate.id = candidate_lookup.id
          AND candidate.tenant_id = target_row.tenant_id
          AND candidate.event_edition_id = target_row.event_edition_id
          AND candidate.grant_id = grant_row.id
        FOR UPDATE OF candidate;
        IF NOT FOUND THEN
            RAISE EXCEPTION 'candidate changed during distribution'
                USING ERRCODE = 'serialization_failure';
        END IF;

        IF candidate_row.candidate_status IN ('open', 'in_review')
           AND grant_row.status = 'active'
           AND grant_row.policy = target_row.policy
           AND grant_row.policy_version = target_row.policy_version
           AND grant_row.trigger_source_document_id =
               target_row.trigger_source_document_id
           AND grant_row.trigger_collection_mode =
               target_row.trigger_collection_mode
        THEN
            UPDATE platform_control.review_distribution_targets
            SET status = 'succeeded', outcome_code = 'already_exists',
                result_grant_id = grant_row.id,
                result_candidate_id = candidate_row.id,
                claim_token = NULL, claimed_by = NULL,
                lease_expires_at = NULL,
                completed_at = NOW(), updated_at = NOW()
            WHERE id = target_row.id;
            PERFORM platform_control.refresh_review_distribution_batch(
                target_row.batch_id
            );
            RETURN QUERY SELECT target_row.id, 'succeeded'::TEXT,
                                'already_exists'::TEXT,
                                grant_row.id,
                                candidate_row.id;
        ELSE
            UPDATE platform_control.review_distribution_targets
            SET status = 'blocked',
                outcome_code = 'blocked_regrant_required',
                result_grant_id = grant_row.id,
                result_candidate_id = candidate_row.id,
                claim_token = NULL, claimed_by = NULL,
                lease_expires_at = NULL,
                completed_at = NOW(), updated_at = NOW()
            WHERE id = target_row.id;
            PERFORM platform_control.refresh_review_distribution_batch(
                target_row.batch_id
            );
            RETURN QUERY SELECT target_row.id, 'blocked'::TEXT,
                                'blocked_regrant_required'::TEXT,
                                grant_row.id,
                                candidate_row.id;
        END IF;
        RETURN;
    END IF;

    SELECT grant_record.id, grant_record.status, grant_record.policy,
           grant_record.policy_version,
           grant_record.trigger_source_document_id,
           grant_record.trigger_collection_mode
    INTO grant_row
    FROM public.tenant_resource_grants AS grant_record
    WHERE grant_record.tenant_id = target_row.tenant_id
      AND grant_record.event_edition_id = target_row.event_edition_id
      AND grant_record.status = 'active'
      AND grant_record.revoked_at IS NULL
    FOR UPDATE OF grant_record;

    IF FOUND THEN
        IF grant_row.policy <> target_row.policy
           OR grant_row.policy_version <> target_row.policy_version
           OR grant_row.trigger_source_document_id <>
              target_row.trigger_source_document_id
           OR grant_row.trigger_collection_mode <>
              target_row.trigger_collection_mode
        THEN
            UPDATE platform_control.review_distribution_targets
            SET status = 'blocked', outcome_code = 'active_grant_conflict',
                claim_token = NULL, claimed_by = NULL,
                lease_expires_at = NULL,
                completed_at = NOW(), updated_at = NOW()
            WHERE id = target_row.id;
            PERFORM platform_control.refresh_review_distribution_batch(
                target_row.batch_id
            );
            RETURN QUERY SELECT target_row.id, 'blocked'::TEXT,
                                'active_grant_conflict'::TEXT,
                                NULL::UUID, NULL::UUID;
            RETURN;
        END IF;
        new_grant_id := grant_row.id;
    ELSE
        IF EXISTS (
            SELECT 1
            FROM public.tenant_resource_grants AS grant_history
            WHERE grant_history.tenant_id = target_row.tenant_id
              AND grant_history.event_edition_id = target_row.event_edition_id
        ) THEN
            UPDATE platform_control.review_distribution_targets
            SET status = 'blocked',
                outcome_code = 'blocked_regrant_required',
                claim_token = NULL, claimed_by = NULL,
                lease_expires_at = NULL,
                completed_at = NOW(), updated_at = NOW()
            WHERE id = target_row.id;
            PERFORM platform_control.refresh_review_distribution_batch(
                target_row.batch_id
            );
            RETURN QUERY SELECT target_row.id, 'blocked'::TEXT,
                                'blocked_regrant_required'::TEXT,
                                NULL::UUID, NULL::UUID;
            RETURN;
        END IF;

        INSERT INTO public.tenant_resource_grants(
            tenant_id, event_edition_id, trigger_source_document_id,
            trigger_collection_mode, policy, policy_version,
            status, grant_source
        ) VALUES (
            target_row.tenant_id,
            target_row.event_edition_id,
            target_row.trigger_source_document_id,
            'realtime_signal',
            'shared_competition',
            target_row.policy_version,
            'active',
            'review_distribution_v1'
        )
        RETURNING id INTO new_grant_id;
    END IF;

    INSERT INTO public.tenant_candidates(
        tenant_id, grant_id, event_edition_id, candidate_status
    ) VALUES (
        target_row.tenant_id,
        new_grant_id,
        target_row.event_edition_id,
        'open'
    )
    RETURNING id INTO new_candidate_id;

    INSERT INTO public.domain_outbox(
        tenant_id, event_type, schema_version,
        aggregate_type, aggregate_id, aggregate_version,
        correlation_id, causation_id, payload
    ) VALUES (
        target_row.tenant_id,
        'candidate.created.v1',
        '1.0',
        'tenant_candidate',
        new_candidate_id,
        1,
        upstream_message_id,
        upstream_message_id,
        jsonb_build_object(
            'candidate_id', new_candidate_id,
            'event_edition_id', target_row.event_edition_id,
            'grant_id', new_grant_id,
            'policy', 'shared_competition',
            'policy_version', target_row.policy_version,
            'trigger_source_document_id',
                target_row.trigger_source_document_id,
            'distribution_batch_id', target_row.batch_id
        )
    );

    UPDATE platform_control.review_distribution_targets
    SET status = 'succeeded', outcome_code = 'created',
        result_grant_id = new_grant_id,
        result_candidate_id = new_candidate_id,
        claim_token = NULL, claimed_by = NULL, lease_expires_at = NULL,
        completed_at = NOW(), updated_at = NOW()
    WHERE id = target_row.id;
    PERFORM platform_control.refresh_review_distribution_batch(
        target_row.batch_id
    );

    RETURN QUERY SELECT target_row.id, 'succeeded'::TEXT, 'created'::TEXT,
                        new_grant_id, new_candidate_id;
END;
$$;

CREATE OR REPLACE FUNCTION public.app_report_review_distribution_failure(
    p_target_id UUID,
    p_claim_token UUID,
    p_error_code TEXT
)
RETURNS TABLE (
    target_id UUID,
    target_status TEXT,
    attempt_count INTEGER,
    next_attempt_at TIMESTAMPTZ
)
LANGUAGE plpgsql
VOLATILE
SECURITY DEFINER
SET search_path = pg_catalog
AS $$
DECLARE
    target_row RECORD;
    new_status TEXT;
    retry_at TIMESTAMPTZ;
BEGIN
    IF p_error_code !~ '^[a-z0-9_]{1,64}$' THEN
        RAISE EXCEPTION 'invalid review distribution error code'
            USING ERRCODE = 'invalid_parameter_value';
    END IF;

    SELECT * INTO target_row
    FROM platform_control.review_distribution_targets AS target
    WHERE target.id = p_target_id
    FOR UPDATE;

    IF NOT FOUND
       OR target_row.status <> 'leased'
       OR target_row.claim_token IS DISTINCT FROM p_claim_token
       OR target_row.lease_expires_at <= NOW()
    THEN
        RAISE EXCEPTION 'stale review distribution claim'
            USING ERRCODE = 'serialization_failure';
    END IF;

    IF target_row.attempt_count >= 5 THEN
        new_status := 'dead';
        retry_at := NULL;
    ELSE
        new_status := 'retry';
        retry_at := NOW() + make_interval(
            secs => LEAST(300, (2 ^ target_row.attempt_count)::INTEGER)
        );
    END IF;

    UPDATE platform_control.review_distribution_targets
    SET status = new_status,
        claim_token = NULL,
        claimed_by = NULL,
        lease_expires_at = NULL,
        next_attempt_at = retry_at,
        outcome_code = CASE WHEN new_status = 'dead' THEN 'retry_exhausted' END,
        last_error_code = p_error_code,
        completed_at = CASE WHEN new_status = 'dead' THEN NOW() END,
        updated_at = NOW()
    WHERE id = target_row.id;

    PERFORM platform_control.refresh_review_distribution_batch(
        target_row.batch_id
    );
    RETURN QUERY SELECT target_row.id, new_status,
                        target_row.attempt_count, retry_at;
END;
$$;

COMMENT ON FUNCTION public.app_expand_review_distribution(UUID) IS
    'Expand one trusted immutable inbox into a frozen server-derived tenant target plan.';
COMMENT ON FUNCTION public.app_claim_review_distribution_target(TEXT, INTEGER) IS
    'Lease one database-selected distribution target and issue a fencing token.';
COMMENT ON FUNCTION public.app_apply_review_distribution_target(UUID, UUID) IS
    'Atomically apply one fenced tenant target without accepting a tenant identifier.';
COMMENT ON FUNCTION public.app_report_review_distribution_failure(UUID, UUID, TEXT) IS
    'Return one fenced target to bounded retry or terminal dead state.';

REVOKE ALL ON FUNCTION
    platform_control.guard_distribution_setting_update() FROM PUBLIC;
REVOKE ALL ON FUNCTION
    platform_control.reject_distribution_inbox_mutation() FROM PUBLIC;
REVOKE ALL ON FUNCTION
    platform_control.refresh_review_distribution_batch(UUID) FROM PUBLIC;
REVOKE ALL ON FUNCTION public.app_expand_review_distribution(UUID) FROM PUBLIC;
REVOKE ALL ON FUNCTION
    public.app_claim_review_distribution_target(TEXT, INTEGER) FROM PUBLIC;
REVOKE ALL ON FUNCTION
    public.app_apply_review_distribution_target(UUID, UUID) FROM PUBLIC;
REVOKE ALL ON FUNCTION
    public.app_report_review_distribution_failure(UUID, UUID, TEXT) FROM PUBLIC;

REVOKE ALL ON ALL TABLES IN SCHEMA platform_control FROM PUBLIC;
REVOKE ALL ON ALL SEQUENCES IN SCHEMA platform_control FROM PUBLIC;

COMMENT ON SCHEMA platform_control IS
    'Platform-only control plane; normal application and tenant roles have no USAGE.';
COMMENT ON TABLE platform_control.review_distribution_tenant_settings IS
    'Explicit approved tenant eligibility; migration 025 intentionally seeds no rows.';
COMMENT ON TABLE platform_control.review_distribution_inbox IS
    'Immutable trusted intake; the production writer is deliberately not enabled in this batch.';
COMMENT ON TABLE platform_control.review_distribution_targets IS
    'Frozen per-tenant work plan claimed and applied through fenced narrow functions.';
