-- 024: immutable review rules, explicit tenant activation, snapshots and safe reopen.
-- Expand-only. No real tenant activation or production review data is created.

CREATE TABLE public.review_rulesets (
    version                 TEXT PRIMARY KEY,
    schema_version          TEXT NOT NULL,
    definition_sha256       TEXT NOT NULL,
    definition              JSONB NOT NULL,
    max_review_rounds       INTEGER NOT NULL,
    approval_reference      TEXT NOT NULL,
    created_at              TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT uq_review_rulesets_snapshot
        UNIQUE (version, definition_sha256),
    CONSTRAINT ck_review_rulesets_version
        CHECK (version ~ '^review-rules/[1-9][0-9]*\.[0-9]+\.[0-9]+$'),
    CONSTRAINT ck_review_rulesets_schema_not_blank
        CHECK (BTRIM(schema_version) <> ''),
    CONSTRAINT ck_review_rulesets_hash
        CHECK (definition_sha256 ~ '^[0-9a-f]{64}$'),
    CONSTRAINT ck_review_rulesets_definition_object
        CHECK (
            JSONB_TYPEOF(definition) = 'object'
            AND definition ->> 'version' = version
            AND definition ->> 'schema_version' = schema_version
        ),
    CONSTRAINT ck_review_rulesets_rounds
        CHECK (
            max_review_rounds BETWEEN 2 AND 10
            AND definition #>> '{reopen,max_rounds}' IS NOT NULL
            AND definition #>> '{reopen,max_rounds}' ~ '^[0-9]+$'
            AND max_review_rounds =
                (definition #>> '{reopen,max_rounds}')::INTEGER
        ),
    CONSTRAINT ck_review_rulesets_approval_not_blank
        CHECK (BTRIM(approval_reference) <> '')
);

CREATE TABLE public.review_ruleset_completion_reasons (
    ruleset_version             TEXT NOT NULL,
    reason_code                 TEXT NOT NULL,
    review_decision             TEXT NOT NULL,
    disposition                 TEXT NOT NULL,
    requires_reopen_not_before  BOOLEAN NOT NULL DEFAULT FALSE,
    requires_note               BOOLEAN NOT NULL DEFAULT FALSE,
    required_capability         TEXT NULL,
    PRIMARY KEY (ruleset_version, reason_code),
    CONSTRAINT uq_review_ruleset_completion_matrix
        UNIQUE (
            ruleset_version, reason_code, review_decision, disposition
        ),
    CONSTRAINT fk_review_ruleset_completion_ruleset
        FOREIGN KEY (ruleset_version)
        REFERENCES public.review_rulesets(version),
    CONSTRAINT ck_review_ruleset_completion_code
        CHECK (reason_code ~ '^[a-z][a-z0-9_]{2,63}$'),
    CONSTRAINT ck_review_ruleset_completion_decision
        CHECK (review_decision IN (
            'qualified', 'rejected', 'needs_more_info'
        )),
    CONSTRAINT ck_review_ruleset_completion_disposition
        CHECK (disposition IN (
            'sales_handoff', 'nurture', 'competitor_watch', 'archive'
        )),
    CONSTRAINT ck_review_ruleset_completion_matrix
        CHECK ((review_decision, disposition) IN (
            ('qualified', 'sales_handoff'),
            ('qualified', 'nurture'),
            ('qualified', 'competitor_watch'),
            ('rejected', 'archive'),
            ('rejected', 'competitor_watch'),
            ('needs_more_info', 'nurture')
        )),
    CONSTRAINT ck_review_ruleset_completion_capability
        CHECK (
            (disposition = 'sales_handoff'
                AND required_capability = 'opportunity_atomic_create')
            OR
            (disposition <> 'sales_handoff' AND required_capability IS NULL)
        )
);

CREATE TABLE public.review_ruleset_reopen_reasons (
    ruleset_version                 TEXT NOT NULL,
    reason_code                     TEXT NOT NULL,
    requires_new_realtime_source    BOOLEAN NOT NULL,
    PRIMARY KEY (ruleset_version, reason_code),
    CONSTRAINT fk_review_ruleset_reopen_ruleset
        FOREIGN KEY (ruleset_version)
        REFERENCES public.review_rulesets(version),
    CONSTRAINT ck_review_ruleset_reopen_code
        CHECK (reason_code ~ '^[a-z][a-z0-9_]{2,63}$')
);

CREATE OR REPLACE FUNCTION public.app_reject_immutable_review_rule()
RETURNS TRIGGER
LANGUAGE plpgsql
SECURITY INVOKER
SET search_path = pg_catalog
AS $$
BEGIN
    RAISE EXCEPTION 'published review rule rows are immutable'
        USING ERRCODE = 'check_violation';
END;
$$;

CREATE OR REPLACE FUNCTION public.app_validate_completion_reason_publish()
RETURNS TRIGGER
LANGUAGE plpgsql
SECURITY INVOKER
SET search_path = pg_catalog
AS $$
DECLARE
    expected_rule JSONB;
BEGIN
    SELECT ruleset.definition -> 'completion_reasons' -> NEW.reason_code
    INTO expected_rule
    FROM public.review_rulesets AS ruleset
    WHERE ruleset.version = NEW.ruleset_version;

    IF expected_rule IS NULL OR expected_rule IS DISTINCT FROM JSONB_BUILD_OBJECT(
        'decision', NEW.review_decision,
        'disposition', NEW.disposition,
        'requires_reopen_not_before', NEW.requires_reopen_not_before,
        'requires_note', NEW.requires_note,
        'required_capability', NEW.required_capability
    ) THEN
        RAISE EXCEPTION 'completion reason differs from ruleset definition'
            USING ERRCODE = 'check_violation';
    END IF;
    RETURN NEW;
END;
$$;

CREATE OR REPLACE FUNCTION public.app_validate_reopen_reason_publish()
RETURNS TRIGGER
LANGUAGE plpgsql
SECURITY INVOKER
SET search_path = pg_catalog
AS $$
DECLARE
    expected_rule JSONB;
BEGIN
    SELECT ruleset.definition -> 'reopen' -> 'reasons' -> NEW.reason_code
    INTO expected_rule
    FROM public.review_rulesets AS ruleset
    WHERE ruleset.version = NEW.ruleset_version;

    IF expected_rule IS NULL OR expected_rule IS DISTINCT FROM JSONB_BUILD_OBJECT(
        'requires_new_realtime_source', NEW.requires_new_realtime_source
    ) THEN
        RAISE EXCEPTION 'reopen reason differs from ruleset definition'
            USING ERRCODE = 'check_violation';
    END IF;
    RETURN NEW;
END;
$$;

CREATE TRIGGER trg_review_rulesets_immutable
    BEFORE UPDATE OR DELETE ON public.review_rulesets
    FOR EACH ROW EXECUTE FUNCTION public.app_reject_immutable_review_rule();

CREATE TRIGGER trg_review_ruleset_completion_immutable
    BEFORE UPDATE OR DELETE ON public.review_ruleset_completion_reasons
    FOR EACH ROW EXECUTE FUNCTION public.app_reject_immutable_review_rule();

CREATE TRIGGER trg_review_ruleset_completion_publish
    BEFORE INSERT ON public.review_ruleset_completion_reasons
    FOR EACH ROW EXECUTE FUNCTION public.app_validate_completion_reason_publish();

CREATE TRIGGER trg_review_ruleset_reopen_immutable
    BEFORE UPDATE OR DELETE ON public.review_ruleset_reopen_reasons
    FOR EACH ROW EXECUTE FUNCTION public.app_reject_immutable_review_rule();

CREATE TRIGGER trg_review_ruleset_reopen_publish
    BEFORE INSERT ON public.review_ruleset_reopen_reasons
    FOR EACH ROW EXECUTE FUNCTION public.app_validate_reopen_reason_publish();

CREATE TABLE public.tenant_review_ruleset_activations (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id           UUID NOT NULL REFERENCES public.tenants(id),
    ruleset_version     TEXT NOT NULL,
    ruleset_sha256      TEXT NOT NULL,
    activation_reference TEXT NOT NULL,
    activated_at        TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    deactivated_at      TIMESTAMPTZ NULL,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT uq_tenant_review_ruleset_activation_identity
        UNIQUE (id, tenant_id, ruleset_version, ruleset_sha256),
    CONSTRAINT fk_tenant_review_ruleset_activation_snapshot
        FOREIGN KEY (ruleset_version, ruleset_sha256)
        REFERENCES public.review_rulesets(version, definition_sha256),
    CONSTRAINT ck_tenant_review_ruleset_activation_reference
        CHECK (BTRIM(activation_reference) <> ''),
    CONSTRAINT ck_tenant_review_ruleset_activation_order
        CHECK (deactivated_at IS NULL OR activated_at <= deactivated_at)
);

CREATE UNIQUE INDEX ux_tenant_review_ruleset_one_active
    ON public.tenant_review_ruleset_activations (tenant_id)
    WHERE deactivated_at IS NULL;

CREATE OR REPLACE FUNCTION public.app_guard_review_ruleset_activation()
RETURNS TRIGGER
LANGUAGE plpgsql
SECURITY INVOKER
SET search_path = pg_catalog
AS $$
DECLARE
    definition_row JSONB;
    completion_reason_count INTEGER;
    reopen_reason_count INTEGER;
BEGIN
    IF TG_OP = 'INSERT' THEN
        SELECT ruleset.definition,
               (
                    SELECT COUNT(*)::INTEGER
                    FROM JSONB_OBJECT_KEYS(
                        ruleset.definition -> 'completion_reasons'
                    )
               ),
               (
                    SELECT COUNT(*)::INTEGER
                    FROM JSONB_OBJECT_KEYS(
                        ruleset.definition -> 'reopen' -> 'reasons'
                    )
               )
        INTO definition_row, completion_reason_count, reopen_reason_count
        FROM public.review_rulesets AS ruleset
        WHERE ruleset.version = NEW.ruleset_version
          AND ruleset.definition_sha256 = NEW.ruleset_sha256;

        IF definition_row IS NULL
           OR completion_reason_count IS DISTINCT FROM (
                SELECT COUNT(*)::INTEGER
                FROM public.review_ruleset_completion_reasons AS reason
                WHERE reason.ruleset_version = NEW.ruleset_version
           )
           OR reopen_reason_count IS DISTINCT FROM (
                SELECT COUNT(*)::INTEGER
                FROM public.review_ruleset_reopen_reasons AS reason
                WHERE reason.ruleset_version = NEW.ruleset_version
           )
        THEN
            RAISE EXCEPTION 'review ruleset cannot activate before complete publish'
                USING ERRCODE = 'check_violation';
        END IF;
        RETURN NEW;
    END IF;

    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION 'review ruleset activation history is immutable'
            USING ERRCODE = 'check_violation';
    END IF;

    IF OLD.deactivated_at IS NULL
       AND NEW.deactivated_at IS NOT NULL
       AND NEW.id = OLD.id
       AND NEW.tenant_id = OLD.tenant_id
       AND NEW.ruleset_version = OLD.ruleset_version
       AND NEW.ruleset_sha256 = OLD.ruleset_sha256
       AND NEW.activation_reference = OLD.activation_reference
       AND NEW.activated_at = OLD.activated_at
       AND NEW.created_at = OLD.created_at
    THEN
        RETURN NEW;
    END IF;

    RAISE EXCEPTION 'review ruleset activation history is immutable'
        USING ERRCODE = 'check_violation';
END;
$$;

CREATE TRIGGER trg_guard_review_ruleset_activation
    BEFORE INSERT OR UPDATE OR DELETE ON public.tenant_review_ruleset_activations
    FOR EACH ROW EXECUTE FUNCTION public.app_guard_review_ruleset_activation();

ALTER TABLE public.tenant_review_ruleset_activations ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.tenant_review_ruleset_activations FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_review_ruleset_activations_isolation
    ON public.tenant_review_ruleset_activations
    AS PERMISSIVE FOR ALL TO PUBLIC
    USING (tenant_id = public.app_current_tenant_id())
    WITH CHECK (tenant_id = public.app_current_tenant_id());

CREATE OR REPLACE FUNCTION public.app_lock_active_review_ruleset(
    requested_tenant_id UUID
)
RETURNS TABLE (
    activation_id UUID,
    ruleset_version TEXT,
    ruleset_sha256 TEXT,
    definition JSONB
)
LANGUAGE sql
VOLATILE
SECURITY DEFINER
SET search_path = pg_catalog
AS $$
    SELECT activation.id,
           ruleset.version,
           ruleset.definition_sha256,
           ruleset.definition
    FROM public.tenant_review_ruleset_activations AS activation
    JOIN public.review_rulesets AS ruleset
      ON ruleset.version = activation.ruleset_version
     AND ruleset.definition_sha256 = activation.ruleset_sha256
    WHERE activation.tenant_id = requested_tenant_id
      AND requested_tenant_id = public.app_current_tenant_id()
      AND activation.deactivated_at IS NULL
    FOR SHARE OF activation
$$;

REVOKE ALL ON FUNCTION public.app_lock_active_review_ruleset(UUID) FROM PUBLIC;

ALTER TABLE public.tenant_reviews
    ADD COLUMN rule_activation_id UUID NULL,
    ADD COLUMN rule_definition_sha256 TEXT NULL,
    ADD COLUMN rule_snapshot JSONB NULL,
    ADD COLUMN reopen_reason_code TEXT NULL,
    ADD COLUMN reopen_trigger_source_document_id UUID NULL,
    ADD COLUMN reopen_not_before TIMESTAMPTZ NULL;

ALTER TABLE public.tenant_reviews
    ADD CONSTRAINT fk_tenant_reviews_rule_activation
        FOREIGN KEY (
            rule_activation_id, tenant_id, rule_version, rule_definition_sha256
        )
        REFERENCES public.tenant_review_ruleset_activations(
            id, tenant_id, ruleset_version, ruleset_sha256
        ),
    ADD CONSTRAINT fk_tenant_reviews_rule_snapshot
        FOREIGN KEY (rule_version, rule_definition_sha256)
        REFERENCES public.review_rulesets(version, definition_sha256),
    ADD CONSTRAINT fk_tenant_reviews_completion_reason
        FOREIGN KEY (
            rule_version, reason_code, review_decision, disposition
        )
        REFERENCES public.review_ruleset_completion_reasons(
            ruleset_version, reason_code, review_decision, disposition
        ),
    ADD CONSTRAINT fk_tenant_reviews_reopen_reason
        FOREIGN KEY (rule_version, reopen_reason_code)
        REFERENCES public.review_ruleset_reopen_reasons(
            ruleset_version, reason_code
        ),
    ADD CONSTRAINT fk_tenant_reviews_reopen_source
        FOREIGN KEY (reopen_trigger_source_document_id)
        REFERENCES public.source_documents(id),
    ADD CONSTRAINT ck_tenant_reviews_rule_snapshot_shape
        CHECK (
            (rule_activation_id IS NULL
                AND rule_version IS NULL
                AND rule_definition_sha256 IS NULL
                AND rule_snapshot IS NULL)
            OR
            (rule_activation_id IS NOT NULL
                AND rule_version IS NOT NULL
                AND rule_definition_sha256 IS NOT NULL
                AND rule_snapshot IS NOT NULL
                AND JSONB_TYPEOF(rule_snapshot) = 'object')
        ),
    ADD CONSTRAINT ck_tenant_reviews_reason_schema_matches_rule
        CHECK (
            reason_schema_version IS NULL
            OR reason_schema_version = rule_version
        ),
    ADD CONSTRAINT ck_tenant_reviews_reopen_reason_shape
        CHECK (
            (review_round = 1
                AND reopen_reason_code IS NULL
                AND reopen_trigger_source_document_id IS NULL)
            OR
            (review_round > 1 AND reopen_reason_code IS NOT NULL)
        );

CREATE OR REPLACE FUNCTION public.app_validate_versioned_tenant_review()
RETURNS TRIGGER
LANGUAGE plpgsql
SECURITY INVOKER
SET search_path = pg_catalog
AS $$
DECLARE
    stored_definition JSONB;
    stored_max_rounds INTEGER;
    reason_requires_time BOOLEAN;
    reason_requires_note BOOLEAN;
    reason_capability TEXT;
    reopen_requires_source BOOLEAN;
    prior_review public.tenant_reviews%ROWTYPE;
BEGIN
    -- Legacy rows created before 024 remain truthful and unversioned. New active
    -- reviews must always carry a complete server-derived ruleset snapshot.
    IF TG_OP = 'INSERT' AND NEW.rule_activation_id IS NULL
    THEN
        RAISE EXCEPTION 'new tenant review requires a versioned ruleset snapshot'
            USING ERRCODE = 'check_violation';
    END IF;

    IF TG_OP = 'UPDATE' AND (
        OLD.id IS DISTINCT FROM NEW.id
        OR OLD.tenant_id IS DISTINCT FROM NEW.tenant_id
        OR OLD.candidate_id IS DISTINCT FROM NEW.candidate_id
        OR OLD.event_edition_id IS DISTINCT FROM NEW.event_edition_id
        OR OLD.grant_id IS DISTINCT FROM NEW.grant_id
        OR OLD.grant_policy_version IS DISTINCT FROM NEW.grant_policy_version
        OR OLD.review_round IS DISTINCT FROM NEW.review_round
        OR OLD.reviewer_user_id IS DISTINCT FROM NEW.reviewer_user_id
        OR OLD.started_at IS DISTINCT FROM NEW.started_at
        OR OLD.supersedes_review_id IS DISTINCT FROM NEW.supersedes_review_id
        OR OLD.rule_activation_id IS DISTINCT FROM NEW.rule_activation_id
        OR OLD.rule_version IS DISTINCT FROM NEW.rule_version
        OR OLD.rule_definition_sha256 IS DISTINCT FROM NEW.rule_definition_sha256
        OR OLD.rule_snapshot IS DISTINCT FROM NEW.rule_snapshot
        OR OLD.reopen_reason_code IS DISTINCT FROM NEW.reopen_reason_code
        OR OLD.reopen_trigger_source_document_id IS DISTINCT FROM
            NEW.reopen_trigger_source_document_id
    ) THEN
        RAISE EXCEPTION 'tenant review rule and reopen provenance are immutable'
            USING ERRCODE = 'check_violation';
    END IF;

    IF NEW.rule_activation_id IS NOT NULL THEN
        SELECT ruleset.definition, ruleset.max_review_rounds
        INTO stored_definition, stored_max_rounds
        FROM public.review_rulesets AS ruleset
        WHERE ruleset.version = NEW.rule_version
          AND ruleset.definition_sha256 = NEW.rule_definition_sha256;

        IF stored_definition IS NULL OR NEW.rule_snapshot IS DISTINCT FROM stored_definition THEN
            RAISE EXCEPTION 'tenant review ruleset snapshot does not match catalog'
                USING ERRCODE = 'check_violation';
        END IF;
    END IF;

    IF NEW.review_status = 'completed'
       AND NEW.rule_activation_id IS NULL
       AND (TG_OP = 'INSERT' OR OLD.review_status IS DISTINCT FROM 'completed')
    THEN
        RAISE EXCEPTION 'legacy unversioned review cannot be newly completed'
            USING ERRCODE = 'check_violation';
    END IF;

    IF TG_OP = 'INSERT' THEN
        IF NOT EXISTS (
            SELECT 1
            FROM public.tenant_review_ruleset_activations AS activation
            WHERE activation.id = NEW.rule_activation_id
              AND activation.tenant_id = NEW.tenant_id
              AND activation.ruleset_version = NEW.rule_version
              AND activation.ruleset_sha256 = NEW.rule_definition_sha256
              AND activation.deactivated_at IS NULL
        ) THEN
            RAISE EXCEPTION 'new tenant review requires an active ruleset activation'
                USING ERRCODE = 'check_violation';
        END IF;
        IF NOT EXISTS (
            SELECT 1
            FROM public.tenant_resource_grants AS grant_row
            WHERE grant_row.id = NEW.grant_id
              AND grant_row.tenant_id = NEW.tenant_id
              AND grant_row.event_edition_id = NEW.event_edition_id
              AND grant_row.status = 'active'
              AND grant_row.revoked_at IS NULL
        ) THEN
            RAISE EXCEPTION 'tenant review requires an active grant'
                USING ERRCODE = 'check_violation';
        END IF;

        IF NEW.review_round = 1 THEN
            IF NEW.supersedes_review_id IS NOT NULL THEN
                RAISE EXCEPTION 'first review round cannot supersede another review'
                    USING ERRCODE = 'check_violation';
            END IF;
        ELSE
            IF NEW.review_round > stored_max_rounds THEN
                RAISE EXCEPTION 'tenant review round exceeds ruleset maximum'
                    USING ERRCODE = 'check_violation';
            END IF;
            SELECT previous.*
            INTO prior_review
            FROM public.tenant_reviews AS previous
            WHERE previous.id = NEW.supersedes_review_id
              AND previous.tenant_id = NEW.tenant_id
              AND previous.candidate_id = NEW.candidate_id
              AND previous.review_status = 'completed';

            IF prior_review.id IS NULL
               OR NEW.review_round <> prior_review.review_round + 1
               OR EXISTS (
                    SELECT 1
                    FROM public.tenant_reviews AS later
                    WHERE later.tenant_id = NEW.tenant_id
                      AND later.candidate_id = NEW.candidate_id
                      AND later.review_round > prior_review.review_round
               )
            THEN
                RAISE EXCEPTION 'tenant review must supersede the latest completed round'
                    USING ERRCODE = 'check_violation';
            END IF;

            SELECT reason.requires_new_realtime_source
            INTO reopen_requires_source
            FROM public.review_ruleset_reopen_reasons AS reason
            WHERE reason.ruleset_version = NEW.rule_version
              AND reason.reason_code = NEW.reopen_reason_code;

            IF NEW.reopen_reason_code = 'scheduled_recheck_due' THEN
                IF prior_review.reopen_not_before IS NULL
                   OR prior_review.reopen_not_before > NOW()
                   OR NEW.reopen_trigger_source_document_id IS NOT NULL
                THEN
                    RAISE EXCEPTION 'scheduled review is not due'
                        USING ERRCODE = 'check_violation';
                END IF;
            ELSIF reopen_requires_source THEN
                IF NEW.reopen_trigger_source_document_id IS NULL
                   OR NOT EXISTS (
                        SELECT 1
                        FROM public.event_sources AS source_link
                        WHERE source_link.event_edition_id = NEW.event_edition_id
                          AND source_link.source_document_id =
                              NEW.reopen_trigger_source_document_id
                          AND source_link.collection_mode = 'realtime_signal'
                          AND source_link.linked_at > prior_review.completed_at
                   )
                THEN
                    RAISE EXCEPTION 'reopen requires newer realtime evidence'
                        USING ERRCODE = 'check_violation';
                END IF;
            ELSE
                RAISE EXCEPTION 'reopen reason is invalid'
                    USING ERRCODE = 'check_violation';
            END IF;
        END IF;
    END IF;

    IF NEW.review_status = 'completed' AND NEW.rule_activation_id IS NOT NULL THEN
        IF NEW.reason_code IS NULL OR NEW.reason_schema_version IS NULL THEN
            RAISE EXCEPTION 'completed review requires a versioned primary reason'
                USING ERRCODE = 'check_violation';
        END IF;
        SELECT reason.requires_reopen_not_before,
               reason.requires_note,
               reason.required_capability
        INTO reason_requires_time, reason_requires_note, reason_capability
        FROM public.review_ruleset_completion_reasons AS reason
        WHERE reason.ruleset_version = NEW.rule_version
          AND reason.reason_code = NEW.reason_code
          AND reason.review_decision = NEW.review_decision
          AND reason.disposition = NEW.disposition;

        IF reason_requires_time IS NULL THEN
            RAISE EXCEPTION 'completed review reason matrix is invalid'
                USING ERRCODE = 'check_violation';
        END IF;
        IF reason_requires_note AND (
            NEW.reviewer_note IS NULL OR BTRIM(NEW.reviewer_note) = ''
        ) THEN
            RAISE EXCEPTION 'completed review reason requires reviewer note'
                USING ERRCODE = 'check_violation';
        END IF;
        IF reason_requires_time AND (
            NEW.reopen_not_before IS NULL
            OR NEW.reopen_not_before <= NEW.completed_at
        ) THEN
            RAISE EXCEPTION 'completed review disposition requires future reopen time'
                USING ERRCODE = 'check_violation';
        END IF;
        IF NOT reason_requires_time AND NEW.reopen_not_before IS NOT NULL THEN
            RAISE EXCEPTION 'completed review disposition forbids reopen time'
                USING ERRCODE = 'check_violation';
        END IF;
        IF reason_capability IS NOT NULL THEN
            RAISE EXCEPTION 'review completion capability is not available'
                USING ERRCODE = 'check_violation';
        END IF;
    END IF;

    RETURN NEW;
END;
$$;

CREATE TRIGGER trg_validate_versioned_tenant_review
    BEFORE INSERT OR UPDATE ON public.tenant_reviews
    FOR EACH ROW EXECUTE FUNCTION public.app_validate_versioned_tenant_review();

CREATE TABLE public.tenant_candidate_score_snapshots (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id           UUID NOT NULL REFERENCES public.tenants(id),
    candidate_id        UUID NOT NULL,
    event_edition_id    UUID NOT NULL,
    grant_id            UUID NOT NULL,
    ruleset_version     TEXT NOT NULL,
    ruleset_sha256      TEXT NOT NULL,
    scoring_method_version TEXT NOT NULL,
    input_hash          TEXT NOT NULL,
    total_score         NUMERIC(5, 2) NOT NULL,
    priority_band       TEXT NOT NULL,
    component_scores    JSONB NOT NULL,
    evidence_refs       JSONB NOT NULL DEFAULT '[]'::JSONB,
    score_as_of         TIMESTAMPTZ NOT NULL,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT uq_tenant_candidate_score_input
        UNIQUE (
            tenant_id, candidate_id, ruleset_version,
            scoring_method_version, input_hash
        ),
    CONSTRAINT fk_tenant_candidate_score_candidate
        FOREIGN KEY (candidate_id, tenant_id, event_edition_id, grant_id)
        REFERENCES public.tenant_candidates(
            id, tenant_id, event_edition_id, grant_id
        ),
    CONSTRAINT fk_tenant_candidate_score_ruleset
        FOREIGN KEY (ruleset_version, ruleset_sha256)
        REFERENCES public.review_rulesets(version, definition_sha256),
    CONSTRAINT ck_tenant_candidate_score_hash
        CHECK (input_hash ~ '^[0-9a-f]{64}$'),
    CONSTRAINT ck_tenant_candidate_score_method
        CHECK (
            scoring_method_version ~
                '^review-priority-envelope/[1-9][0-9]*\.[0-9]+\.[0-9]+$'
        ),
    CONSTRAINT ck_tenant_candidate_score_range
        CHECK (total_score BETWEEN 0 AND 100),
    CONSTRAINT ck_tenant_candidate_score_band
        CHECK (priority_band IN ('urgent', 'high', 'normal', 'low')),
    CONSTRAINT ck_tenant_candidate_score_components
        CHECK (JSONB_TYPEOF(component_scores) = 'object'),
    CONSTRAINT ck_tenant_candidate_score_evidence
        CHECK (JSONB_TYPEOF(evidence_refs) = 'array')
);

CREATE OR REPLACE FUNCTION public.app_validate_candidate_score_snapshot()
RETURNS TRIGGER
LANGUAGE plpgsql
SECURITY INVOKER
SET search_path = pg_catalog
AS $$
DECLARE
    priority_definition JSONB;
    component_code TEXT;
    component_value JSONB;
    component_score NUMERIC;
    component_maximum NUMERIC;
    computed_total NUMERIC := 0;
    expected_band TEXT;
BEGIN
    SELECT ruleset.definition -> 'priority'
    INTO priority_definition
    FROM public.review_rulesets AS ruleset
    WHERE ruleset.version = NEW.ruleset_version
      AND ruleset.definition_sha256 = NEW.ruleset_sha256;

    IF priority_definition IS NULL
       OR NEW.scoring_method_version IS DISTINCT FROM
            priority_definition ->> 'method_version'
       OR (
            SELECT COUNT(*)::INTEGER
            FROM JSONB_OBJECT_KEYS(NEW.component_scores)
       ) IS DISTINCT FROM (
            SELECT COUNT(*)::INTEGER
            FROM JSONB_OBJECT_KEYS(priority_definition -> 'components')
       )
    THEN
        RAISE EXCEPTION 'score snapshot envelope differs from ruleset'
            USING ERRCODE = 'check_violation';
    END IF;

    FOR component_code, component_value IN
        SELECT key, value FROM JSONB_EACH(NEW.component_scores)
    LOOP
        IF JSONB_TYPEOF(component_value) <> 'object'
           OR (
                SELECT COUNT(*)
                FROM JSONB_OBJECT_KEYS(component_value)
           ) <> 2
           OR NOT component_value ? 'score'
           OR NOT component_value ? 'explanation_code'
           OR JSONB_TYPEOF(component_value -> 'score') <> 'number'
        THEN
            RAISE EXCEPTION 'score snapshot component shape is invalid'
                USING ERRCODE = 'check_violation';
        END IF;

        component_maximum := (
            priority_definition -> 'components' ->> component_code
        )::NUMERIC;
        component_score := (component_value ->> 'score')::NUMERIC;
        IF component_maximum IS NULL
           OR component_score < 0
           OR component_score > component_maximum
           OR NOT EXISTS (
                SELECT 1
                FROM JSONB_ARRAY_ELEMENTS_TEXT(
                    priority_definition -> 'explanation_codes' -> component_code
                ) AS allowed(code)
                WHERE allowed.code = component_value ->> 'explanation_code'
           )
        THEN
            RAISE EXCEPTION 'score snapshot component is outside ruleset envelope'
                USING ERRCODE = 'check_violation';
        END IF;
        computed_total := computed_total + component_score;
    END LOOP;

    SELECT band ->> 'code'
    INTO expected_band
    FROM JSONB_ARRAY_ELEMENTS(priority_definition -> 'bands') AS band
    WHERE computed_total >= (band ->> 'minimum')::NUMERIC
    ORDER BY (band ->> 'minimum')::NUMERIC DESC
    LIMIT 1;

    IF NEW.total_score IS DISTINCT FROM computed_total
       OR NEW.priority_band IS DISTINCT FROM expected_band
    THEN
        RAISE EXCEPTION 'score snapshot total or band differs from components'
            USING ERRCODE = 'check_violation';
    END IF;
    RETURN NEW;
END;
$$;

CREATE TRIGGER trg_tenant_candidate_scores_validate
    BEFORE INSERT ON public.tenant_candidate_score_snapshots
    FOR EACH ROW EXECUTE FUNCTION public.app_validate_candidate_score_snapshot();

CREATE INDEX idx_tenant_candidate_score_queue
    ON public.tenant_candidate_score_snapshots (
        tenant_id, priority_band, total_score DESC, score_as_of, id
    );

CREATE TRIGGER trg_tenant_candidate_scores_immutable
    BEFORE UPDATE OR DELETE ON public.tenant_candidate_score_snapshots
    FOR EACH ROW EXECUTE FUNCTION public.app_reject_immutable_review_rule();

ALTER TABLE public.tenant_candidate_score_snapshots ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.tenant_candidate_score_snapshots FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_candidate_score_snapshots_isolation
    ON public.tenant_candidate_score_snapshots
    AS PERMISSIVE FOR ALL TO PUBLIC
    USING (tenant_id = public.app_current_tenant_id())
    WITH CHECK (tenant_id = public.app_current_tenant_id());

-- Forward-fix 023: revoke only withdraws unfinished work. Completed history remains closed.
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
              AND candidate_status IN ('open', 'in_review');
        END IF;
    END IF;

    RETURN NEW;
END;
$$;

-- Published repository ruleset. It is not activated for any tenant here.
INSERT INTO public.review_rulesets (
    version, schema_version, definition_sha256, definition,
    max_review_rounds, approval_reference
) VALUES (
    'review-rules/1.0.0',
    'review-ruleset.v1',
    '285b8c2fe43ca8b6d3517df223488f4d2fd3e6c7940cbe84ae547ade4b3f48ff',
    $ruleset${"completion_reasons":{"competitor_committed_no_entry":{"decision":"rejected","disposition":"competitor_watch","required_capability":null,"requires_note":false,"requires_reopen_not_before":true},"competitor_present_replaceable":{"decision":"qualified","disposition":"competitor_watch","required_capability":null,"requires_note":false,"requires_reopen_not_before":true},"contact_missing_or_stale":{"decision":"needs_more_info","disposition":"nurture","required_capability":null,"requires_note":false,"requires_reopen_not_before":true},"event_ended_or_too_late":{"decision":"rejected","disposition":"archive","required_capability":null,"requires_note":false,"requires_reopen_not_before":false},"evidence_conflict":{"decision":"needs_more_info","disposition":"nurture","required_capability":null,"requires_note":false,"requires_reopen_not_before":true},"future_contact_window":{"decision":"qualified","disposition":"nurture","required_capability":null,"requires_note":false,"requires_reopen_not_before":true},"invalid_or_unverifiable_evidence":{"decision":"rejected","disposition":"archive","required_capability":null,"requires_note":false,"requires_reopen_not_before":false},"no_online_voting_need":{"decision":"rejected","disposition":"archive","required_capability":null,"requires_note":false,"requires_reopen_not_before":false},"not_selection_or_voting":{"decision":"rejected","disposition":"archive","required_capability":null,"requires_note":false,"requires_reopen_not_before":false},"online_voting_need_unknown":{"decision":"needs_more_info","disposition":"nurture","required_capability":null,"requires_note":false,"requires_reopen_not_before":true},"organizer_unconfirmed":{"decision":"needs_more_info","disposition":"nurture","required_capability":null,"requires_note":false,"requires_reopen_not_before":true},"other_missing_information":{"decision":"needs_more_info","disposition":"nurture","required_capability":null,"requires_note":true,"requires_reopen_not_before":true},"other_rejection":{"decision":"rejected","disposition":"archive","required_capability":null,"requires_note":true,"requires_reopen_not_before":false},"outside_target_policy":{"decision":"rejected","disposition":"archive","required_capability":null,"requires_note":false,"requires_reopen_not_before":false},"sales_ready_confirmed":{"decision":"qualified","disposition":"sales_handoff","required_capability":"opportunity_atomic_create","requires_note":false,"requires_reopen_not_before":false},"stage_or_deadline_unknown":{"decision":"needs_more_info","disposition":"nurture","required_capability":null,"requires_note":false,"requires_reopen_not_before":true},"valid_but_not_sales_ready":{"decision":"qualified","disposition":"nurture","required_capability":null,"requires_note":false,"requires_reopen_not_before":true}},"priority":{"bands":[{"code":"urgent","minimum":80,"sla_minutes":120},{"code":"high","minimum":60,"sla_minutes":480},{"code":"normal","minimum":40,"sla_minutes":1440},{"code":"low","minimum":0,"sla_minutes":null}],"components":{"contactability":15,"evidence_quality":10,"online_voting_demand":25,"organizer_value":20,"timeliness_stage":30},"explanation_codes":{"contactability":["contact_none","contact_stale","contact_indirect","contact_verified"],"evidence_quality":["evidence_weak","evidence_single_source","evidence_correlated","evidence_verified"],"online_voting_demand":["demand_unknown","demand_negative","demand_indirect","demand_explicit"],"organizer_value":["organizer_unknown","organizer_first_seen","organizer_repeat","organizer_strategic"],"timeliness_stage":["timeliness_unknown","timeliness_historical","timeliness_future","timeliness_active","timeliness_urgent"]},"method_version":"review-priority-envelope/1.0.0"},"reopen":{"max_rounds":3,"reasons":{"competitor_status_changed":{"requires_new_realtime_source":true},"missing_information_resolved":{"requires_new_realtime_source":true},"new_realtime_evidence":{"requires_new_realtime_source":true},"scheduled_recheck_due":{"requires_new_realtime_source":false}}},"schema_version":"review-ruleset.v1","version":"review-rules/1.0.0"}$ruleset$::JSONB,
    3,
    'D-035 / 审核规则集契约 v1'
);

INSERT INTO public.review_ruleset_completion_reasons (
    ruleset_version, reason_code, review_decision, disposition,
    requires_reopen_not_before, requires_note, required_capability
) VALUES
    ('review-rules/1.0.0', 'sales_ready_confirmed', 'qualified', 'sales_handoff', FALSE, FALSE, 'opportunity_atomic_create'),
    ('review-rules/1.0.0', 'future_contact_window', 'qualified', 'nurture', TRUE, FALSE, NULL),
    ('review-rules/1.0.0', 'valid_but_not_sales_ready', 'qualified', 'nurture', TRUE, FALSE, NULL),
    ('review-rules/1.0.0', 'competitor_present_replaceable', 'qualified', 'competitor_watch', TRUE, FALSE, NULL),
    ('review-rules/1.0.0', 'not_selection_or_voting', 'rejected', 'archive', FALSE, FALSE, NULL),
    ('review-rules/1.0.0', 'event_ended_or_too_late', 'rejected', 'archive', FALSE, FALSE, NULL),
    ('review-rules/1.0.0', 'no_online_voting_need', 'rejected', 'archive', FALSE, FALSE, NULL),
    ('review-rules/1.0.0', 'outside_target_policy', 'rejected', 'archive', FALSE, FALSE, NULL),
    ('review-rules/1.0.0', 'invalid_or_unverifiable_evidence', 'rejected', 'archive', FALSE, FALSE, NULL),
    ('review-rules/1.0.0', 'other_rejection', 'rejected', 'archive', FALSE, TRUE, NULL),
    ('review-rules/1.0.0', 'competitor_committed_no_entry', 'rejected', 'competitor_watch', TRUE, FALSE, NULL),
    ('review-rules/1.0.0', 'stage_or_deadline_unknown', 'needs_more_info', 'nurture', TRUE, FALSE, NULL),
    ('review-rules/1.0.0', 'online_voting_need_unknown', 'needs_more_info', 'nurture', TRUE, FALSE, NULL),
    ('review-rules/1.0.0', 'organizer_unconfirmed', 'needs_more_info', 'nurture', TRUE, FALSE, NULL),
    ('review-rules/1.0.0', 'contact_missing_or_stale', 'needs_more_info', 'nurture', TRUE, FALSE, NULL),
    ('review-rules/1.0.0', 'evidence_conflict', 'needs_more_info', 'nurture', TRUE, FALSE, NULL),
    ('review-rules/1.0.0', 'other_missing_information', 'needs_more_info', 'nurture', TRUE, TRUE, NULL);

INSERT INTO public.review_ruleset_reopen_reasons (
    ruleset_version, reason_code, requires_new_realtime_source
) VALUES
    ('review-rules/1.0.0', 'scheduled_recheck_due', FALSE),
    ('review-rules/1.0.0', 'new_realtime_evidence', TRUE),
    ('review-rules/1.0.0', 'missing_information_resolved', TRUE),
    ('review-rules/1.0.0', 'competitor_status_changed', TRUE);

COMMENT ON TABLE public.review_rulesets IS
    'Immutable platform-approved review rule definitions; tenant activation is separate.';
COMMENT ON TABLE public.tenant_review_ruleset_activations IS
    'Tenant-private append-only activation history; runtime is read-only.';
COMMENT ON TABLE public.tenant_candidate_score_snapshots IS
    'Append-only explainable queue score snapshots; never an automatic review decision.';
