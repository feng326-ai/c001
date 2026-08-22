-- =====================================================
-- 迁移 001: 修复 channels 类型一致性 + 补充 resource_level + 新增 keywords 数组
-- 版本：v1.1 (兼容云端首次部署/现有库升级)
-- =====================================================

-- ===================================================================
-- 1. articles_core.resource_level 补充
-- ===================================================================
ALTER TABLE articles_core ADD COLUMN IF NOT EXISTS resource_level TEXT DEFAULT 'normal';
CREATE INDEX IF NOT EXISTS idx_ac_resource_level ON articles_core(resource_level);

-- 回补历史数据（默认 'normal'，已有 AI 评分的可按规则重算，这里先用默认值安全起见）
UPDATE articles_core SET resource_level = 'normal' WHERE resource_level IS NULL;


-- ===================================================================
-- 2. qualified_leads.resource_level 补充（如果之前 ALTER 没落下去）
-- ===================================================================
ALTER TABLE qualified_leads ADD COLUMN IF NOT EXISTS resource_level TEXT DEFAULT 'normal';
CREATE INDEX IF NOT EXISTS idx_ql_resource_level ON qualified_leads(resource_level);

-- 从关联的文章同步资源等级（幂等）
UPDATE qualified_leads ql
SET resource_level = a.resource_level
FROM articles_core a
WHERE a.id = ql.article_id AND a.resource_level != 'normal';


-- ===================================================================
-- 3. articles_core.keywords TEXT[] 新增
--    同时把现有的 single-value keyword 扩展为数组格式
-- ===================================================================
ALTER TABLE articles_core ADD COLUMN IF NOT EXISTS keywords TEXT[];

-- 初始化：把单值的 keyword 转成数组 [keyword]
UPDATE articles_core SET keywords = ARRAY[keyword] WHERE keywords IS NULL;

-- GIN 索引加速数组查询
CREATE INDEX IF NOT EXISTS idx_ac_keywords ON articles_core USING GIN (keywords);


-- ===================================================================
-- 4. 修复 articles_core.channels 为 TEXT[] (兼容三种情况，幂等)
--    a) 列不存在 (云端全新部署)      → 直接新建 TEXT[]
--    b) 列是 JSONB (现有库)             → 重命名+新建+转换
--    c) 列已是 TEXT[]/ARRAY (重复执行) → 跳过
-- ===================================================================
DO $chan$
DECLARE
    col_type TEXT;
BEGIN
    SELECT data_type INTO col_type
    FROM information_schema.columns
    WHERE table_name = 'articles_core' AND column_name = 'channels';

    IF col_type IS NULL THEN
        -- a) 列不存在：直接新建
        ALTER TABLE articles_core ADD COLUMN channels TEXT[];
        RAISE NOTICE '  channels: 列不存在，已新建 TEXT[]';

    ELSIF col_type = 'jsonb' THEN
        -- b) JSONB 转 TEXT[]
        ALTER TABLE articles_core RENAME COLUMN channels TO channels_jsonb_legacy;
        ALTER TABLE articles_core ADD COLUMN channels TEXT[];
        UPDATE articles_core SET channels = ARRAY(
            SELECT jsonb_array_elements_text(channels_jsonb_legacy)
        ) WHERE channels_jsonb_legacy IS NOT NULL
          AND jsonb_typeof(channels_jsonb_legacy) = 'array';
        RAISE NOTICE '  channels: 已从 JSONB 转为 TEXT[] (旧列保留为 channels_jsonb_legacy)';

    ELSE
        -- c) 已是数组类型，无需处理
        RAISE NOTICE '  channels: 已是 % 类型，跳过', col_type;
    END IF;
END $chan$;

-- GIN 索引（文本数组推荐用 GIN）
CREATE INDEX IF NOT EXISTS idx_ac_channels ON articles_core USING GIN (channels);

-- 注：旧列 channels_jsonb_legacy 先保留，确认无误后可手动清理：
-- ALTER TABLE articles_core DROP COLUMN IF EXISTS channels_jsonb_legacy;


-- ===================================================================
-- 5. 校验与提示
-- ===================================================================

DO $$
BEGIN
    -- 检查关键字段是否存在
    IF NOT EXISTS (
        SELECT 1 FROM information_schema.columns 
        WHERE table_name = 'articles_core' 
        AND column_name = 'resource_level'
    ) THEN
        RAISE EXCEPTION '❌ Migration failed: articles_core.resource_level missing';
    END IF;
    
    IF NOT EXISTS (
        SELECT 1 FROM information_schema.columns 
        WHERE table_name = 'articles_core' 
        AND column_name = 'keywords'
    ) THEN
        RAISE EXCEPTION '❌ Migration failed: articles_core.keywords missing';
    END IF;
    
    RAISE NOTICE '✅ Migration 001 completed successfully.';
    RAISE NOTICE '  - articles_core.channels converted from JSONB to TEXT[]';
    RAISE NOTICE '  - articles_core.keywords added (initialized from single keyword)';
    RAISE NOTICE '  - articles_core.resource_level added and backfilled (default: normal)';
    RAISE NOTICE '  - qualified_leads.resource_level synced from articles_core';
END $$;
