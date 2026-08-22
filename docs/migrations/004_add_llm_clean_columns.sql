-- =====================================================
-- 迁移 004: 线索表新增「后台 LLM 清洗流水线」状态列
-- 版本：v1.0 (云端首次部署/现有库升级均可幂等执行)
-- 用途：支持后台异步大模型清洗——按状态挑单、幂等重试、保护人工编辑
--   - llm_status       清洗状态：pending(待清洗) / done(已完成) / fail(失败)
--   - updated_by_human 人工是否编辑过（悬浮窗改过即置 TRUE）；置 TRUE 后 LLM 不再覆盖
--   - llm_last_run_at   最近一次 LLM 处理时间（成功/失败均记，便于排查与退避）
--   - llm_attempts      累计尝试次数（达上限仍失败则标 fail，不再无限重试）
-- 说明：新列默认值保证存量线索自动进入「待清洗」，无需数据回填即可被后台任务捞取。
-- =====================================================

ALTER TABLE qualified_leads
    ADD COLUMN IF NOT EXISTS llm_status       TEXT    DEFAULT 'pending',
    ADD COLUMN IF NOT EXISTS updated_by_human BOOLEAN DEFAULT FALSE,
    ADD COLUMN IF NOT EXISTS llm_last_run_at  TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS llm_attempts     INTEGER DEFAULT 0;

-- 部分索引：后台任务只挑「待清洗且未被人工动过」的线索，按优先级出队。
-- 加 WHERE 条件使索引精小、命中率高（待处理量通常远小于总量）。
CREATE INDEX IF NOT EXISTS idx_ql_llm_pending
    ON qualified_leads (priority_level, priority_score DESC)
    WHERE llm_status = 'pending' AND updated_by_human = FALSE;

-- ===================================================================
-- 校验与提示
-- ===================================================================

DO $$
BEGIN
    RAISE NOTICE '✅ Migration 004 completed successfully.';
    RAISE NOTICE '  - qualified_leads: llm_status / updated_by_human / llm_last_run_at / llm_attempts added';
    RAISE NOTICE '  - index idx_ql_llm_pending created (partial: pending & not human-edited)';
END $$;
