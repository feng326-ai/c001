-- 018: 关键词价值评分视图（P0 运营优化）
-- 基于 kw_stat_basic 实时计算关键词的产量、质量、优质率、转化率、新鲜度和趋势，
-- 不存储额外状态，可通过 CREATE OR REPLACE 安全重复执行。

CREATE OR REPLACE VIEW kw_stat_value AS
WITH base AS (
    SELECT
        b.*,
        CASE WHEN b.articles_count > 0
             THEN round(100.0 * b.leads_count / b.articles_count, 1)
             ELSE 0 END AS conversion_rate,
        CASE WHEN b.last_lead_date IS NOT NULL
             THEN EXTRACT(EPOCH FROM (now() - b.last_lead_date)) / 86400.0
             ELSE NULL END AS days_since_lead
    FROM kw_stat_basic b
),
scored AS (
    SELECT base.*,
        LEAST(100, 100.0 * ln((leads_count + 1)::numeric) / ln(3001::numeric)) AS s_volume,
        LEAST(100, GREATEST(0, COALESCE(avg_score, 0)))                        AS s_quality,
        LEAST(100, GREATEST(0, COALESCE(excellent_rate, 0)))                   AS s_excellent,
        LEAST(100, conversion_rate)                                            AS s_conversion,
        CASE WHEN days_since_lead IS NULL THEN 0
             ELSE GREATEST(0, 100 - days_since_lead * (100.0/30)) END          AS s_freshness,
        CASE WHEN recent_30d = 0 THEN (CASE WHEN recent_7d > 0 THEN 100 ELSE 50 END)
             ELSE LEAST(100, 50.0 * recent_7d / NULLIF(recent_30d/4.0, 0)) END AS s_trend
    FROM base
),
valued AS (
    SELECT scored.*,
        round(0.25*s_volume + 0.25*s_quality + 0.20*s_excellent
            + 0.15*s_conversion + 0.10*s_freshness + 0.05*s_trend, 1) AS value_score
    FROM scored
)
SELECT valued.*,
    CASE
        WHEN articles_count < 30                      THEN '数据不足'
        WHEN value_score >= 65                        THEN '优质·建议高频'
        WHEN conversion_rate < 50 OR value_score < 50 THEN '低效·建议降频/淘汰'
        WHEN days_since_lead > 14                     THEN '观察·可能老化'
        ELSE '正常'
    END AS suggestion
FROM valued
ORDER BY value_score DESC;
