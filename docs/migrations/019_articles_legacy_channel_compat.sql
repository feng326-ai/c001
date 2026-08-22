-- 019: 补齐从旧 JSONB channels 演进到 TEXT[] 时保留的兼容列。
-- 001 在旧库升级路径会重命名出此列，但全新/较新基础库没有旧列，导致两条建库路径结构不一致。

ALTER TABLE articles_core
    ADD COLUMN IF NOT EXISTS channels_jsonb_legacy JSONB DEFAULT '["wechat_pc"]'::jsonb;
