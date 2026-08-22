-- =====================================================
-- 迁移 009: 活动地区 / 多届三态 / 活动状态 (LLM 判定)
-- 版本：v1.0 (幂等)
-- 用途：资源等级列新增展示项，均由 LLM 清洗判定填写。
--   - activity_region   地区级别: 全国 / 省 / 市 / 县 / 镇
--   - recurrence        多届三态: 多届 / 第一届 / 单届
--   - activity_status   活动状态: 征集中 / 报名中 / 进行中 / 已结束
-- 说明：不回填历史，存量行留空；新清洗自动填充。
--       resource_level 已存在(TEXT)，新增取值 'poor'(低)，无需改表。
-- =====================================================

ALTER TABLE articles_core
    ADD COLUMN IF NOT EXISTS activity_region  TEXT DEFAULT NULL,
    ADD COLUMN IF NOT EXISTS recurrence       TEXT DEFAULT NULL,
    ADD COLUMN IF NOT EXISTS activity_status  TEXT DEFAULT NULL;

ALTER TABLE qualified_leads
    ADD COLUMN IF NOT EXISTS activity_region  TEXT DEFAULT NULL,
    ADD COLUMN IF NOT EXISTS recurrence       TEXT DEFAULT NULL,
    ADD COLUMN IF NOT EXISTS activity_status  TEXT DEFAULT NULL;

-- ===================================================================
-- 校验与提示
-- ===================================================================
DO $$
BEGIN
    RAISE NOTICE '✅ Migration 009 completed successfully.';
    RAISE NOTICE '  - articles_core/qualified_leads: activity_region / recurrence / activity_status added';
END $$;
