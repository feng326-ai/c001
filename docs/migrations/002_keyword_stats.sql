-- =====================================================
-- 迁移 002: 关键词效果统计 (keyword_article_map + 统计视图)
-- 版本：v1.0 (云端首次部署/现有库升级均可幂等执行)
-- 用途：为“关键词统计”功能提供地基，支持长期分析
-- =====================================================

-- ===================================================================
-- 1. keyword_article_map: 规范化 keyword ↔ article 映射表
--    记录每个 keyword 命中了哪些文章，便于多维度归因与统计
-- ===================================================================

CREATE TABLE IF NOT EXISTS keyword_article_map (
    id BIGSERIAL PRIMARY KEY,
    keyword TEXT NOT NULL,
    article_id BIGINT NOT NULL REFERENCES articles_core(id) ON DELETE CASCADE,
    first_seen_at TIMESTAMPTZ DEFAULT now(),
    last_seen_at TIMESTAMPTZ DEFAULT now(),
    match_count INT DEFAULT 1,
    UNIQUE(keyword, article_id)
);

-- 索引优化查询
CREATE INDEX IF NOT EXISTS idx_kam_article ON keyword_article_map(article_id);
CREATE INDEX IF NOT EXISTS idx_kam_keyword ON keyword_article_map(keyword);
CREATE INDEX IF NOT EXISTS idx_kam_keywords_seq ON keyword_article_map(keyword, last_seen_at DESC);

-- ===================================================================
-- 2. kw_stat_basic: 基础统计视图 (关键词概览报表)
--    返回每个 keyword 的核心指标
-- ===================================================================

DROP VIEW IF EXISTS kw_stat_basic;
CREATE VIEW kw_stat_basic AS
SELECT 
    k.keyword,
    COUNT(DISTINCT kam.article_id) AS articles_count,
    COUNT(DISTINCT ql.id) AS leads_count,
    ROUND(AVG(ql.priority_score),2) AS avg_score,
    ROUND(AVG(CASE WHEN ql.resource_level = 'excellent' THEN 1.0 ELSE 0.0 END)*100,2) AS excellent_rate,
    COUNT(*) FILTER (WHERE ql.intent_category IN ('评选','投票','征集','活动')) AS intent_events,
    COUNT(*) FILTER (WHERE ql.publish_time >= now() - interval '30 days') AS recent_30d,
    COUNT(*) FILTER (WHERE ql.publish_time >= now() - interval '7 days') AS recent_7d,
    MAX(ql.created_at) FILTER (WHERE ql.created_at IS NOT NULL) AS last_lead_date
FROM keywords k
LEFT JOIN keyword_article_map kam ON kam.keyword = k.keyword
LEFT JOIN qualified_leads ql ON ql.article_id = kam.article_id
GROUP BY k.keyword
ORDER BY leads_count DESC;


-- ===================================================================
-- 3. kw_stat_intent: 意图分布视图 (按分类拆解)
--    查看某 keyword 在各意图类别中的表现
-- ===================================================================

DROP VIEW IF EXISTS kw_stat_intent;
CREATE VIEW kw_stat_intent AS
SELECT 
    k.keyword,
    ql.intent_category AS category,
    COUNT(*) AS count,
    ROUND(AVG(ql.priority_score),2) AS avg_score,
    ROUND(AVG(CASE WHEN ql.resource_level = 'excellent' THEN 1.0 ELSE 0.0 END)*100,2) AS excellent_rate,
    COUNT(*) FILTER (WHERE ql.resource_level = 'excellent') AS excellent_count
FROM keywords k
LEFT JOIN keyword_article_map kam ON kam.keyword = k.keyword
LEFT JOIN qualified_leads ql ON ql.article_id = kam.article_id
GROUP BY k.keyword, ql.intent_category
ORDER BY k.keyword, count DESC;


-- ===================================================================
-- 4. 历史数据回补: 从现有 qualified_leads + articles_core.keywords
--    把已有数据填充到 keyword_article_map 表中
-- ===================================================================

DO $backfill$
DECLARE
    rec RECORD;
    existing_count INT;
BEGIN
    -- 只运行一次：先检查是否已有数据
    SELECT COUNT(*) INTO existing_count FROM keyword_article_map;
    
    IF existing_count > 0 THEN
        RAISE NOTICE '⚠️ keyword_article_map 已有数据，跳过历史回补';
        RETURN;
    END IF;

    RAISE NOTICE '🔧 开始回补历史数据...';

    -- 从 qualified_leads 逐条插入 (利用 keywords ARRAY)
    FOR rec IN
        SELECT DISTINCT qa.keyword, a.id AS article_id
        FROM qualified_leads qa
        JOIN articles_core a ON a.id = qa.article_id
        WHERE a.keywords IS NOT NULL AND array_length(a.keywords, 1) > 0
    LOOP
        INSERT INTO keyword_article_map (keyword, article_id, match_count)
        VALUES (rec.keyword, rec.article_id, 1)
        ON CONFLICT(keyword, article_id) DO UPDATE SET
            last_seen_at = now(),
            match_count = keyword_article_map.match_count + 1;
    END LOOP;

    RAISE NOTICE '✅ 历史数据回补完成';
    RAISE NOTICE '   keyword_article_map 总行数：%', (SELECT COUNT(*) FROM keyword_article_map);
END $backfill$;


-- ===================================================================
-- 5. 校验与提示
-- ===================================================================

DO $$
BEGIN
    RAISE NOTICE '✅ Migration 002 completed successfully.';
    RAISE NOTICE '  - keyword_article_map table created';
    RAISE NOTICE '  - kw_stat_basic view created (basic metrics per keyword)';
    RAISE NOTICE '  - kw_stat_intent view created (intent breakdown per keyword)';
END $$;
