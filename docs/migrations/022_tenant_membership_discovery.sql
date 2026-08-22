-- 022: 认证后、选租户前的最小成员发现入口（expand-only）。
-- 调用方必须先用已验签会话解析 users.id/public_id；本函数不承担身份认证。

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
            '022 requires a controlled migration owner that can bypass FORCE RLS';
    END IF;
END;
$$;

CREATE OR REPLACE FUNCTION public.app_list_active_tenants(
    p_user_public_id UUID
)
RETURNS TABLE (
    tenant_id UUID,
    tenant_code TEXT,
    tenant_name TEXT,
    membership_id UUID,
    membership_role TEXT
)
LANGUAGE sql
STABLE
SECURITY DEFINER
SET search_path = pg_catalog
AS $$
    SELECT
        t.id,
        t.code,
        t.name,
        tm.id,
        tm.role
    FROM public.users AS u
    JOIN public.tenant_memberships AS tm
      ON tm.user_id = u.public_id
    JOIN public.tenants AS t
      ON t.id = tm.tenant_id
    WHERE u.public_id = p_user_public_id
      AND u.enabled = TRUE
      AND tm.status = 'active'
      AND t.status = 'active'
    ORDER BY t.code, t.id;
$$;

COMMENT ON FUNCTION public.app_list_active_tenants(UUID) IS
    'Narrow post-auth membership discovery; caller identity must come from a verified server session.';

REVOKE ALL ON FUNCTION public.app_list_active_tenants(UUID) FROM PUBLIC;
