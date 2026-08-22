-- =====================================================
-- 迁移 005: 线索表新增「线上投票/网络报名」识别列
-- 版本：v1.0 (云端首次部署/现有库升级均可幂等执行)
-- 用途：支持从 LLM 分析结果中提取并标识活动是否有线上投票环节
--   - is_online_voting      是否有线上投票/网络评选环节 (BOOLEAN)
--   - online_voting_url     网络投票/报名链接 (TEXT)
-- 说明：存量线索为 NULL(未清洗前), 后台 LLM 流水线会自动填充
-- =====================================================

ALTER TABLE qualified_leads
    ADD COLUMN IF NOT EXISTS is_online_voting       BOOLEAN DEFAULT NULL,
    ADD COLUMN IF NOT EXISTS online_voting_url      TEXT    DEFAULT NULL;

-- ===================================================================
-- 校验与提示
-- ===================================================================

DO $$
BEGIN
    RAISE NOTICE '✅ Migration 005 completed successfully.';
    RAISE NOTICE '  - qualified_leads: is_online_voting / online_voting_url added';
END $$;
