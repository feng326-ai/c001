-- 014: 测试期反馈机制 —— 每条线索的人工分型标注 + 站内 bug 反馈表。
-- 设计要点：
--   1) human_label 存 lead_user_state（每人一份，与 llm_feedback 同源）：
--      不同测试员对同一条线索可各标各的、互不覆盖，保留分歧信号，最适合喂给 AI 学习。
--   2) bug_feedback 为独立表：承载测试员就地提交的 bug（含"重复活动"类），
--      自动带上涉及的线索 ID 数组、页面 URL、提交人；管理员在站内页查看/标记已处理。
-- 全部 IF NOT EXISTS / ADD COLUMN，秒级、不锁表、不动存量数据，旧代码读不到新列照常运行。

-- ① 每条线索的人工分型（搜集人员视角）：有活动/无效/优质/普通/垃圾；空串=未标。
ALTER TABLE lead_user_state
    ADD COLUMN IF NOT EXISTS human_label TEXT DEFAULT '';

-- ② 站内 bug 反馈
CREATE TABLE IF NOT EXISTS bug_feedback (
    id           SERIAL PRIMARY KEY,
    category     TEXT NOT NULL,              -- 重复活动 | 漏判 | 误判为广告 | 清洗错误 | 其他
    description  TEXT DEFAULT '',            -- 测试员的文字描述
    lead_ids     BIGINT[] DEFAULT '{}',      -- 涉及的线索 ID（重复活动=被选中的多条；其他=可空）
    page_url     TEXT DEFAULT '',            -- 提交时所在页面（便于复现上下文）
    created_by   INTEGER REFERENCES users(id),  -- 提交人（NULL 容错，删用户不阻断）
    created_by_name TEXT DEFAULT '',         -- 冗余存用户名，避免 join；删用户后仍可追溯
    status       TEXT NOT NULL DEFAULT 'open',  -- open=待处理 | resolved=已处理 | wontfix=不处理
    resolved_by  INTEGER REFERENCES users(id),
    resolved_at  TIMESTAMPTZ,
    admin_note   TEXT DEFAULT '',            -- 管理员处理备注
    created_at   TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_bf_status  ON bug_feedback (status);
CREATE INDEX IF NOT EXISTS idx_bf_created ON bug_feedback (created_at DESC);
CREATE INDEX IF NOT EXISTS idx_bf_category ON bug_feedback (category);

DO $$ BEGIN
    RAISE NOTICE '014 applied: lead_user_state.human_label + bug_feedback';
END $$;
