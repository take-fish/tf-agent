-- 检索上下文补全、分题型评测与会话沉淀的基础字段。
-- 全部为追加列（带默认值），不改已有列和索引，可在线执行。

-- 1. 切块记录章节路径与章节序号：命中后可整节回捞，不必只拿孤立一块。
ALTER TABLE `agent_kb_chunk`
    ADD COLUMN `section` VARCHAR(255) NOT NULL DEFAULT '' COMMENT '章节路径，如 07-游戏中心 / 赛制说明' AFTER `seq`,
    ADD COLUMN `section_seq` INT NOT NULL DEFAULT 0 COMMENT '章节在文档内的序号，从 0 开始' AFTER `section`,
    ADD KEY `idx_kb_chunk_doc_section` (`doc_id`, `section_seq`);

-- 2. 评测题区分事实型 / 流程型；候选池快照保存当时的章节，便于计算章节覆盖率。
ALTER TABLE `agent_eval_question`
    ADD COLUMN `type` VARCHAR(16) NOT NULL DEFAULT 'fact' COMMENT '题型：fact 事实直答 / flow 流程与排障' AFTER `query`;

ALTER TABLE `agent_eval_candidate`
    ADD COLUMN `section` VARCHAR(255) NOT NULL DEFAULT '' COMMENT '候选片段所在章节路径' AFTER `seq`,
    ADD COLUMN `section_seq` INT NULL COMMENT '候选片段所在章节序号；老候选为空' AFTER `section`;

ALTER TABLE `agent_eval_run`
    ADD COLUMN `section_coverage` DOUBLE NULL COMMENT '章节覆盖率：top-k 覆盖到的相关章节数 / 全部相关章节数' AFTER `mrr`;

-- 3. 会话沉淀草稿的指纹：同一份内容只生成一次待审核文档。
ALTER TABLE `agent_kb_doc`
    ADD COLUMN `content_hash` VARCHAR(64) NOT NULL DEFAULT '' COMMENT '正文 SHA-256，用于会话沉淀去重' AFTER `content`,
    ADD KEY `idx_kb_doc_content_hash` (`content_hash`);
