-- =====================================================
-- 迁移 006: 真实采集时间 + 资源等级多标签字段
-- 版本：v1.0 (云端首次部署/现有库升级均可幂等执行)
-- 用途：
--   - collected_at       虚拟机真正采集(抓取)该文章的时刻(区别于入库时刻 created_at)
--   - is_recurring       是否多届/往届活动 (LLM 判定)
--   - activity_category  活动类别 (LLM 判定, 值待定, 允许自由给)
-- 说明：不回填历史，存量行这三列为 NULL；新采集/新清洗自动填充。
-- =====================================================

ALTER TABLE articles_core
    ADD COLUMN IF NOT EXISTS collected_at      TIMESTAMPTZ DEFAULT NULL,
    ADD COLUMN IF NOT EXISTS is_recurring      BOOLEAN     DEFAULT NULL,
    ADD COLUMN IF NOT EXISTS activity_category TEXT        DEFAULT NULL;

ALTER TABLE qualified_leads
    ADD COLUMN IF NOT EXISTS collected_at      TIMESTAMPTZ DEFAULT NULL,
    ADD COLUMN IF NOT EXISTS is_recurring      BOOLEAN     DEFAULT NULL,
    ADD COLUMN IF NOT EXISTS activity_category TEXT        DEFAULT NULL;

-- 采集时间排序会用到（发布时间已有 idx_ac_publish_time）
CREATE INDEX IF NOT EXISTS idx_ac_collected_at ON articles_core(collected_at DESC);
CREATE INDEX IF NOT EXISTS idx_ql_collected_at ON qualified_leads(collected_at DESC);

-- ===================================================================
-- 校验与提示
-- ===================================================================

DO $$
BEGIN
    RAISE NOTICE '✅ Migration 006 completed successfully.';
    RAISE NOTICE '  - articles_core/qualified_leads: collected_at / is_recurring / activity_category added';
END $$;
