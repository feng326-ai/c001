-- 012: 线索详情交互增强所需字段。
-- in_library：是否加入「我的活动库」（用户手动收藏，供活动库页筛选展示）。
-- llm_feedback：对 LLM 判定的人工反馈，0=未评/1=赞成/-1=反对（用于后续评估提示词质量）。

ALTER TABLE qualified_leads
    ADD COLUMN IF NOT EXISTS in_library BOOLEAN DEFAULT FALSE,
    ADD COLUMN IF NOT EXISTS llm_feedback SMALLINT DEFAULT 0;

CREATE INDEX IF NOT EXISTS idx_ql_in_library ON qualified_leads (in_library) WHERE in_library = TRUE;
