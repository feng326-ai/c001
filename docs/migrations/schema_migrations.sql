-- =====================================================
-- schema_migrations: 记录已应用的迁移版本 (幂等可重复执行)
-- =====================================================

CREATE TABLE IF NOT EXISTS schema_migrations (
    version VARCHAR(255) PRIMARY KEY,
    applied_at TIMESTAMPTZ DEFAULT NOW(),
    description TEXT,
    checksum VARCHAR(64)  -- 可选：文件内容的 MD5/SHA256，防止脚本被篡改
);

-- 索引
CREATE INDEX IF NOT EXISTS idx_sm_applied_at ON schema_migrations(applied_at DESC);
