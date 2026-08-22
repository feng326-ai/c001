-- 015: 采集机日志回传 —— VM 端循环把运行日志批量 POST 上来，管理页实时展示/告警。
-- 设计要点：
--   1) 只存最近日志：写入即写死保留 20000 条以内（上报端点顺手裁剪），避免无限膨胀。
--   2) device_id + level + ts 建索引，页面按设备/级别拉取。
--   3) 全 IF NOT EXISTS，秒级、不锁表、不动存量数据。

CREATE TABLE IF NOT EXISTS collect_logs (
    id         BIGSERIAL PRIMARY KEY,
    device_id  TEXT NOT NULL DEFAULT '',        -- 上报设备号（如 sogou-vm-01）
    level      TEXT NOT NULL DEFAULT 'INFO',    -- DEBUG/INFO/WARNING/ERROR/CRITICAL
    message    TEXT NOT NULL,
    created_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_clogs_device_ts ON collect_logs (device_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_clogs_level     ON collect_logs (level, created_at DESC);

DO $$ BEGIN
    RAISE NOTICE '015 applied: collect_logs';
END $$;
