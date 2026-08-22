-- ============================================
-- 微信搜一搜智能采集器 - 数据库初始化脚本
-- ============================================
-- 版本：1.0.0
-- 用途：创建所有必要的表和索引
-- ============================================

-- 创建数据库 (如果不存在)
-- CREATE DATABASE wx_search;
-- \c wx_search

-- ====================================================
-- 1. 关键词任务管理表
-- ====================================================
CREATE TABLE IF NOT EXISTS keywords (
    id BIGSERIAL PRIMARY KEY,
    keyword TEXT NOT NULL UNIQUE,          -- 核心关键词
    category TEXT,                          -- 分类标签
    weight INTEGER DEFAULT 1,               -- 权重 (1-10)
    status TEXT NOT NULL DEFAULT 'pending', -- pending/running/failed/stopped
    next_collect_time TIMESTAMPTZ,          -- 下次采集时间
    update_cycle_minutes INTEGER DEFAULT 20, -- 更新周期 (分钟)
    enabled BOOLEAN DEFAULT TRUE,           -- 是否启用
    collected_count INTEGER DEFAULT 0,       -- 已采集数量
    valid_lead_count INTEGER DEFAULT 0,      -- 有效线索数
    
    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW()
);

-- 索引优化
CREATE INDEX IF NOT EXISTS idx_keywords_status_next ON keywords(status, next_collect_time);
CREATE INDEX IF NOT EXISTS idx_keywords_enabled ON keywords(enabled);

-- ====================================================
-- 2. 文章主表 (去重后的主体)
-- ====================================================
CREATE TABLE IF NOT EXISTS articles_core (
    id BIGSERIAL PRIMARY KEY,
    
    -- 内容指纹
    content_hash TEXT NOT NULL UNIQUE,      -- SHA256 内容哈希
    url_fingerprint TEXT NOT NULL UNIQUE,   -- URL 指纹 (__biz-mid-idx-sn)
    canonical_url TEXT NOT NULL UNIQUE,     -- 标准化 URL
    
    -- 文章内容
    title TEXT NOT NULL,                    -- 文章标题
    summary TEXT NOT NULL,                  -- 摘要
    content TEXT,                           -- 原始内容 (HTML 或文本)
    
    -- 来源信息
    account TEXT NOT NULL,                  -- 公众号名称
    account_id TEXT,                        -- __biz (公众号 ID)
    source_channel TEXT NOT NULL,           -- wechat_pc / sogou_wap / baidu_news
    publish_time TIMESTAMPTZ,               -- 发布时间
    
    -- 关联信息
    keyword TEXT NOT NULL,                  -- 采集时使用的关键词
    collect_time TIMESTAMPTZ DEFAULT NOW(), -- 采集时间
    
    -- AI 评估字段
    intent_category TEXT,                   -- purchase/tender/cooperation/news/other
    has_lead_value BOOLEAN DEFAULT FALSE,   -- 是否有线索价值
    priority_score NUMERIC,                 -- 优先级分数 (0-100)
    priority_level TEXT,                    -- P0/P1/P2
    
    -- 元数据
    word_count INTEGER,                     -- 字数
    image_count INTEGER,                    -- 图片数
    
    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW()
);

-- 复合索引 (查询优化)
CREATE INDEX IF NOT EXISTS idx_articles_content_hash ON articles_core(content_hash);
CREATE INDEX IF NOT EXISTS idx_articles_keyword_publish ON articles_core(keyword, publish_time DESC);
CREATE INDEX IF NOT EXISTS idx_articles_intent ON articles_core(intent_category);
CREATE INDEX IF NOT EXISTS idx_articles_has_lead ON articles_core(has_lead_value, priority_score DESC);

-- ====================================================
-- 3. 文章集群表 (关联转载)
-- ====================================================
CREATE TABLE IF NOT EXISTS article_clusters (
    id BIGSERIAL PRIMARY KEY,
    
    primary_article_id BIGINT REFERENCES articles_core(id),
    content_hash TEXT NOT NULL UNIQUE,      -- 主文章的内容哈希
    
    mirror_count INTEGER DEFAULT 1,         -- 镜像数量
    channels TEXT[],                         -- 渠道分布 ["wechat_pc", "sogou_wap"]
    
    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW()
);

-- ====================================================
-- 4. 镜像链接表 (所有转载链接)
-- ====================================================
CREATE TABLE IF NOT EXISTS article_mirrors (
    id BIGSERIAL PRIMARY KEY,
    
    cluster_id BIGINT REFERENCES article_clusters(id) ON DELETE CASCADE,
    mirror_url TEXT NOT NULL UNIQUE,        -- 转载 URL
    mirror_source_channel TEXT NOT NULL,    -- 转载渠道
    
    UNIQUE(cluster_id, mirror_url)
);

-- ====================================================
-- 5. 线索记录表 (高价值内容)
-- ====================================================
CREATE TABLE IF NOT EXISTS qualified_leads (
    id BIGSERIAL PRIMARY KEY,
    
    lead_id BIGSERIAL,                       -- 独立线索 ID
    article_id BIGINT REFERENCES articles_core(id),
    
    -- 原文引用
    title TEXT NOT NULL,
    content TEXT,
    url TEXT NOT NULL,
    account TEXT NOT NULL,
    publish_time TIMESTAMPTZ,
    
    -- AI 评估结果
    intent_category TEXT NOT NULL,          -- purchase/tender/cooperation/news/other
    has_lead_value BOOLEAN DEFAULT TRUE,
    lead_type TEXT,                         -- 采购/投标/合作/资讯/其他
    priority_score NUMERIC DEFAULT 0.0,
    priority_level TEXT DEFAULT 'P2',       -- P0/P1/P2
    
    -- 跟踪状态
    status TEXT DEFAULT 'pending_followup', -- pending_followup/following/contacted/reserved/archived
    assigned_to TEXT,                       -- 负责人
    follow_up_deadline TIMESTAMPTZ,         -- 跟进截止日期
    
    -- LLM 分析
    llm_reasoning TEXT,                     -- AI 推理过程
    scoring_breakdown JSONB,                -- 评分详情
    
    -- 人工反馈
    was_relevant_by_human BOOLEAN,          -- 人工确认相关
    corrected_by TEXT,                      -- 修正人
    feedback_at TIMESTAMPTZ,
    
    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW()
);

-- 索引优化
CREATE INDEX IF NOT EXISTS idx_leads_article ON qualified_leads(article_id);
CREATE INDEX IF NOT EXISTS idx_leads_intent ON qualified_leads(intent_category);
CREATE INDEX IF NOT EXISTS idx_leads_priority ON qualified_leads(priority_level, priority_score DESC);
CREATE INDEX IF NOT EXISTS idx_leads_status ON qualified_leads(status);

-- ====================================================
-- 6. 采集任务记录表
-- ====================================================
CREATE TABLE IF NOT EXISTS collection_tasks (
    id BIGSERIAL PRIMARY KEY,
    
    keyword TEXT NOT NULL,
    channel TEXT NOT NULL,              -- wechat_pc/sogou_wap/baidu_news/weixin_mobile
    vm_instance_id TEXT,                -- VM 实例 ID
    
    articles_count INTEGER NOT NULL,    -- 采集数量
    success BOOLEAN NOT NULL,           -- 是否成功
    
    start_time TIMESTAMPTZ NOT NULL,
    end_time TIMESTAMPTZ,
    error_message TEXT,
    
    created_at TIMESTAMPTZ DEFAULT NOW()
);

-- 索引优化
CREATE INDEX IF NOT EXISTS idx_tasks_vm_time ON collection_tasks(vm_instance_id, end_time DESC);
CREATE INDEX IF NOT EXISTS idx_tasks_success ON collection_tasks(success, end_time DESC);

-- ====================================================
-- 7. 系统配置表
-- ====================================================
CREATE TABLE IF NOT EXISTS system_config (
    config_key TEXT PRIMARY KEY,
    config_value TEXT NOT NULL,
    description TEXT,
    updated_at TIMESTAMPTZ DEFAULT NOW()
);

-- 插入默认配置
INSERT INTO system_config (config_key, config_value, description) VALUES
    ('system_version', '1.0.0', '系统版本号'),
    ('max_concurrent_workers', '4', '最大并发采集数'),
    ('daily_quota_per_keyword', '100', '每关键词每日上限'),
    ('dedup_similarity_threshold', '0.90', '相似度去重阈值'),
    ('collection_cycle_minutes', '20', '采集周期 (分钟)'),
    ('ai_filter_enabled', 'true', 'AI 过滤器启用')
ON CONFLICT (config_key) DO NOTHING;

-- ====================================================
-- 8. 物化视图 (性能优化)
-- ====================================================

-- 今日新增统计
CREATE MATERIALIZED VIEW IF NOT EXISTS mv_daily_stats AS
SELECT 
    DATE(created_at) as stat_date,
    COUNT(*) as total_articles,
    COUNT(DISTINCT intent_category) as categories_found,
    AVG(priority_score) as avg_priority,
    COUNT(CASE WHEN has_lead_value = true THEN 1 END) as potential_leads
FROM articles_core
WHERE collect_time >= NOW() - INTERVAL '1 day'
GROUP BY DATE(created_at);

-- 关键词采集效率
CREATE MATERIALIZED VIEW IF NOT EXISTS mv_keyword_efficiency AS
SELECT 
    k.keyword,
    k.category,
    k.status,
    COUNT(ac.id) as articles_collected,
    COUNT(DISTINCT ac.intent_category) as unique_intents,
    MAX(ac.collect_time) as last_collect_time,
    ROUND(AVG(ac.priority_score), 2) as avg_priority
FROM keywords k
LEFT JOIN articles_core ac ON k.keyword = ac.keyword
GROUP BY k.id, k.keyword, k.category, k.status
ORDER BY articles_collected DESC;

-- ====================================================
-- 9. 触发器 (自动更新时间戳)
-- ====================================================
CREATE OR REPLACE FUNCTION update_updated_at_column()
RETURNS TRIGGER AS $$
BEGIN
    NEW.updated_at = NOW();
    RETURN NEW;
END;
$$ language 'plpgsql';

CREATE TRIGGER trigger_keywords_updated_at
BEFORE UPDATE ON keywords
FOR EACH ROW EXECUTE FUNCTION update_updated_at_column();

CREATE TRIGGER trigger_articles_updated_at
BEFORE UPDATE ON articles_core
FOR EACH ROW EXECUTE FUNCTION update_updated_at_column();

-- ====================================================
-- 验证与提示
-- ====================================================
DO $$
BEGIN
    RAISE NOTICE '✅ 数据库结构初始化完成!';
    RAISE NOTICE '总表数：%', (SELECT count(*) FROM pg_tables WHERE schemaname = 'public');
    RAISE NOTICE '索引数：%', (SELECT count(*) FROM pg_indexes WHERE schemaname = 'public');
    RAISE NOTICE '物化视图：%', (SELECT count(*) FROM pg_matviews WHERE schemaname = 'public');
END $$;
