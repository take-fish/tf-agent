-- 可见性粒度改为「文档」级：每个知识库文件可单独对普通用户隐藏（默认可见）。
-- 之前的知识库级开关移除（还没被真正使用）。
ALTER TABLE agent_kb_doc
    ADD COLUMN visible TINYINT(1) NOT NULL DEFAULT 1 AFTER content;

ALTER TABLE agent_kb
    DROP COLUMN visible;
