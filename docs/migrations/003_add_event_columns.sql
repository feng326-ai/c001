-- =====================================================
-- 迁移 003: 线索表新增「活动名称 / 活动信息 / 备注笔记」三列
-- 版本：v1.0 (云端首次部署/现有库升级均可幂等执行)
-- 用途：支持资源处理人员补充活动信息与笔记，并可随线索一起导出
--   - event_name    活动名称（AI 生成初稿 + 人工可编辑）
--   - event_details 活动信息（AI 提取：活动介绍/联系方式/报名方式等）
--   - notes         备注/笔记（纯人工填写，悬浮窗内维护）
-- =====================================================

ALTER TABLE qualified_leads
    ADD COLUMN IF NOT EXISTS event_name    TEXT,
    ADD COLUMN IF NOT EXISTS event_details TEXT,
    ADD COLUMN IF NOT EXISTS notes         TEXT DEFAULT '';

-- articles_core 同步保留 AI 生成的活动名称/信息，便于回写与统计（备注不落 articles_core，属人工态）
ALTER TABLE articles_core
    ADD COLUMN IF NOT EXISTS event_name    TEXT,
    ADD COLUMN IF NOT EXISTS event_details TEXT;

-- ===================================================================
-- 校验与提示
-- ===================================================================

DO $$
BEGIN
    RAISE NOTICE '✅ Migration 003 completed successfully.';
    RAISE NOTICE '  - qualified_leads: event_name / event_details / notes added';
    RAISE NOTICE '  - articles_core:   event_name / event_details added';
END $$;
