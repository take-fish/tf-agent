-- V2：知识库审核 + 分类
--
-- 背景：知识库要向普通用户开放"上传"，但只有管理员审核通过后才能进索引、
-- 才能被 agent 检索到。所以把"索引状态"（status）和"审核状态"（review_status）
-- 拆成两个维度：
--   review_status: pending（待审核）/ approved（已通过）/ rejected（已驳回）
--   status:        waiting（等待审核，不建索引）/ pending|parsing|chunking|embedding（构建中）
--                  / ready（可检索）/ failed（构建失败）/ rejected（已驳回）
-- 存量文档在旧版本里只要 ready 就已经在索引里，迁移时补成 approved。

CREATE TABLE IF NOT EXISTS `agent_kb_category` (
  `id` VARCHAR(40) NOT NULL,
  `name` VARCHAR(64) NOT NULL,
  `description` VARCHAR(255) NOT NULL DEFAULT '',
  `sort` INT NOT NULL DEFAULT 0,
  `created_by` BIGINT NULL,
  `created_at` DATETIME(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
  `updated_at` DATETIME(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3) ON UPDATE CURRENT_TIMESTAMP(3),
  PRIMARY KEY (`id`),
  UNIQUE KEY `uk_agent_kb_category_name` (`name`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='知识库分类';

ALTER TABLE `agent_kb_doc`
  ADD COLUMN `category_id` VARCHAR(40) NULL AFTER `kb_id`,
  ADD COLUMN `review_status` VARCHAR(16) NOT NULL DEFAULT 'pending' AFTER `status`,
  ADD COLUMN `reviewed_by` BIGINT NULL AFTER `review_status`,
  ADD COLUMN `reviewed_at` DATETIME(3) NULL AFTER `reviewed_by`,
  ADD COLUMN `review_note` VARCHAR(255) NOT NULL DEFAULT '' AFTER `reviewed_at`;

ALTER TABLE `agent_kb_doc`
  ADD KEY `ix_agent_kb_doc_review_status` (`review_status`),
  ADD KEY `ix_agent_kb_doc_category` (`category_id`);

UPDATE `agent_kb_doc` SET `review_status` = 'approved' WHERE `status` = 'ready';
