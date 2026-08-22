-- 023: shared event intelligence -> tenant review vertical slice (expand-only).
-- This migration creates no tenant/person seed data and no cross-tenant dispatcher.

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
            '023 requires a controlled migration owner that can bypass FORCE RLS';
    END IF;
END;
$$;

CREATE OR REPLACE FUNCTION public.app_authorize_tenant_write(
    p_legacy_user_id INTEGER,
    p_tenant_id UUID
)
RETURNS TABLE (
    user_public_id UUID,
    membership_id UUID,
    membership_role TEXT
)
LANGUAGE sql
VOLATILE
SECURITY DEFINER
SET search_path = pg_catalog
AS $$
    SELECT
        u.public_id,
        tm.id,
        tm.role
    FROM public.users AS u
    JOIN public.tenant_memberships AS tm
      ON tm.user_id = u.public_id
     AND tm.tenant_id = p_tenant_id
    JOIN public.tenants AS t
      ON t.id = tm.tenant_id
    WHERE u.id = p_legacy_user_id
      AND u.enabled = TRUE
      AND tm.status = 'active'
      AND t.status = 'active'
    FOR SHARE OF u, t, tm;
$$;

COMMENT ON FUNCTION public.app_authorize_tenant_write(INTEGER, UUID) IS
    'Post-auth write authorization with identity locks; callers must SET LOCAL tenant context afterwards.';

REVOKE ALL ON FUNCTION public.app_authorize_tenant_write(INTEGER, UUID) FROM PUBLIC;

CREATE TABLE public.source_documents (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    source_channel      TEXT NOT NULL,
    source_key          TEXT NOT NULL,
    source_url          TEXT NULL,
    title               TEXT NULL,
    collection_mode     TEXT NOT NULL,
    content_sha256      TEXT NOT NULL,
    published_at        TIMESTAMPTZ NULL,
    observed_at         TIMESTAMPTZ NOT NULL,
    metadata            JSONB NOT NULL DEFAULT '{}'::JSONB,
    version             BIGINT NOT NULL DEFAULT 1,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT uq_source_documents_channel_key
        UNIQUE (source_channel, source_key),
    CONSTRAINT uq_source_documents_id_collection_mode
        UNIQUE (id, collection_mode),
    CONSTRAINT ck_source_documents_channel_not_blank
        CHECK (BTRIM(source_channel) <> ''),
    CONSTRAINT ck_source_documents_key_not_blank
        CHECK (BTRIM(source_key) <> ''),
    CONSTRAINT ck_source_documents_collection_mode
        CHECK (collection_mode IN ('realtime_signal', 'historical_backfill')),
    CONSTRAINT ck_source_documents_content_sha256
        CHECK (content_sha256 ~ '^[0-9a-f]{64}$'),
    CONSTRAINT ck_source_documents_metadata_object
        CHECK (JSONB_TYPEOF(metadata) = 'object'),
    CONSTRAINT ck_source_documents_version_positive CHECK (version >= 1)
);

CREATE INDEX idx_source_documents_mode_observed
    ON public.source_documents (collection_mode, observed_at DESC, id);

CREATE INDEX idx_source_documents_content_sha256
    ON public.source_documents (content_sha256);

CREATE TABLE public.organizations (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    canonical_name      TEXT NOT NULL,
    normalized_name     TEXT NOT NULL,
    identity_key        TEXT NULL,
    organization_type   TEXT NOT NULL DEFAULT 'unknown',
    region_code         TEXT NULL,
    region_name         TEXT NULL,
    industry_code       TEXT NULL,
    official_url        TEXT NULL,
    resolution_status   TEXT NOT NULL DEFAULT 'proposed',
    merged_into_id      UUID NULL REFERENCES public.organizations(id),
    confidence          NUMERIC(5, 4) NULL,
    human_verified      BOOLEAN NOT NULL DEFAULT FALSE,
    version             BIGINT NOT NULL DEFAULT 1,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT ck_organizations_canonical_name_not_blank
        CHECK (BTRIM(canonical_name) <> ''),
    CONSTRAINT ck_organizations_normalized_name_not_blank
        CHECK (BTRIM(normalized_name) <> ''),
    CONSTRAINT ck_organizations_identity_key_not_blank
        CHECK (identity_key IS NULL OR BTRIM(identity_key) <> ''),
    CONSTRAINT ck_organizations_resolution_status
        CHECK (resolution_status IN ('proposed', 'confirmed', 'merged', 'tombstoned')),
    CONSTRAINT ck_organizations_merge_shape
        CHECK (
            (resolution_status = 'merged'
                AND merged_into_id IS NOT NULL
                AND merged_into_id <> id)
            OR
            (resolution_status <> 'merged' AND merged_into_id IS NULL)
        ),
    CONSTRAINT ck_organizations_confidence
        CHECK (confidence IS NULL OR (confidence >= 0 AND confidence <= 1)),
    CONSTRAINT ck_organizations_version_positive CHECK (version >= 1)
);

CREATE INDEX idx_organizations_normalized_name
    ON public.organizations (normalized_name);

CREATE UNIQUE INDEX ux_organizations_confirmed_identity_key
    ON public.organizations (identity_key)
    WHERE resolution_status = 'confirmed'
      AND identity_key IS NOT NULL
      AND BTRIM(identity_key) <> '';

CREATE TABLE public.event_series (
    id                      UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    canonical_name          TEXT NOT NULL,
    normalized_name         TEXT NOT NULL,
    series_key              TEXT NULL,
    activity_category       TEXT NULL,
    recurrence_type         TEXT NOT NULL DEFAULT 'unknown',
    cycle_months            SMALLINT NULL,
    usual_start_month       SMALLINT NULL,
    next_expected_start     DATE NULL,
    prediction_confidence   NUMERIC(5, 4) NULL,
    prediction_as_of        DATE NULL,
    resolution_status       TEXT NOT NULL DEFAULT 'proposed',
    merged_into_id          UUID NULL REFERENCES public.event_series(id),
    version                 BIGINT NOT NULL DEFAULT 1,
    created_at              TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at              TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT ck_event_series_canonical_name_not_blank
        CHECK (BTRIM(canonical_name) <> ''),
    CONSTRAINT ck_event_series_normalized_name_not_blank
        CHECK (BTRIM(normalized_name) <> ''),
    CONSTRAINT ck_event_series_key_not_blank
        CHECK (series_key IS NULL OR BTRIM(series_key) <> ''),
    CONSTRAINT ck_event_series_recurrence_type
        CHECK (recurrence_type IN (
            'annual', 'biennial', 'quarterly', 'monthly', 'irregular', 'unknown'
        )),
    CONSTRAINT ck_event_series_cycle_months_positive
        CHECK (cycle_months IS NULL OR cycle_months > 0),
    CONSTRAINT ck_event_series_usual_start_month
        CHECK (usual_start_month IS NULL OR usual_start_month BETWEEN 1 AND 12),
    CONSTRAINT ck_event_series_prediction_confidence
        CHECK (
            prediction_confidence IS NULL
            OR (prediction_confidence >= 0 AND prediction_confidence <= 1)
        ),
    CONSTRAINT ck_event_series_resolution_status
        CHECK (resolution_status IN ('proposed', 'confirmed', 'merged', 'tombstoned')),
    CONSTRAINT ck_event_series_merge_shape
        CHECK (
            (resolution_status = 'merged'
                AND merged_into_id IS NOT NULL
                AND merged_into_id <> id)
            OR
            (resolution_status <> 'merged' AND merged_into_id IS NULL)
        ),
    CONSTRAINT ck_event_series_version_positive CHECK (version >= 1)
);

CREATE INDEX idx_event_series_normalized_name
    ON public.event_series (normalized_name);

CREATE UNIQUE INDEX ux_event_series_confirmed_series_key
    ON public.event_series (series_key)
    WHERE resolution_status = 'confirmed'
      AND series_key IS NOT NULL
      AND BTRIM(series_key) <> '';

CREATE TABLE public.event_editions (
    id                          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    series_id                   UUID NULL REFERENCES public.event_series(id),
    canonical_name              TEXT NOT NULL,
    normalized_name             TEXT NOT NULL,
    edition_key                 TEXT NULL,
    edition_year                SMALLINT NULL,
    edition_no                  INTEGER NULL,
    activity_category           TEXT NULL,
    region_code                 TEXT NULL,
    region_name                 TEXT NULL,
    activity_stage              TEXT NOT NULL DEFAULT 'unknown',
    registration_started_at     TIMESTAMPTZ NULL,
    registration_ended_at       TIMESTAMPTZ NULL,
    voting_started_at           TIMESTAMPTZ NULL,
    voting_ended_at             TIMESTAMPTZ NULL,
    result_announced_at         TIMESTAMPTZ NULL,
    online_voting_status        TEXT NOT NULL DEFAULT 'unknown',
    voting_platform             TEXT NULL,
    first_observed_at           TIMESTAMPTZ NOT NULL,
    last_observed_at            TIMESTAMPTZ NOT NULL,
    resolution_status           TEXT NOT NULL DEFAULT 'proposed',
    merged_into_id              UUID NULL REFERENCES public.event_editions(id),
    version                     BIGINT NOT NULL DEFAULT 1,
    created_at                  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at                  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT ck_event_editions_canonical_name_not_blank
        CHECK (BTRIM(canonical_name) <> ''),
    CONSTRAINT ck_event_editions_normalized_name_not_blank
        CHECK (BTRIM(normalized_name) <> ''),
    CONSTRAINT ck_event_editions_key_not_blank
        CHECK (edition_key IS NULL OR BTRIM(edition_key) <> ''),
    CONSTRAINT ck_event_editions_edition_year_positive
        CHECK (edition_year IS NULL OR edition_year > 0),
    CONSTRAINT ck_event_editions_edition_no_positive
        CHECK (edition_no IS NULL OR edition_no > 0),
    CONSTRAINT ck_event_editions_activity_stage
        CHECK (activity_stage IN (
            'unknown', 'planning', 'nomination', 'voting', 'judging',
            'result_published', 'ended', 'cancelled'
        )),
    CONSTRAINT ck_event_editions_online_voting_status
        CHECK (online_voting_status IN ('unknown', 'suspect', 'has', 'none')),
    CONSTRAINT ck_event_editions_observed_order
        CHECK (first_observed_at <= last_observed_at),
    CONSTRAINT ck_event_editions_registration_order
        CHECK (
            registration_started_at IS NULL
            OR registration_ended_at IS NULL
            OR registration_started_at <= registration_ended_at
        ),
    CONSTRAINT ck_event_editions_voting_order
        CHECK (
            voting_started_at IS NULL
            OR voting_ended_at IS NULL
            OR voting_started_at <= voting_ended_at
        ),
    CONSTRAINT ck_event_editions_resolution_status
        CHECK (resolution_status IN ('proposed', 'confirmed', 'merged', 'tombstoned')),
    CONSTRAINT ck_event_editions_merge_shape
        CHECK (
            (resolution_status = 'merged'
                AND merged_into_id IS NOT NULL
                AND merged_into_id <> id)
            OR
            (resolution_status <> 'merged' AND merged_into_id IS NULL)
        ),
    CONSTRAINT ck_event_editions_version_positive CHECK (version >= 1)
);

CREATE INDEX idx_event_editions_series
    ON public.event_editions (series_id);

CREATE INDEX idx_event_editions_normalized_name
    ON public.event_editions (normalized_name);

CREATE UNIQUE INDEX ux_event_editions_confirmed_edition_key
    ON public.event_editions (edition_key)
    WHERE resolution_status = 'confirmed'
      AND edition_key IS NOT NULL
      AND BTRIM(edition_key) <> '';

CREATE TABLE public.event_sources (
    event_edition_id    UUID NOT NULL REFERENCES public.event_editions(id),
    source_document_id  UUID NOT NULL,
    collection_mode     TEXT NOT NULL,
    linked_at           TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT pk_event_sources
        PRIMARY KEY (event_edition_id, source_document_id),
    CONSTRAINT uq_event_sources_trigger_identity
        UNIQUE (event_edition_id, source_document_id, collection_mode),
    CONSTRAINT fk_event_sources_document_mode
        FOREIGN KEY (source_document_id, collection_mode)
        REFERENCES public.source_documents(id, collection_mode)
);

CREATE INDEX idx_event_sources_document
    ON public.event_sources (source_document_id, event_edition_id);

CREATE TABLE public.tenant_resource_grants (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id           UUID NOT NULL REFERENCES public.tenants(id),
    event_edition_id    UUID NOT NULL,
    trigger_source_document_id UUID NOT NULL,
    trigger_collection_mode TEXT NOT NULL DEFAULT 'realtime_signal',
    policy              TEXT NOT NULL DEFAULT 'shared_competition',
    policy_version      TEXT NOT NULL,
    status              TEXT NOT NULL DEFAULT 'active',
    grant_source        TEXT NOT NULL,
    granted_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    revoked_at          TIMESTAMPTZ NULL,
    supersedes_grant_id UUID NULL,
    version             BIGINT NOT NULL DEFAULT 1,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT uq_tenant_resource_grants_identity
        UNIQUE (id, tenant_id, event_edition_id),
    CONSTRAINT uq_tenant_resource_grants_policy_snapshot
        UNIQUE (id, tenant_id, event_edition_id, policy_version),
    CONSTRAINT fk_tenant_resource_grants_realtime_source
        FOREIGN KEY (
            event_edition_id,
            trigger_source_document_id,
            trigger_collection_mode
        )
        REFERENCES public.event_sources(
            event_edition_id,
            source_document_id,
            collection_mode
        ),
    CONSTRAINT fk_tenant_resource_grants_supersedes_same_resource
        FOREIGN KEY (supersedes_grant_id, tenant_id, event_edition_id)
        REFERENCES public.tenant_resource_grants(id, tenant_id, event_edition_id),
    CONSTRAINT ck_tenant_resource_grants_trigger_mode
        CHECK (trigger_collection_mode = 'realtime_signal'),
    CONSTRAINT ck_tenant_resource_grants_policy
        CHECK (policy IN (
            'shared_competition', 'tenant_private', 'selected_tenants'
        )),
    CONSTRAINT ck_tenant_resource_grants_policy_version_not_blank
        CHECK (BTRIM(policy_version) <> ''),
    CONSTRAINT ck_tenant_resource_grants_status
        CHECK (status IN ('active', 'revoked', 'superseded')),
    CONSTRAINT ck_tenant_resource_grants_source_not_blank
        CHECK (BTRIM(grant_source) <> ''),
    CONSTRAINT ck_tenant_resource_grants_revocation_shape
        CHECK (
            (status = 'active' AND revoked_at IS NULL)
            OR (status IN ('revoked', 'superseded') AND revoked_at IS NOT NULL)
        ),
    CONSTRAINT ck_tenant_resource_grants_supersedes_not_self
        CHECK (supersedes_grant_id IS NULL OR supersedes_grant_id <> id),
    CONSTRAINT ck_tenant_resource_grants_version_positive CHECK (version >= 1)
);

CREATE UNIQUE INDEX ux_tenant_resource_grants_one_active
    ON public.tenant_resource_grants (tenant_id, event_edition_id)
    WHERE status = 'active' AND revoked_at IS NULL;

CREATE INDEX idx_tenant_resource_grants_tenant_status
    ON public.tenant_resource_grants (tenant_id, status, event_edition_id);

CREATE OR REPLACE FUNCTION public.app_lock_active_review_grant(
    p_tenant_id UUID,
    p_grant_id UUID
)
RETURNS TABLE (
    grant_id UUID,
    event_edition_id UUID,
    policy_version TEXT
)
LANGUAGE sql
VOLATILE
SECURITY DEFINER
SET search_path = pg_catalog
AS $$
    SELECT grant_row.id, grant_row.event_edition_id, grant_row.policy_version
    FROM public.tenant_resource_grants AS grant_row
    WHERE grant_row.id = p_grant_id
      AND grant_row.tenant_id = p_tenant_id
      AND p_tenant_id = public.app_current_tenant_id()
      AND grant_row.status = 'active'
      AND grant_row.revoked_at IS NULL
    FOR SHARE OF grant_row;
$$;

COMMENT ON FUNCTION public.app_lock_active_review_grant(UUID, UUID) IS
    'Lock one active in-scope review grant without granting runtime UPDATE on control data.';

REVOKE ALL ON FUNCTION public.app_lock_active_review_grant(UUID, UUID) FROM PUBLIC;

CREATE TABLE public.tenant_candidates (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id           UUID NOT NULL REFERENCES public.tenants(id),
    grant_id            UUID NOT NULL,
    event_edition_id    UUID NOT NULL,
    candidate_status    TEXT NOT NULL DEFAULT 'open',
    score               NUMERIC(8, 4) NULL,
    score_version       TEXT NULL,
    generated_at        TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    version             BIGINT NOT NULL DEFAULT 1,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT uq_tenant_candidates_tenant_edition
        UNIQUE (tenant_id, event_edition_id),
    CONSTRAINT uq_tenant_candidates_review_identity
        UNIQUE (id, tenant_id, event_edition_id, grant_id),
    CONSTRAINT fk_tenant_candidates_grant_scope
        FOREIGN KEY (grant_id, tenant_id, event_edition_id)
        REFERENCES public.tenant_resource_grants(id, tenant_id, event_edition_id),
    CONSTRAINT ck_tenant_candidates_status
        CHECK (candidate_status IN ('open', 'in_review', 'closed', 'withdrawn')),
    CONSTRAINT ck_tenant_candidates_score_version_not_blank
        CHECK (score_version IS NULL OR BTRIM(score_version) <> ''),
    CONSTRAINT ck_tenant_candidates_version_positive CHECK (version >= 1)
);

CREATE INDEX idx_tenant_candidates_queue
    ON public.tenant_candidates (tenant_id, candidate_status, generated_at, id);

CREATE TABLE public.tenant_reviews (
    id                      UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id               UUID NOT NULL REFERENCES public.tenants(id),
    candidate_id            UUID NOT NULL,
    event_edition_id        UUID NOT NULL,
    grant_id                UUID NOT NULL,
    grant_policy_version    TEXT NOT NULL,
    review_round            INTEGER NOT NULL DEFAULT 1,
    review_status           TEXT NOT NULL,
    review_decision         TEXT NULL,
    disposition             TEXT NULL,
    reason_code             TEXT NULL,
    reason_schema_version   TEXT NULL,
    field_overrides         JSONB NOT NULL DEFAULT '{}'::JSONB,
    reviewer_note           TEXT NULL,
    reviewer_user_id        UUID NULL,
    ai_assessment_id        UUID NULL,
    rule_version            TEXT NULL,
    started_at              TIMESTAMPTZ NULL,
    completed_at            TIMESTAMPTZ NULL,
    cancel_reason           TEXT NULL,
    cancelled_at            TIMESTAMPTZ NULL,
    supersedes_review_id    UUID NULL,
    version                 BIGINT NOT NULL DEFAULT 1,
    created_at              TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at              TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT uq_tenant_reviews_round
        UNIQUE (tenant_id, candidate_id, review_round),
    CONSTRAINT uq_tenant_reviews_supersession_identity
        UNIQUE (id, tenant_id, candidate_id),
    CONSTRAINT fk_tenant_reviews_candidate_scope
        FOREIGN KEY (candidate_id, tenant_id, event_edition_id, grant_id)
        REFERENCES public.tenant_candidates(id, tenant_id, event_edition_id, grant_id),
    CONSTRAINT fk_tenant_reviews_grant_snapshot
        FOREIGN KEY (grant_id, tenant_id, event_edition_id, grant_policy_version)
        REFERENCES public.tenant_resource_grants(
            id, tenant_id, event_edition_id, policy_version
        ),
    CONSTRAINT fk_tenant_reviews_reviewer_membership
        FOREIGN KEY (tenant_id, reviewer_user_id)
        REFERENCES public.tenant_memberships(tenant_id, user_id),
    CONSTRAINT fk_tenant_reviews_supersedes_same_candidate
        FOREIGN KEY (supersedes_review_id, tenant_id, candidate_id)
        REFERENCES public.tenant_reviews(id, tenant_id, candidate_id),
    CONSTRAINT ck_tenant_reviews_round_positive CHECK (review_round >= 1),
    CONSTRAINT ck_tenant_reviews_status
        CHECK (review_status IN ('pending', 'in_review', 'completed', 'cancelled')),
    CONSTRAINT ck_tenant_reviews_decision
        CHECK (
            review_decision IS NULL
            OR review_decision IN ('qualified', 'rejected', 'needs_more_info')
        ),
    CONSTRAINT ck_tenant_reviews_disposition
        CHECK (
            disposition IS NULL
            OR disposition IN ('sales_handoff', 'nurture', 'competitor_watch', 'archive')
        ),
    CONSTRAINT ck_tenant_reviews_field_overrides_object
        CHECK (JSONB_TYPEOF(field_overrides) = 'object'),
    CONSTRAINT ck_tenant_reviews_lifecycle_shape
        CHECK (
            (review_status = 'pending'
                AND started_at IS NULL
                AND completed_at IS NULL
                AND cancelled_at IS NULL
                AND cancel_reason IS NULL
                AND review_decision IS NULL
                AND disposition IS NULL)
            OR
            (review_status = 'in_review'
                AND started_at IS NOT NULL
                AND completed_at IS NULL
                AND cancelled_at IS NULL
                AND cancel_reason IS NULL
                AND reviewer_user_id IS NOT NULL
                AND review_decision IS NULL
                AND disposition IS NULL)
            OR
            (review_status = 'completed'
                AND started_at IS NOT NULL
                AND completed_at IS NOT NULL
                AND cancelled_at IS NULL
                AND cancel_reason IS NULL
                AND reviewer_user_id IS NOT NULL
                AND review_decision IS NOT NULL
                AND disposition IS NOT NULL)
            OR
            (review_status = 'cancelled'
                AND completed_at IS NULL
                AND cancelled_at IS NOT NULL
                AND cancel_reason IS NOT NULL
                AND BTRIM(cancel_reason) <> ''
                AND review_decision IS NULL
                AND disposition IS NULL)
        ),
    CONSTRAINT ck_tenant_reviews_completed_order
        CHECK (completed_at IS NULL OR started_at <= completed_at),
    CONSTRAINT ck_tenant_reviews_supersedes_not_self
        CHECK (supersedes_review_id IS NULL OR supersedes_review_id <> id),
    CONSTRAINT ck_tenant_reviews_version_positive CHECK (version >= 1)
);

CREATE UNIQUE INDEX ux_tenant_reviews_one_active
    ON public.tenant_reviews (tenant_id, candidate_id)
    WHERE review_status IN ('pending', 'in_review');

CREATE INDEX idx_tenant_reviews_tenant_status
    ON public.tenant_reviews (tenant_id, review_status, created_at, id);

CREATE OR REPLACE FUNCTION public.app_guard_completed_tenant_review()
RETURNS TRIGGER
LANGUAGE plpgsql
SECURITY INVOKER
SET search_path = pg_catalog
AS $$
BEGIN
    IF OLD.review_status IN ('completed', 'cancelled') THEN
        RAISE EXCEPTION 'terminal tenant review % is immutable', OLD.id
            USING ERRCODE = 'check_violation';
    END IF;

    IF TG_OP = 'DELETE' THEN
        RETURN OLD;
    END IF;
    RETURN NEW;
END;
$$;

CREATE TRIGGER trg_guard_completed_tenant_review
    BEFORE UPDATE OR DELETE ON public.tenant_reviews
    FOR EACH ROW
    EXECUTE FUNCTION public.app_guard_completed_tenant_review();

CREATE OR REPLACE FUNCTION public.app_withdraw_revoked_grant_review()
RETURNS TRIGGER
LANGUAGE plpgsql
SECURITY INVOKER
SET search_path = pg_catalog
AS $$
DECLARE
    affected_candidate_id UUID;
BEGIN
    IF OLD.status = 'active'
       AND NEW.status IN ('revoked', 'superseded')
    THEN
        SELECT candidate.id
        INTO affected_candidate_id
        FROM public.tenant_candidates AS candidate
        WHERE candidate.tenant_id = NEW.tenant_id
          AND candidate.event_edition_id = NEW.event_edition_id
        FOR UPDATE;

        IF affected_candidate_id IS NOT NULL THEN
            PERFORM review.id
            FROM public.tenant_reviews AS review
            WHERE review.tenant_id = NEW.tenant_id
              AND review.candidate_id = affected_candidate_id
              AND review.review_status IN ('pending', 'in_review')
            ORDER BY review.review_round DESC
            FOR UPDATE;

            UPDATE public.tenant_reviews
            SET review_status = 'cancelled',
                cancel_reason = 'grant_revoked',
                cancelled_at = NOW(),
                version = version + 1,
                updated_at = NOW()
            WHERE tenant_id = NEW.tenant_id
              AND candidate_id = affected_candidate_id
              AND review_status IN ('pending', 'in_review');

            UPDATE public.tenant_candidates
            SET candidate_status = 'withdrawn',
                version = version + 1,
                updated_at = NOW()
            WHERE id = affected_candidate_id
              AND tenant_id = NEW.tenant_id
              AND candidate_status <> 'withdrawn';
        END IF;
    END IF;

    RETURN NEW;
END;
$$;

CREATE TRIGGER trg_withdraw_revoked_grant_review
    AFTER UPDATE OF status ON public.tenant_resource_grants
    FOR EACH ROW
    EXECUTE FUNCTION public.app_withdraw_revoked_grant_review();

CREATE TABLE public.domain_outbox (
    message_id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id           UUID NOT NULL REFERENCES public.tenants(id),
    event_type          TEXT NOT NULL,
    schema_version      TEXT NOT NULL,
    aggregate_type      TEXT NOT NULL,
    aggregate_id        UUID NOT NULL,
    aggregate_version   BIGINT NOT NULL,
    occurred_at         TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    correlation_id      UUID NOT NULL,
    causation_id        UUID NULL,
    payload             JSONB NOT NULL DEFAULT '{}'::JSONB,
    status              TEXT NOT NULL DEFAULT 'pending',
    attempt_count       INTEGER NOT NULL DEFAULT 0,
    available_at        TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    published_at        TIMESTAMPTZ NULL,
    last_error          TEXT NULL,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT uq_domain_outbox_aggregate_version
        UNIQUE (tenant_id, event_type, aggregate_id, aggregate_version),
    CONSTRAINT ck_domain_outbox_event_type_not_blank CHECK (BTRIM(event_type) <> ''),
    CONSTRAINT ck_domain_outbox_schema_version_not_blank
        CHECK (BTRIM(schema_version) <> ''),
    CONSTRAINT ck_domain_outbox_aggregate_type_not_blank
        CHECK (BTRIM(aggregate_type) <> ''),
    CONSTRAINT ck_domain_outbox_aggregate_version_positive
        CHECK (aggregate_version >= 1),
    CONSTRAINT ck_domain_outbox_payload_object
        CHECK (JSONB_TYPEOF(payload) = 'object'),
    CONSTRAINT ck_domain_outbox_status
        CHECK (status IN ('pending', 'published', 'failed')),
    CONSTRAINT ck_domain_outbox_attempt_count_nonnegative CHECK (attempt_count >= 0),
    CONSTRAINT ck_domain_outbox_publication_shape
        CHECK (
            (status = 'published' AND published_at IS NOT NULL)
            OR (status <> 'published' AND published_at IS NULL)
        )
);

CREATE INDEX idx_domain_outbox_publishable
    ON public.domain_outbox (available_at, occurred_at, message_id)
    WHERE status IN ('pending', 'failed');

CREATE TABLE public.tenant_command_receipts (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id           UUID NOT NULL REFERENCES public.tenants(id),
    command_name        TEXT NOT NULL,
    idempotency_key     TEXT NOT NULL,
    request_hash        TEXT NOT NULL,
    response_json       JSONB NULL,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT uq_tenant_command_receipts_idempotency
        UNIQUE (tenant_id, command_name, idempotency_key),
    CONSTRAINT ck_tenant_command_receipts_command_not_blank
        CHECK (BTRIM(command_name) <> ''),
    CONSTRAINT ck_tenant_command_receipts_key_not_blank
        CHECK (BTRIM(idempotency_key) <> ''),
    CONSTRAINT ck_tenant_command_receipts_request_hash
        CHECK (request_hash ~ '^[0-9a-f]{64}$')
);

CREATE INDEX idx_tenant_command_receipts_tenant_created
    ON public.tenant_command_receipts (tenant_id, created_at DESC, id);

-- Tenant-owned rows fail closed without a transaction-local app.tenant_id.
ALTER TABLE public.tenant_resource_grants ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.tenant_resource_grants FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_resource_grants_tenant_isolation
    ON public.tenant_resource_grants
    AS PERMISSIVE FOR ALL TO PUBLIC
    USING (tenant_id = public.app_current_tenant_id())
    WITH CHECK (tenant_id = public.app_current_tenant_id());

ALTER TABLE public.tenant_candidates ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.tenant_candidates FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_candidates_tenant_isolation
    ON public.tenant_candidates
    AS PERMISSIVE FOR ALL TO PUBLIC
    USING (tenant_id = public.app_current_tenant_id())
    WITH CHECK (tenant_id = public.app_current_tenant_id());

ALTER TABLE public.tenant_reviews ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.tenant_reviews FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_reviews_tenant_isolation
    ON public.tenant_reviews
    AS PERMISSIVE FOR ALL TO PUBLIC
    USING (tenant_id = public.app_current_tenant_id())
    WITH CHECK (tenant_id = public.app_current_tenant_id());

ALTER TABLE public.domain_outbox ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.domain_outbox FORCE ROW LEVEL SECURITY;
CREATE POLICY domain_outbox_tenant_isolation
    ON public.domain_outbox
    AS PERMISSIVE FOR ALL TO PUBLIC
    USING (tenant_id = public.app_current_tenant_id())
    WITH CHECK (tenant_id = public.app_current_tenant_id());

ALTER TABLE public.tenant_command_receipts ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.tenant_command_receipts FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_command_receipts_tenant_isolation
    ON public.tenant_command_receipts
    AS PERMISSIVE FOR ALL TO PUBLIC
    USING (tenant_id = public.app_current_tenant_id())
    WITH CHECK (tenant_id = public.app_current_tenant_id());
