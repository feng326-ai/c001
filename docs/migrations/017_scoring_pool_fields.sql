-- =====================================================
-- 迁移 017: 评分口径校准 + 分池 + 周期唤醒 字段
-- 版本：v1.0 (幂等，ADD COLUMN IF NOT EXISTS，云端可重复执行)
-- 用途：为「线索公海 LLM 过滤与分池优化」埋字段，qualified_leads 与 articles_core 镜像。
--   - recurrence_period   周期粒度(年度/双年/季度/月度/不定期/未知)：LLM 弱提示，权威值由 Phase5 系列聚合派生
--   - edition_no          第几届(整数)：辅助系列排序/缺采检测，可空、不编造
--   - voting_status       线上投票三态(has/none/suspect)：suspect=文中未提但评选/榜单类疑似有，交业务员核实
--   - event_key           归一事件键：跨媒体多来源聚成同一事件，供清洗去重/主办方聚合
--   - Phase5 埋点(本轮只加列不建仓)：
--       is_annual_recurring  是否年度周期(权威值 Phase5 派生)
--       event_cycle_month    常规举办自然月(1~12)
--       next_wake_up_date    下次唤醒触发日期
--       wake_up_status       唤醒状态(SLEEPING/WOKEN)
-- 说明：存量行留空，不回填；新清洗/新聚合自动填充。is_online_voting 保留兼容不动。
-- =====================================================

ALTER TABLE qualified_leads
    ADD COLUMN IF NOT EXISTS recurrence_period   TEXT    DEFAULT NULL,
    ADD COLUMN IF NOT EXISTS edition_no          INTEGER DEFAULT NULL,
    ADD COLUMN IF NOT EXISTS voting_status       TEXT    DEFAULT NULL,
    ADD COLUMN IF NOT EXISTS event_key           TEXT    DEFAULT NULL,
    ADD COLUMN IF NOT EXISTS is_annual_recurring BOOLEAN DEFAULT NULL,
    ADD COLUMN IF NOT EXISTS event_cycle_month   INTEGER DEFAULT NULL,
    ADD COLUMN IF NOT EXISTS next_wake_up_date   DATE    DEFAULT NULL,
    ADD COLUMN IF NOT EXISTS wake_up_status      TEXT    DEFAULT NULL;

ALTER TABLE articles_core
    ADD COLUMN IF NOT EXISTS recurrence_period   TEXT    DEFAULT NULL,
    ADD COLUMN IF NOT EXISTS edition_no          INTEGER DEFAULT NULL,
    ADD COLUMN IF NOT EXISTS voting_status       TEXT    DEFAULT NULL,
    ADD COLUMN IF NOT EXISTS event_key           TEXT    DEFAULT NULL;

-- 事件聚合按 event_key 分组，加部分索引（只索引非空，索引精小）
CREATE INDEX IF NOT EXISTS idx_ql_event_key
    ON qualified_leads (event_key)
    WHERE event_key IS NOT NULL AND event_key <> '';

-- 周期唤醒定时任务按 next_wake_up_date + wake_up_status 扫描，加部分索引
CREATE INDEX IF NOT EXISTS idx_ql_wake_due
    ON qualified_leads (next_wake_up_date)
    WHERE wake_up_status = 'SLEEPING' AND next_wake_up_date IS NOT NULL;

-- ===================================================================
-- 校验与提示
-- ===================================================================
DO $$
BEGIN
    RAISE NOTICE '✅ Migration 017 completed successfully.';
    RAISE NOTICE '  - qualified_leads: +recurrence_period/edition_no/voting_status/event_key + phase5(is_annual_recurring/event_cycle_month/next_wake_up_date/wake_up_status)';
    RAISE NOTICE '  - articles_core mirror: +recurrence_period/edition_no/voting_status/event_key';
    RAISE NOTICE '  - indexes: idx_ql_event_key, idx_ql_wake_due';
END $$;
