-- 知识库可见性：管理员可把某个知识库对普通用户隐藏（默认可见）。
-- 隐藏只影响列表/详情/检索入口，不影响已入库向量；管理员始终可见。
ALTER TABLE agent_kb
    ADD COLUMN visible TINYINT(1) NOT NULL DEFAULT 1 AFTER dim;
