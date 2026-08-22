-- =====================================================
-- 迁移 016: 主办方库（独立业务模块）
-- 版本：v1.0 (幂等，云端首次部署/现有库升级均可重复执行)
-- 定位：主办方库是独立于「线索公海/AI活动库/我的活动库」的业务模块，作为业务线索的
--       另一个来源。与线索库仅两个交互点：
--         ① 数据来源：线索 LLM 清洗时顺带抽出主办方相关字段，沉淀进本库；
--         ② 详情页跳转：线索详情页主办方命中本库时可点击跳转到主办方档案。
--
-- 本迁移做两件事：
--   A. 给 qualified_leads 补 4 个「线索级」主办方抽取字段（LLM 清洗填写，不回填历史）：
--       - organizer_name    主办方主体（只抽主办，不含承办/协办）
--       - organizer_contact 结构化联系方式 JSONB（电话/微信/邮箱/链接）
--       - organizer_region  具体省/市（与层级字段 activity_region 全国/省/市 并存，概念不同）
--       - voting_platform   评选系统平台名（LLM 抽平台名 + URL 域名字典归一，可空）
--   B. 新建 organizers 聚合表（归并后的主办方档案，可由聚合任务定期重建）。
-- 说明：存量行留空，新清洗自动填充；聚合表由后台任务从 qualified_leads 归并生成。
-- =====================================================

-- ---------- A. 线索表：主办方线索级抽取字段 ----------
ALTER TABLE qualified_leads
    ADD COLUMN IF NOT EXISTS organizer_name    TEXT  DEFAULT NULL,
    ADD COLUMN IF NOT EXISTS organizer_contact JSONB DEFAULT NULL,
    ADD COLUMN IF NOT EXISTS organizer_region  TEXT  DEFAULT NULL,
    ADD COLUMN IF NOT EXISTS voting_platform   TEXT  DEFAULT NULL;

-- 镜像回 articles_core（与 activity_* 字段一致，避免重新提升线索时回退）
ALTER TABLE articles_core
    ADD COLUMN IF NOT EXISTS organizer_name    TEXT  DEFAULT NULL,
    ADD COLUMN IF NOT EXISTS organizer_contact JSONB DEFAULT NULL,
    ADD COLUMN IF NOT EXISTS organizer_region  TEXT  DEFAULT NULL,
    ADD COLUMN IF NOT EXISTS voting_platform   TEXT  DEFAULT NULL;

-- 按主办方名筛/聚合的部分索引（只索引非空，索引精小）
CREATE INDEX IF NOT EXISTS idx_ql_organizer_name
    ON qualified_leads (organizer_name)
    WHERE organizer_name IS NOT NULL AND organizer_name <> '';

-- ---------- B. 主办方聚合档案表 ----------
-- 归并策略：保守精确归并（规范名精确匹配）+ 人工合并入口（merged_into 指向主档案）。
CREATE TABLE IF NOT EXISTS organizers (
    id              SERIAL PRIMARY KEY,
    canonical_name  TEXT NOT NULL,                 -- 规范名（归一后的主办方主体）
    norm_key        TEXT NOT NULL,                 -- 归一键（去空格/括号/全半角，用于精确归并去重）
    aliases         JSONB       DEFAULT '[]'::jsonb, -- 别名/变体列表
    contact         JSONB       DEFAULT '{}'::jsonb, -- 合并后的结构化联系方式（取名下最全）
    region          TEXT        DEFAULT NULL,        -- 地区（名下活动地区众数）
    voting_platforms JSONB      DEFAULT '[]'::jsonb, -- 用过的评选系统平台（去重列表）
    event_count     INTEGER     DEFAULT 0,           -- 举办次数（按活动去重，非文章篇数）
    lead_ids        JSONB       DEFAULT '[]'::jsonb, -- 关联线索 id 列表
    first_activity_at TIMESTAMPTZ DEFAULT NULL,      -- 首次活动时间
    last_activity_at  TIMESTAMPTZ DEFAULT NULL,      -- 最近活动时间
    merged_into     INTEGER     DEFAULT NULL          -- 人工合并：指向主档案 id（非空表示本行已被并入）
                    REFERENCES organizers(id) ON DELETE SET NULL,
    updated_by_human BOOLEAN    DEFAULT FALSE,        -- 人工是否编辑/合并过（重建时保护人工结果）
    created_at      TIMESTAMPTZ DEFAULT NOW(),
    updated_at      TIMESTAMPTZ DEFAULT NOW()
);

-- 归一键唯一：保证保守精确归并落到同一档案（同一 norm_key 只有一条主档案）
CREATE UNIQUE INDEX IF NOT EXISTS uq_organizers_norm_key ON organizers (norm_key);
CREATE INDEX IF NOT EXISTS idx_organizers_event_count ON organizers (event_count DESC);
CREATE INDEX IF NOT EXISTS idx_organizers_merged_into ON organizers (merged_into);

-- ===================================================================
-- 校验与提示
-- ===================================================================
DO $$
BEGIN
    RAISE NOTICE '✅ Migration 016 completed successfully.';
    RAISE NOTICE '  - qualified_leads/articles_core: organizer_name/organizer_contact/organizer_region/voting_platform added';
    RAISE NOTICE '  - organizers aggregate table created (norm_key unique, merged_into for manual merge)';
END $$;
