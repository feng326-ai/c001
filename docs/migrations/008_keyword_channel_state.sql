-- =====================================================
-- 迁移 008: 按渠道独立调度 (keyword_channel_state)
-- 版本：v1.0 (幂等)
-- 用途：让搜一搜(souyisou)与搜狗(sogou)对同一批关键词各自独立按周期循环，
--       取代 keywords.status 单列的跨渠道耦合。
-- 兼容说明：自租约协议 v2 起，本迁移及实际渠道播种是领取硬前置；
--           渠道无 state 行时调度器拒绝领取，不再回退 keywords.status。
-- =====================================================

CREATE TABLE IF NOT EXISTS keyword_channel_state (
    id                BIGSERIAL PRIMARY KEY,
    keyword_id        BIGINT NOT NULL REFERENCES keywords(id) ON DELETE CASCADE,
    channel           TEXT NOT NULL,                 -- souyisou | sogou
    status            TEXT NOT NULL DEFAULT 'pending',   -- pending | running | completed | failed
    claimer           TEXT,                          -- 领取该(词,渠道)的 device_id
    last_claimed      TIMESTAMPTZ,
    next_collect_time TIMESTAMPTZ,
    last_collect_time TIMESTAMPTZ,
    last_count        INTEGER DEFAULT 0,
    created_at        TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE (keyword_id, channel)
);
CREATE INDEX IF NOT EXISTS idx_kcs_channel_status ON keyword_channel_state(channel, status, next_collect_time);
CREATE INDEX IF NOT EXISTS idx_kcs_keyword ON keyword_channel_state(keyword_id);

-- ===================================================================
-- 校验与提示
-- ===================================================================
DO $$
BEGIN
    RAISE NOTICE '✅ Migration 008 completed successfully.';
    RAISE NOTICE '  - keyword_channel_state table created (per-(keyword,channel) scheduling)';
    RAISE NOTICE '  - seed every active channel before enabling scheduler claims';
END $$;
