-- =====================================================
-- 微信搜一搜智能采集系统 - 完整数据库架构 (PostgreSQL 15)
-- 目标：支持多渠道采集 +AI 清洗 + 线索价值评估
-- 版本：v1.1 (修正 PG 方言：内联 INDEX 拆分为独立 CREATE INDEX；补 simhash 列)
-- =====================================================

-- 启用扩展
CREATE EXTENSION IF NOT EXISTS "uuid-ossp";
CREATE EXTENSION IF NOT EXISTS "pg_trgm";  -- 文本相似度

-- =====================================================
-- 1. 关键词任务管理表
-- =====================================================

CREATE TABLE IF NOT EXISTS keywords (
    id BIGSERIAL PRIMARY KEY,

    keyword TEXT NOT NULL UNIQUE,           -- 采集词本身
    category TEXT,                          -- 词分类 (蓝海库关联)
    weight INTEGER DEFAULT 1,               -- 优先级权重 (高权重优先采)

    status TEXT NOT NULL DEFAULT 'pending', -- pending / running / completed / failed

    created_at TIMESTAMPTZ DEFAULT NOW(),
    first_collect_time TIMESTAMPTZ,
    last_collect_time TIMESTAMPTZ,
    next_collect_time TIMESTAMPTZ,

    total_collected INTEGER DEFAULT 0,
    last_collected_count INTEGER,
    avg_daily_count NUMERIC,

    update_cycle_minutes INTEGER DEFAULT 20,
    enabled BOOLEAN DEFAULT TRUE
);
CREATE INDEX IF NOT EXISTS idx_kw_status_next ON keywords(status, next_collect_time);
CREATE INDEX IF NOT EXISTS idx_kw_enabled ON keywords(enabled);
CREATE INDEX IF NOT EXISTS idx_kw_weight ON keywords(weight DESC);

CREATE TABLE IF NOT EXISTS collect_tasks (
    id BIGSERIAL PRIMARY KEY,

    keyword_id BIGINT REFERENCES keywords(id),
    channel TEXT NOT NULL,                  -- wechat_pc / sogou_wap / baidu_news
    vm_instance TEXT,

    status TEXT NOT NULL,                   -- started / completed / failed
    articles_count INTEGER,
    start_time TIMESTAMPTZ DEFAULT NOW(),
    end_time TIMESTAMPTZ,

    error_message TEXT
);
CREATE INDEX IF NOT EXISTS idx_ct_keyword_channel ON collect_tasks(keyword_id, channel);
CREATE INDEX IF NOT EXISTS idx_ct_vm_time ON collect_tasks(vm_instance, start_time DESC);

-- =====================================================
-- 2. 文章主表 (去重后的主体)
-- =====================================================

CREATE TABLE IF NOT EXISTS articles_core (
    id BIGSERIAL PRIMARY KEY,
    uuid uuid DEFAULT uuid_generate_v4() UNIQUE,

    -- 核心指纹 (防重关键)
    content_hash TEXT NOT NULL,             -- 内容 SHA256
    url_fingerprint TEXT NOT NULL,          -- __biz-mid-idx-sn
    simhash TEXT,                           -- SimHash 指纹 (十进制字符串, 近似去重用)

    -- 文章基本信息
    title TEXT NOT NULL,
    summary TEXT NOT NULL,
    account TEXT NOT NULL,
    account_id TEXT,
    mid TEXT,
    idx TEXT,
    sn TEXT,
    publish_time TIMESTAMPTZ,
    source_date DATE,

    -- URL 和来源
    canonical_url TEXT NOT NULL,
    original_url TEXT NOT NULL,
    source_channel TEXT NOT NULL,
    keyword TEXT NOT NULL,

    -- AI 评估字段
    intent_category TEXT,
    has_lead_value BOOLEAN DEFAULT FALSE,
    lead_type TEXT,
    priority_score NUMERIC,
    priority_level TEXT,
    scoring_breakdown JSONB,
    llm_reasoning TEXT,

    -- 原始内容
    content TEXT,
    content_clean TEXT,

    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW(),

    -- 唯一约束 (防重核心)
    UNIQUE (content_hash),
    UNIQUE (url_fingerprint),
    UNIQUE (canonical_url)
);
CREATE INDEX IF NOT EXISTS idx_ac_content_hash ON articles_core(content_hash);
CREATE INDEX IF NOT EXISTS idx_ac_simhash ON articles_core(simhash);
CREATE INDEX IF NOT EXISTS idx_ac_publish_time ON articles_core(publish_time DESC);
CREATE INDEX IF NOT EXISTS idx_ac_source_date ON articles_core(source_date);
CREATE INDEX IF NOT EXISTS idx_ac_account ON articles_core(account);
CREATE INDEX IF NOT EXISTS idx_ac_keyword ON articles_core(keyword);
CREATE INDEX IF NOT EXISTS idx_ac_intent ON articles_core(intent_category);
CREATE INDEX IF NOT EXISTS idx_ac_priority ON articles_core(priority_score DESC);

-- =====================================================
-- 3. 镜像表 (转载/多渠道链接)
-- =====================================================

CREATE TABLE IF NOT EXISTS article_clusters (
    id BIGSERIAL PRIMARY KEY,

    primary_article_id BIGINT REFERENCES articles_core(id) ON DELETE CASCADE,

    content_hash TEXT NOT NULL UNIQUE,

    mirror_count INTEGER DEFAULT 1,
    channels TEXT[],

    created_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS article_mirrors (
    id BIGSERIAL PRIMARY KEY,

    cluster_id BIGINT REFERENCES article_clusters(id) ON DELETE CASCADE,

    mirror_url TEXT NOT NULL,
    mirror_source_channel TEXT NOT NULL,
    mirror_account TEXT,
    mirror_publish_time TIMESTAMPTZ,

    UNIQUE(cluster_id, mirror_url)
);
CREATE INDEX IF NOT EXISTS idx_mirrors_url ON article_mirrors(mirror_url);
CREATE INDEX IF NOT EXISTS idx_mirrors_cluster ON article_mirrors(cluster_id);

-- =====================================================
-- 4. 合格线索表 (通过 AI 筛选的高价值线索)
-- =====================================================

CREATE TABLE IF NOT EXISTS qualified_leads (
    id BIGSERIAL PRIMARY KEY,

    article_id BIGINT REFERENCES articles_core(id),
    title TEXT NOT NULL,
    summary TEXT,
    content TEXT,
    url TEXT NOT NULL UNIQUE,
    account TEXT NOT NULL,
    publish_time TIMESTAMPTZ,
    source_channel TEXT,
    keyword TEXT,

    intent_category TEXT NOT NULL,
    has_lead_value BOOLEAN DEFAULT TRUE,
    lead_type TEXT,
    priority_score NUMERIC NOT NULL,
    priority_level TEXT NOT NULL,
    scoring_breakdown JSONB,
    llm_reasoning TEXT,

    status TEXT NOT NULL DEFAULT 'pending_followup',

    assigned_to TEXT,
    follow_up_deadline TIMESTAMPTZ,
    last_contacted_at TIMESTAMPTZ,
    conversion_notes TEXT,

    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_ql_priority_score ON qualified_leads(priority_score DESC);
CREATE INDEX IF NOT EXISTS idx_ql_status ON qualified_leads(status);
CREATE INDEX IF NOT EXISTS idx_ql_intent ON qualified_leads(intent_category);
CREATE INDEX IF NOT EXISTS idx_ql_created ON qualified_leads(created_at DESC);

-- =====================================================
-- 5. 线索反馈表 (用于模型迭代)
-- =====================================================

CREATE TABLE IF NOT EXISTS lead_feedback (
    id BIGSERIAL PRIMARY KEY,

    lead_id BIGINT REFERENCES qualified_leads(id),

    was_relevant BOOLEAN NOT NULL,
    relevance_score INTEGER,
    corrected_category TEXT,
    tags TEXT[],

    marked_by TEXT,
    mark_method TEXT,

    created_at TIMESTAMPTZ DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_lf_lead_id ON lead_feedback(lead_id);
CREATE INDEX IF NOT EXISTS idx_lf_was_relevant ON lead_feedback(was_relevant);

-- =====================================================
-- 6. 物化视图 (预聚合统计表)
-- =====================================================

CREATE MATERIALIZED VIEW IF NOT EXISTS mv_daily_keywords_summary AS
SELECT
    k.id,
    k.keyword,
    k.category,
    k.status,
    DATE(ct.start_time) as date,
    COUNT(ct.id) as task_count,
    SUM(ct.articles_count) as total_articles,
    AVG(ct.articles_count::numeric) as avg_articles
FROM keywords k
LEFT JOIN collect_tasks ct ON k.id = ct.keyword_id AND DATE(ct.start_time) = CURRENT_DATE
GROUP BY k.id, k.keyword, k.category, k.status, DATE(ct.start_time);

CREATE UNIQUE INDEX IF NOT EXISTS idx_md_key_date ON mv_daily_keywords_summary(id, date);

CREATE MATERIALIZED VIEW IF NOT EXISTS mv_daily_leads_summary AS
SELECT
    DATE(created_at) as date,
    intent_category,
    priority_level,
    COUNT(*) as count,
    AVG(priority_score) as avg_score,
    MIN(priority_score) as min_score,
    MAX(priority_score) as max_score
FROM qualified_leads
WHERE DATE(created_at) >= CURRENT_DATE - INTERVAL '30 days'
GROUP BY DATE(created_at), intent_category, priority_level;

CREATE UNIQUE INDEX IF NOT EXISTS idx_md_leads ON mv_daily_leads_summary(date, intent_category, priority_level);

-- =====================================================
-- 7. 示例数据插入 (测试用)
-- =====================================================

INSERT INTO keywords (keyword, category, weight, enabled) VALUES
('评选征集', '商业机会', 2, true),
('人工智能', '行业趋势', 2, true),
('工业自动化', '采购需求', 3, true),
('传感器采购', 'B2B', 3, true),
('政府招标', 'Government', 1, true)
ON CONFLICT(keyword) DO NOTHING;

-- =====================================================
-- END OF FILE
-- =====================================================
