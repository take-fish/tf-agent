-- Agent 展示配置：名称 / 3D 形象 / 问候语 / 推荐问题，单行 id='default'。
-- 读不到就用代码里的默认值，所以这张表缺失也不会导致对话不可用。
CREATE TABLE IF NOT EXISTS agent_profile (
    id VARCHAR(16) NOT NULL,
    config LONGTEXT NOT NULL,
    updated_by BIGINT NULL,
    updated_at DATETIME(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3) ON UPDATE CURRENT_TIMESTAMP(3),
    PRIMARY KEY (id)
) ENGINE = InnoDB DEFAULT CHARSET = utf8mb4 COLLATE = utf8mb4_0900_ai_ci;
