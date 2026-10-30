-- fm_agent 初始化表结构（V1）
--
-- 由 `python -m app.migrate` 以 fm_agent_migrate 账号执行；
-- 业务容器只用 fm_agent_app（仅 DML），启动时不再建表，只校验表是否齐全。
--
-- 约定：文件名 V<序号>__<说明>.sql，按序号顺序应用，已应用的版本记录在 schema_version。

CREATE TABLE IF NOT EXISTS `agent_session` (
  `id` VARCHAR(40) NOT NULL,
  `user_id` BIGINT NOT NULL,
  `scene` VARCHAR(32) NOT NULL DEFAULT 'ops',
  `env` VARCHAR(8) NOT NULL DEFAULT 'prod',
  `messages` JSON NOT NULL,
  `updated_at` DATETIME(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3) ON UPDATE CURRENT_TIMESTAMP(3),
  PRIMARY KEY (`id`),
  KEY `ix_agent_session_user_id` (`user_id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='Agent 会话（只保留最近 N 条消息）';

CREATE TABLE IF NOT EXISTS `agent_tool_call` (
  `id` BIGINT NOT NULL AUTO_INCREMENT,
  `trace_id` VARCHAR(32) NOT NULL,
  `user_id` BIGINT NOT NULL,
  `scene` VARCHAR(32) NOT NULL DEFAULT 'ops',
  `env` VARCHAR(8) NOT NULL DEFAULT 'prod',
  `tool` VARCHAR(64) NOT NULL,
  `args_digest` VARCHAR(512) NOT NULL DEFAULT '',
  `ok` SMALLINT NOT NULL DEFAULT 1,
  `rows_affected` INT DEFAULT NULL,
  `ms` INT NOT NULL DEFAULT 0,
  `need_confirm` SMALLINT NOT NULL DEFAULT 0,
  `confirmed_by` BIGINT DEFAULT NULL,
  `created_at` DATETIME(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
  PRIMARY KEY (`id`),
  KEY `ix_agent_tool_call_trace_id` (`trace_id`),
  KEY `ix_agent_tool_call_user_id` (`user_id`),
  KEY `ix_agent_tool_call_tool` (`tool`),
  KEY `ix_agent_tool_call_created_at` (`created_at`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='工具调用审计（参数摘要，不落返回正文）';

CREATE TABLE IF NOT EXISTS `agent_tool_config` (
  `name` VARCHAR(64) NOT NULL,
  `enabled` SMALLINT NOT NULL DEFAULT 1,
  `timeout_ms` INT NOT NULL DEFAULT 15000,
  `roles` VARCHAR(128) NOT NULL DEFAULT 'ADMIN,USER',
  `params` JSON NOT NULL,
  `updated_by` BIGINT DEFAULT NULL,
  `updated_at` DATETIME(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3) ON UPDATE CURRENT_TIMESTAMP(3),
  PRIMARY KEY (`name`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='工具配置（管理台可改，agent 缓存 30s）';

CREATE TABLE IF NOT EXISTS `agent_kb` (
  `id` VARCHAR(40) NOT NULL,
  `name` VARCHAR(64) NOT NULL,
  `description` VARCHAR(255) NOT NULL DEFAULT '',
  `embedding_model` VARCHAR(64) NOT NULL DEFAULT '',
  `dim` INT NOT NULL DEFAULT 768,
  `created_by` BIGINT DEFAULT NULL,
  `created_at` DATETIME(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
  PRIMARY KEY (`id`),
  UNIQUE KEY `uk_agent_kb_name` (`name`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='知识库';

CREATE TABLE IF NOT EXISTS `agent_kb_doc` (
  `id` VARCHAR(40) NOT NULL,
  `kb_id` VARCHAR(40) NOT NULL,
  `title` VARCHAR(255) NOT NULL DEFAULT '',
  `source` VARCHAR(512) NOT NULL DEFAULT '',
  `format` VARCHAR(16) NOT NULL DEFAULT 'txt',
  `size_bytes` INT NOT NULL DEFAULT 0,
  `status` VARCHAR(16) NOT NULL DEFAULT 'pending',
  `chunk_count` INT NOT NULL DEFAULT 0,
  `error` VARCHAR(512) NOT NULL DEFAULT '',
  `created_by` BIGINT DEFAULT NULL,
  `created_at` DATETIME(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
  `updated_at` DATETIME(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3) ON UPDATE CURRENT_TIMESTAMP(3),
  PRIMARY KEY (`id`),
  KEY `ix_agent_kb_doc_kb_id` (`kb_id`),
  KEY `ix_agent_kb_doc_status` (`status`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='知识库文档（状态机 pending→ready）';

CREATE TABLE IF NOT EXISTS `agent_kb_chunk` (
  `id` VARCHAR(40) NOT NULL,
  `doc_id` VARCHAR(40) NOT NULL,
  `kb_id` VARCHAR(40) NOT NULL,
  `seq` INT NOT NULL DEFAULT 0,
  `text` LONGTEXT NOT NULL,
  `tokens` INT NOT NULL DEFAULT 0,
  PRIMARY KEY (`id`),
  KEY `ix_agent_kb_chunk_doc_id` (`doc_id`),
  KEY `ix_agent_kb_chunk_kb_id` (`kb_id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='知识库分块（Qdrant 里存向量，正文以此为准）';
