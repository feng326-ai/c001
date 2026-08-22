-- 011: 关键词按渠道差异化更新周期。
-- keyword_channel_state.update_cycle_minutes：每「词×渠道」可独立覆盖采集周期。
-- 为 NULL 时回退渠道默认（搜一搜 souyisou=20 快 / 搜狗 sogou=180 广），再回退 keywords.update_cycle_minutes，最后 20。
-- 支撑：核心词分发搜一搜、力求更新快；拓展词分发搜狗、覆盖广但频率低。

ALTER TABLE keyword_channel_state
    ADD COLUMN IF NOT EXISTS update_cycle_minutes INTEGER DEFAULT NULL;
