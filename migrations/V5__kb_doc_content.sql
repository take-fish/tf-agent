-- 文档正文：上传时把解析后的文本存下来，支持在线编辑（改完可重新切块入库）。
ALTER TABLE agent_kb_doc
    ADD COLUMN content LONGTEXT NULL AFTER title;
