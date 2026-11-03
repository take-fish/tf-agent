-- 会话标题：列表展示用（取第一条用户消息），历史会话标题留空时由写入侧补。
ALTER TABLE agent_session
    ADD COLUMN title VARCHAR(120) NOT NULL DEFAULT '' AFTER env;

CREATE INDEX idx_agent_session_user_updated ON agent_session (user_id, updated_at);
