-- 013: 多团队使用（第一期）—— 团队、用户、每用户线索个人状态。
-- 公海/AI活动库线索池共享；已处理/回收站(隐藏)/我的活动库/备注/点赞点踩 改为按用户隔离。

CREATE TABLE IF NOT EXISTS teams (
    id          SERIAL PRIMARY KEY,
    name        TEXT UNIQUE NOT NULL,
    created_at  TIMESTAMPTZ DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS users (
    id            SERIAL PRIMARY KEY,
    username      TEXT UNIQUE NOT NULL,
    password_hash TEXT NOT NULL,
    team_id       INTEGER REFERENCES teams(id),
    role          TEXT NOT NULL DEFAULT 'member' CHECK (role IN ('admin', 'member', 'super')),
    enabled       BOOLEAN DEFAULT TRUE,
    created_at    TIMESTAMPTZ DEFAULT NOW()
);

-- 每「用户 × 线索」个人状态：只对该用户可见。
CREATE TABLE IF NOT EXISTS lead_user_state (
    user_id       INTEGER NOT NULL REFERENCES users(id),
    lead_id       INTEGER NOT NULL REFERENCES qualified_leads(id),
    processed     BOOLEAN DEFAULT FALSE,   -- 已处理
    hidden        BOOLEAN DEFAULT FALSE,   -- 回收站(仅自己隐藏)
    in_library    BOOLEAN DEFAULT FALSE,   -- 我的活动库
    notes         TEXT,                    -- 个人备注
    llm_feedback  SMALLINT DEFAULT 0,      -- 对LLM判定的反馈 1赞/-1踩/0无
    updated_at    TIMESTAMPTZ DEFAULT NOW(),
    PRIMARY KEY (user_id, lead_id)
);

CREATE INDEX IF NOT EXISTS idx_lus_user ON lead_user_state (user_id);
CREATE INDEX IF NOT EXISTS idx_lus_lead ON lead_user_state (lead_id);
CREATE INDEX IF NOT EXISTS idx_lus_user_lib ON lead_user_state (user_id) WHERE in_library = TRUE;
