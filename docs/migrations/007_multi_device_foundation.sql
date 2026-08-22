-- =====================================================
-- 迁移 007: 多设备采集地基 (设备注册 + 每机采集历史 + 关键词渠道归属)
-- 版本：v1.0 (幂等)
-- 用途：
--   - devices               设备注册与心跳(在线/健康/当前在采)
--   - keywords.channels[]    该关键词参与哪些采集渠道(souyisou/sogou)
--   - collect_tasks 补列      device_id(=vm_instance 语义) + 唯一/索引完善
-- 说明：不回填历史；新采集/新上报自动填充。
-- =====================================================

-- 1. 设备注册与心跳表
CREATE TABLE IF NOT EXISTS devices (
    device_id       TEXT PRIMARY KEY,          -- = unattended.vm_instance_id, 如 pc-01/phone-01
    device_type     TEXT DEFAULT 'pc',         -- phone | pc
    channel         TEXT DEFAULT 'souyisou',   -- souyisou(搜一搜) | sogou(搜狗)
    status          TEXT DEFAULT 'online',     -- online | offline
    current_keyword TEXT,                      -- 当前正在采的关键词
    last_heartbeat  TIMESTAMPTZ,               -- 最近心跳时间(在线判定用)
    started_at      TIMESTAMPTZ DEFAULT NOW(),
    note            TEXT
);
CREATE INDEX IF NOT EXISTS idx_devices_channel ON devices(channel);
CREATE INDEX IF NOT EXISTS idx_devices_heartbeat ON devices(last_heartbeat DESC);

-- 2. keywords 增加渠道归属数组(该词参与哪些渠道采集), 默认两渠道都采
ALTER TABLE keywords ADD COLUMN IF NOT EXISTS channels TEXT[] DEFAULT ARRAY['souyisou','sogou'];
UPDATE keywords SET channels = ARRAY['souyisou','sogou'] WHERE channels IS NULL;
CREATE INDEX IF NOT EXISTS idx_kw_channels ON keywords USING GIN (channels);

-- 3. collect_tasks: 补 device_id 列(与 vm_instance 并存, device_id 为规范列)
ALTER TABLE collect_tasks ADD COLUMN IF NOT EXISTS device_id TEXT;
CREATE INDEX IF NOT EXISTS idx_ct_device_time ON collect_tasks(device_id, start_time DESC);
CREATE INDEX IF NOT EXISTS idx_ct_start_time ON collect_tasks(start_time DESC);

-- ===================================================================
-- 校验与提示
-- ===================================================================
DO $$
BEGIN
    RAISE NOTICE '✅ Migration 007 completed successfully.';
    RAISE NOTICE '  - devices table created';
    RAISE NOTICE '  - keywords.channels[] added (default souyisou+sogou)';
    RAISE NOTICE '  - collect_tasks.device_id added';
END $$;
