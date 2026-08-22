-- 021: 租户身份与 RLS 地基（expand-only）。
-- 保留 users.id、teams 和全部旧外键；只为后续 v2 业务表增加稳定 UUID 身份。

ALTER TABLE users
    ADD COLUMN IF NOT EXISTS public_id UUID;

ALTER TABLE users
    ALTER COLUMN public_id SET DEFAULT gen_random_uuid();

UPDATE users
SET public_id = gen_random_uuid()
WHERE public_id IS NULL;

ALTER TABLE users
    ALTER COLUMN public_id SET NOT NULL;

CREATE UNIQUE INDEX IF NOT EXISTS ux_users_public_id
    ON users (public_id);

CREATE TABLE tenants (
    id                        UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    code                      TEXT NOT NULL UNIQUE,
    name                      TEXT NOT NULL,
    status                    TEXT NOT NULL DEFAULT 'active',
    default_visibility_policy TEXT NOT NULL DEFAULT 'shared_competition',
    version                   BIGINT NOT NULL DEFAULT 1,
    created_at                TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at                TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT ck_tenants_code_not_blank
        CHECK (BTRIM(code) <> ''),
    CONSTRAINT ck_tenants_name_not_blank
        CHECK (BTRIM(name) <> ''),
    CONSTRAINT ck_tenants_status
        CHECK (status IN ('active', 'disabled')),
    CONSTRAINT ck_tenants_default_visibility_policy
        CHECK (default_visibility_policy IN (
            'shared_competition',
            'tenant_private',
            'selected_tenants'
        )),
    CONSTRAINT ck_tenants_version_positive CHECK (version >= 1)
);

CREATE INDEX idx_tenants_status
    ON tenants (status);

CREATE TABLE tenant_memberships (
    id         UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id  UUID NOT NULL REFERENCES tenants(id),
    user_id    UUID NOT NULL REFERENCES users(public_id),
    role       TEXT NOT NULL,
    status     TEXT NOT NULL DEFAULT 'active',
    version    BIGINT NOT NULL DEFAULT 1,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT uq_tenant_memberships_tenant_user
        UNIQUE (tenant_id, user_id),
    CONSTRAINT ck_tenant_memberships_role
        CHECK (role IN (
            'tenant_admin',
            'resource_reviewer',
            'sales',
            'readonly_manager'
        )),
    CONSTRAINT ck_tenant_memberships_status
        CHECK (status IN ('active', 'disabled')),
    CONSTRAINT ck_tenant_memberships_version_positive CHECK (version >= 1)
);

CREATE INDEX idx_tenant_memberships_user_status
    ON tenant_memberships (user_id, status);

CREATE INDEX idx_tenant_memberships_tenant_status
    ON tenant_memberships (tenant_id, status);

-- Fail closed for missing, empty or malformed context. The authenticated API
-- must set this value with SET LOCAL (or set_config(..., true)) after beginning
-- a transaction; a request-supplied tenant_id is never a trusted source.
CREATE OR REPLACE FUNCTION app_current_tenant_id()
RETURNS UUID
LANGUAGE plpgsql
STABLE
PARALLEL SAFE
SECURITY INVOKER
SET search_path = pg_catalog
AS $$
DECLARE
    setting_value TEXT := current_setting('app.tenant_id', TRUE);
BEGIN
    IF setting_value IS NULL OR setting_value !~*
        '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
    THEN
        RETURN NULL;
    END IF;

    RETURN setting_value::UUID;
EXCEPTION
    WHEN invalid_text_representation THEN
        RETURN NULL;
END;
$$;

COMMENT ON FUNCTION app_current_tenant_id() IS
    'Fail-closed parser for transaction-local app.tenant_id; callers must use SET LOCAL after authorization.';

ALTER TABLE tenants ENABLE ROW LEVEL SECURITY;
ALTER TABLE tenants FORCE ROW LEVEL SECURITY;

CREATE POLICY tenants_tenant_isolation
    ON tenants
    AS PERMISSIVE
    FOR ALL
    TO PUBLIC
    USING (id = public.app_current_tenant_id())
    WITH CHECK (id = public.app_current_tenant_id());

ALTER TABLE tenant_memberships ENABLE ROW LEVEL SECURITY;
ALTER TABLE tenant_memberships FORCE ROW LEVEL SECURITY;

CREATE POLICY tenant_memberships_tenant_isolation
    ON tenant_memberships
    AS PERMISSIVE
    FOR ALL
    TO PUBLIC
    USING (tenant_id = public.app_current_tenant_id())
    WITH CHECK (tenant_id = public.app_current_tenant_id());
