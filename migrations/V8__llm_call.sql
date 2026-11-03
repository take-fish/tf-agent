-- LLM 调用流水：每次模型轮次一条，记 token 用量与耗时，用于算成本 / 找慢轮次。
-- 工具调用本身的审计仍在 agent_tool_call，本表只回答"这次回答花了多少 token"。
CREATE TABLE IF NOT EXISTS agent_llm_call (
    id BIGINT NOT NULL AUTO_INCREMENT,
    trace_id VARCHAR(32) NOT NULL,
    user_id BIGINT NOT NULL,
    scene VARCHAR(32) NOT NULL DEFAULT '',
    `round` INT NOT NULL DEFAULT 0,
    model VARCHAR(64) NOT NULL DEFAULT '',
    prompt_tokens INT NOT NULL DEFAULT 0,
    completion_tokens INT NOT NULL DEFAULT 0,
    ms INT NOT NULL DEFAULT 0,
    created_at DATETIME(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
    PRIMARY KEY (id),
    KEY idx_agent_llm_call_trace (trace_id),
    KEY idx_agent_llm_call_user (user_id)
) ENGINE = InnoDB DEFAULT CHARSET = utf8mb4 COLLATE = utf8mb4_0900_ai_ci;
