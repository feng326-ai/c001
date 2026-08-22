-- 010: 多渠道命中标记 —— 一篇文章可被 搜一搜(wechat_pc) 与 搜狗(sogou_weixin) 等多个渠道采到。
-- source_channels 记录该文章被哪些渠道命中（逗号分隔，去重），供前端渠道列显示多个徽章。
-- 主记录仍以 source_channel 表示“主渠道”（以搜一搜为主，见 smart_dedup_store 的升级逻辑）。

ALTER TABLE articles_core
    ADD COLUMN IF NOT EXISTS source_channels TEXT DEFAULT NULL;

-- 存量初始化：未设置的用当前主渠道填充。
UPDATE articles_core
SET source_channels = source_channel
WHERE source_channels IS NULL OR source_channels = '';
