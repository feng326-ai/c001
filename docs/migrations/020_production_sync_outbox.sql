-- 020: 本地采集库 -> 正式业务库的可靠、幂等同步基础。
-- 同一迁移在两端执行；production_sync_settings.enabled 默认 false，只有采集源库显式开启。

CREATE TABLE IF NOT EXISTS production_sync_settings (
    singleton   BOOLEAN PRIMARY KEY DEFAULT TRUE CHECK (singleton),
    enabled     BOOLEAN NOT NULL DEFAULT FALSE,
    source_name TEXT NOT NULL DEFAULT 'local-collector',
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

INSERT INTO production_sync_settings (singleton, enabled)
VALUES (TRUE, FALSE)
ON CONFLICT (singleton) DO NOTHING;

CREATE TABLE IF NOT EXISTS production_sync_outbox (
    id                BIGSERIAL PRIMARY KEY,
    event_id          UUID NOT NULL DEFAULT uuid_generate_v4() UNIQUE,
    entity_type       TEXT NOT NULL CHECK (entity_type IN ('article', 'lead')),
    entity_key        TEXT NOT NULL,
    source_updated_at TIMESTAMPTZ NOT NULL,
    payload           JSONB NOT NULL,
    status            TEXT NOT NULL DEFAULT 'pending'
                      CHECK (status IN ('pending', 'retry', 'sent', 'dead')),
    attempts          INTEGER NOT NULL DEFAULT 0,
    next_attempt_at   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    last_error        TEXT,
    created_at        TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    sent_at           TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS idx_prod_sync_outbox_due
    ON production_sync_outbox (next_attempt_at, id)
    WHERE status IN ('pending', 'retry');

CREATE TABLE IF NOT EXISTS production_sync_receipts (
    event_id          UUID PRIMARY KEY,
    entity_type       TEXT NOT NULL,
    entity_key        TEXT NOT NULL,
    source_updated_at TIMESTAMPTZ NOT NULL,
    payload_hash      TEXT NOT NULL,
    received_at       TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS production_sync_entity_versions (
    entity_type       TEXT NOT NULL,
    entity_key        TEXT NOT NULL,
    source_updated_at TIMESTAMPTZ NOT NULL,
    event_id          UUID NOT NULL,
    PRIMARY KEY (entity_type, entity_key)
);

CREATE TABLE IF NOT EXISTS production_sync_article_keys (
    source_uuid       UUID PRIMARY KEY,
    target_article_id BIGINT NOT NULL REFERENCES articles_core(id) ON DELETE CASCADE,
    created_at        TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE OR REPLACE FUNCTION enqueue_production_sync_event()
RETURNS TRIGGER AS $$
DECLARE
    sync_enabled BOOLEAN := FALSE;
    article_uuid UUID;
    clean_payload JSONB;
BEGIN
    SELECT enabled INTO sync_enabled
    FROM production_sync_settings
    WHERE singleton = TRUE;

    IF NOT COALESCE(sync_enabled, FALSE) THEN
        RETURN NEW;
    END IF;

    IF TG_TABLE_NAME = 'articles_core' THEN
        article_uuid := NEW.uuid;
        clean_payload := to_jsonb(NEW) - ARRAY['id', 'channels_jsonb_legacy'];
        INSERT INTO production_sync_outbox
            (entity_type, entity_key, source_updated_at, payload)
        VALUES
            ('article', article_uuid::TEXT, COALESCE(NEW.updated_at, NOW()), clean_payload);
    ELSIF TG_TABLE_NAME = 'qualified_leads' THEN
        SELECT uuid INTO article_uuid FROM articles_core WHERE id = NEW.article_id;
        IF article_uuid IS NULL THEN
            RETURN NEW;
        END IF;
        -- 只发送采集/AI 字段；正式业务字段绝不进入同步负载。
        clean_payload := (to_jsonb(NEW) - ARRAY[
            'id', 'article_id', 'status', 'assigned_to', 'follow_up_deadline',
            'last_contacted_at', 'conversion_notes', 'notes', 'in_library',
            'llm_feedback', 'updated_by_human'
        ]) || jsonb_build_object('article_uuid', article_uuid);
        INSERT INTO production_sync_outbox
            (entity_type, entity_key, source_updated_at, payload)
        VALUES
            ('lead', article_uuid::TEXT, COALESCE(NEW.updated_at, NOW()), clean_payload);
    END IF;

    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_articles_production_sync ON articles_core;
CREATE TRIGGER trg_articles_production_sync
AFTER INSERT OR UPDATE ON articles_core
FOR EACH ROW EXECUTE FUNCTION enqueue_production_sync_event();

DROP TRIGGER IF EXISTS trg_leads_production_sync ON qualified_leads;
CREATE TRIGGER trg_leads_production_sync
AFTER INSERT OR UPDATE ON qualified_leads
FOR EACH ROW EXECUTE FUNCTION enqueue_production_sync_event();
