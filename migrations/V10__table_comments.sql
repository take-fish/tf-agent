-- tf-agent 全表中文注释
-- 只修改表与字段注释，不改变类型、默认值、索引；列定义逐字取自 MySQL 8.4 的 SHOW CREATE TABLE。
-- 列注释统一用 ALGORITHM=INSTANT，若定义与生产不一致会直接报错，避免静默改结构。

ALTER TABLE `agent_session`
    MODIFY COLUMN `id` varchar(40) NOT NULL COMMENT '会话主键',
    MODIFY COLUMN `user_id` bigint NOT NULL COMMENT '所属用户 ID',
    MODIFY COLUMN `scene` varchar(32) NOT NULL DEFAULT 'ops' COMMENT '会话场景，如 ops 运维 / kb 知识库问答',
    MODIFY COLUMN `env` varchar(8) NOT NULL DEFAULT 'prod' COMMENT '环境标识，prod 生产 / test 测试',
    MODIFY COLUMN `title` varchar(120) NOT NULL DEFAULT '' COMMENT '会话标题，取首条用户消息',
    MODIFY COLUMN `messages` json NOT NULL COMMENT '会话消息 JSON 数组，只保留最近若干条',
    MODIFY COLUMN `updated_at` datetime(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3) ON UPDATE CURRENT_TIMESTAMP(3) COMMENT '最后更新时间',
    ALGORITHM=INSTANT;

ALTER TABLE `agent_tool_call`
    MODIFY COLUMN `id` bigint NOT NULL AUTO_INCREMENT COMMENT '调用记录主键，自增',
    MODIFY COLUMN `trace_id` varchar(32) NOT NULL COMMENT '链路追踪 ID',
    MODIFY COLUMN `user_id` bigint NOT NULL COMMENT '调用用户 ID',
    MODIFY COLUMN `scene` varchar(32) NOT NULL DEFAULT 'ops' COMMENT '调用场景',
    MODIFY COLUMN `env` varchar(8) NOT NULL DEFAULT 'prod' COMMENT '环境标识，prod 生产 / test 测试',
    MODIFY COLUMN `tool` varchar(64) NOT NULL COMMENT '工具名',
    MODIFY COLUMN `args_digest` varchar(512) NOT NULL DEFAULT '' COMMENT '工具参数摘要，不保存明文参数',
    MODIFY COLUMN `ok` smallint NOT NULL DEFAULT '1' COMMENT '是否成功，1 成功 / 0 失败',
    MODIFY COLUMN `rows_affected` int DEFAULT NULL COMMENT '影响行数，无此概念时为空',
    MODIFY COLUMN `ms` int NOT NULL DEFAULT '0' COMMENT '调用耗时毫秒',
    MODIFY COLUMN `need_confirm` smallint NOT NULL DEFAULT '0' COMMENT '是否需要人工确认，1 需要 / 0 不需要',
    MODIFY COLUMN `confirmed_by` bigint DEFAULT NULL COMMENT '确认人用户 ID',
    MODIFY COLUMN `created_at` datetime(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3) COMMENT '调用时间',
    ALGORITHM=INSTANT;

ALTER TABLE `agent_tool_config`
    MODIFY COLUMN `name` varchar(64) NOT NULL COMMENT '工具名，主键',
    MODIFY COLUMN `enabled` smallint NOT NULL DEFAULT '1' COMMENT '是否启用，1 启用 / 0 停用',
    MODIFY COLUMN `timeout_ms` int NOT NULL DEFAULT '15000' COMMENT '超时毫秒',
    MODIFY COLUMN `roles` varchar(128) NOT NULL DEFAULT 'ADMIN,USER' COMMENT '允许使用的角色，逗号分隔',
    MODIFY COLUMN `params` json NOT NULL COMMENT '工具参数 JSON',
    MODIFY COLUMN `updated_by` bigint DEFAULT NULL COMMENT '最后修改人用户 ID',
    MODIFY COLUMN `updated_at` datetime(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3) ON UPDATE CURRENT_TIMESTAMP(3) COMMENT '最后更新时间',
    ALGORITHM=INSTANT;

ALTER TABLE `agent_kb`
    MODIFY COLUMN `id` varchar(40) NOT NULL COMMENT '知识库主键',
    MODIFY COLUMN `name` varchar(64) NOT NULL COMMENT '知识库名称，唯一',
    MODIFY COLUMN `description` varchar(255) NOT NULL DEFAULT '' COMMENT '知识库描述',
    MODIFY COLUMN `embedding_model` varchar(64) NOT NULL DEFAULT '' COMMENT '向量模型名',
    MODIFY COLUMN `dim` int NOT NULL DEFAULT '768' COMMENT '向量维度',
    MODIFY COLUMN `created_by` bigint DEFAULT NULL COMMENT '创建人用户 ID',
    MODIFY COLUMN `created_at` datetime(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3) COMMENT '创建时间',
    ALGORITHM=INSTANT;

ALTER TABLE `agent_kb_category`
    MODIFY COLUMN `id` varchar(40) NOT NULL COMMENT '分类主键',
    MODIFY COLUMN `name` varchar(64) NOT NULL COMMENT '分类名称，唯一',
    MODIFY COLUMN `description` varchar(255) NOT NULL DEFAULT '' COMMENT '分类描述',
    MODIFY COLUMN `sort` int NOT NULL DEFAULT '0' COMMENT '排序值，越大越靠前',
    MODIFY COLUMN `created_by` bigint DEFAULT NULL COMMENT '创建人用户 ID',
    MODIFY COLUMN `created_at` datetime(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3) COMMENT '创建时间',
    MODIFY COLUMN `updated_at` datetime(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3) ON UPDATE CURRENT_TIMESTAMP(3) COMMENT '最后更新时间',
    ALGORITHM=INSTANT;

ALTER TABLE `agent_kb_doc`
    MODIFY COLUMN `id` varchar(40) NOT NULL COMMENT '文档主键',
    MODIFY COLUMN `kb_id` varchar(40) NOT NULL COMMENT '所属知识库 ID',
    MODIFY COLUMN `category_id` varchar(40) DEFAULT NULL COMMENT '所属分类 ID',
    MODIFY COLUMN `title` varchar(255) NOT NULL DEFAULT '' COMMENT '文档标题',
    MODIFY COLUMN `content` longtext COMMENT '解析后的文档正文，可在线编辑',
    MODIFY COLUMN `visible` tinyint(1) NOT NULL DEFAULT '1' COMMENT '是否对普通用户可见，1 可见 / 0 隐藏',
    MODIFY COLUMN `source` varchar(512) NOT NULL DEFAULT '' COMMENT '原始来源，文件名或 URL',
    MODIFY COLUMN `format` varchar(16) NOT NULL DEFAULT 'txt' COMMENT '文件格式，如 txt / md / pdf',
    MODIFY COLUMN `size_bytes` int NOT NULL DEFAULT '0' COMMENT '原始文件字节数',
    MODIFY COLUMN `status` varchar(16) NOT NULL DEFAULT 'pending' COMMENT '构建状态，waiting 待审核 / pending 排队 / parsing 解析中 / chunking 切块中 / embedding 向量化中 / ready 可检索 / failed 构建失败 / rejected 已驳回',
    MODIFY COLUMN `review_status` varchar(16) NOT NULL DEFAULT 'pending' COMMENT '审核状态，pending 待审核 / approved 已通过 / rejected 已驳回',
    MODIFY COLUMN `reviewed_by` bigint DEFAULT NULL COMMENT '审核人用户 ID',
    MODIFY COLUMN `reviewed_at` datetime(3) DEFAULT NULL COMMENT '审核时间',
    MODIFY COLUMN `review_note` varchar(255) NOT NULL DEFAULT '' COMMENT '审核备注',
    MODIFY COLUMN `chunk_count` int NOT NULL DEFAULT '0' COMMENT '分块数量',
    MODIFY COLUMN `error` varchar(512) NOT NULL DEFAULT '' COMMENT '构建失败原因',
    MODIFY COLUMN `created_by` bigint DEFAULT NULL COMMENT '上传人用户 ID',
    MODIFY COLUMN `created_at` datetime(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3) COMMENT '上传时间',
    MODIFY COLUMN `updated_at` datetime(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3) ON UPDATE CURRENT_TIMESTAMP(3) COMMENT '最后更新时间',
    ALGORITHM=INSTANT;

ALTER TABLE `agent_kb_chunk` COMMENT = '知识库分块（正文以此为准，向量存于 Elasticsearch）';

ALTER TABLE `agent_kb_chunk`
    MODIFY COLUMN `id` varchar(40) NOT NULL COMMENT '分块主键',
    MODIFY COLUMN `doc_id` varchar(40) NOT NULL COMMENT '所属文档 ID',
    MODIFY COLUMN `kb_id` varchar(40) NOT NULL COMMENT '所属知识库 ID',
    MODIFY COLUMN `seq` int NOT NULL DEFAULT '0' COMMENT '文档内分块序号，从 0 开始',
    MODIFY COLUMN `text` longtext NOT NULL COMMENT '分块正文，ES 中的向量对应这段文本',
    MODIFY COLUMN `tokens` int NOT NULL DEFAULT '0' COMMENT '分块 token 数',
    ALGORITHM=INSTANT;

ALTER TABLE `agent_llm_call` COMMENT = 'LLM 调用流水（每次模型轮次的 token 用量与耗时）';

ALTER TABLE `agent_llm_call`
    MODIFY COLUMN `id` bigint NOT NULL AUTO_INCREMENT COMMENT '调用记录主键，自增',
    MODIFY COLUMN `trace_id` varchar(32) NOT NULL COMMENT '链路追踪 ID',
    MODIFY COLUMN `user_id` bigint NOT NULL COMMENT '发起用户 ID',
    MODIFY COLUMN `scene` varchar(32) NOT NULL DEFAULT '' COMMENT '调用场景',
    MODIFY COLUMN `round` int NOT NULL DEFAULT '0' COMMENT '模型轮次，从 0 开始',
    MODIFY COLUMN `model` varchar(64) NOT NULL DEFAULT '' COMMENT '模型名',
    MODIFY COLUMN `prompt_tokens` int NOT NULL DEFAULT '0' COMMENT '输入 token 数',
    MODIFY COLUMN `completion_tokens` int NOT NULL DEFAULT '0' COMMENT '输出 token 数',
    MODIFY COLUMN `ms` int NOT NULL DEFAULT '0' COMMENT '调用耗时毫秒',
    MODIFY COLUMN `created_at` datetime(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3) COMMENT '调用时间',
    ALGORITHM=INSTANT;

ALTER TABLE `agent_profile` COMMENT = 'Agent 展示配置（单行 id=default）';

ALTER TABLE `agent_profile`
    MODIFY COLUMN `id` varchar(16) NOT NULL COMMENT '配置主键，固定为 default',
    MODIFY COLUMN `config` longtext NOT NULL COMMENT '展示配置 JSON，名称、形象、问候语与推荐问题',
    MODIFY COLUMN `updated_by` bigint DEFAULT NULL COMMENT '最后修改人用户 ID',
    MODIFY COLUMN `updated_at` datetime(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3) ON UPDATE CURRENT_TIMESTAMP(3) COMMENT '最后更新时间',
    ALGORITHM=INSTANT;

ALTER TABLE `agent_eval_set` COMMENT = '知识库检索评测题集';

ALTER TABLE `agent_eval_set`
    MODIFY COLUMN `id` varchar(40) NOT NULL COMMENT '题集主键',
    MODIFY COLUMN `name` varchar(80) NOT NULL COMMENT '题集名称',
    MODIFY COLUMN `description` varchar(255) NOT NULL DEFAULT '' COMMENT '题集描述',
    MODIFY COLUMN `kb_ids` json NOT NULL COMMENT '评测范围的知识库 ID 数组 JSON',
    MODIFY COLUMN `top_k` int NOT NULL DEFAULT '8' COMMENT '评测取前 k 条',
    MODIFY COLUMN `created_by` bigint DEFAULT NULL COMMENT '创建人用户 ID',
    MODIFY COLUMN `created_at` datetime(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3) COMMENT '创建时间',
    ALGORITHM=INSTANT;

ALTER TABLE `agent_eval_question` COMMENT = '评测问题';

ALTER TABLE `agent_eval_question`
    MODIFY COLUMN `id` varchar(40) NOT NULL COMMENT '问题主键',
    MODIFY COLUMN `set_id` varchar(40) NOT NULL COMMENT '所属题集 ID',
    MODIFY COLUMN `query` varchar(500) NOT NULL COMMENT '评测问题文本',
    MODIFY COLUMN `note` varchar(255) NOT NULL DEFAULT '' COMMENT '备注',
    MODIFY COLUMN `pooled_at` datetime(3) DEFAULT NULL COMMENT '候选池生成时间，为空表示未池化',
    MODIFY COLUMN `created_by` bigint DEFAULT NULL COMMENT '创建人用户 ID',
    MODIFY COLUMN `created_at` datetime(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3) COMMENT '创建时间',
    ALGORITHM=INSTANT;

ALTER TABLE `agent_eval_candidate` COMMENT = '候选池快照与人工标注';

ALTER TABLE `agent_eval_candidate`
    MODIFY COLUMN `id` bigint NOT NULL AUTO_INCREMENT COMMENT '候选主键，自增',
    MODIFY COLUMN `question_id` varchar(40) NOT NULL COMMENT '所属问题 ID',
    MODIFY COLUMN `doc_id` varchar(40) NOT NULL COMMENT '文档 ID，与 seq 一起标识候选片段',
    MODIFY COLUMN `seq` int NOT NULL DEFAULT '0' COMMENT '文档内分块序号，与 doc_id 一起标识候选片段',
    MODIFY COLUMN `title` varchar(255) NOT NULL DEFAULT '' COMMENT '候选片段所在文档标题',
    MODIFY COLUMN `text` longtext NOT NULL COMMENT '候选片段正文快照',
    MODIFY COLUMN `score` double NOT NULL DEFAULT '0' COMMENT '融合得分',
    MODIFY COLUMN `knn_rank` int DEFAULT NULL COMMENT '向量检索名次，未命中为空',
    MODIFY COLUMN `bm25_rank` int DEFAULT NULL COMMENT '关键词检索名次，未命中为空',
    MODIFY COLUMN `rrf_rank` int NOT NULL DEFAULT '0' COMMENT 'RRF 融合名次，评测按此排序取前 k',
    MODIFY COLUMN `label` smallint DEFAULT NULL COMMENT '人工标注，0 不相关 / 1 部分相关 / 2 高度相关，为空表示未标注',
    MODIFY COLUMN `label_source` varchar(8) NOT NULL DEFAULT '' COMMENT '标注来源，预留字段',
    MODIFY COLUMN `labeled_by` bigint DEFAULT NULL COMMENT '标注人用户 ID',
    MODIFY COLUMN `labeled_at` datetime(3) DEFAULT NULL COMMENT '标注时间',
    MODIFY COLUMN `created_at` datetime(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3) COMMENT '候选生成时间',
    ALGORITHM=INSTANT;

ALTER TABLE `agent_eval_run` COMMENT = '评测运行快照';

ALTER TABLE `agent_eval_run`
    MODIFY COLUMN `id` varchar(40) NOT NULL COMMENT '快照主键',
    MODIFY COLUMN `set_id` varchar(40) NOT NULL COMMENT '所属题集 ID',
    MODIFY COLUMN `top_k` int NOT NULL DEFAULT '8' COMMENT '本次评测的 k',
    MODIFY COLUMN `question_count` int NOT NULL DEFAULT '0' COMMENT '题集问题总数',
    MODIFY COLUMN `complete_count` int NOT NULL DEFAULT '0' COMMENT '标注完整的问题数',
    MODIFY COLUMN `precision` double DEFAULT NULL COMMENT '精确率，标注完整题的宏平均',
    MODIFY COLUMN `recall` double DEFAULT NULL COMMENT '召回率，分母为候选池内标注相关的片段数',
    MODIFY COLUMN `mrr` double DEFAULT NULL COMMENT '平均倒数排名',
    MODIFY COLUMN `detail` json NOT NULL COMMENT '逐题指标与配置快照 JSON',
    MODIFY COLUMN `created_by` bigint DEFAULT NULL COMMENT '生成人用户 ID',
    MODIFY COLUMN `created_at` datetime(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3) COMMENT '生成时间',
    ALGORITHM=INSTANT;
-- schema_version 由 app.migrate 在应用迁移前自动创建，单独补注释
ALTER TABLE `schema_version` COMMENT = '迁移版本记录（由 app.migrate 维护）';

ALTER TABLE `schema_version`
    MODIFY COLUMN `version` varchar(64) NOT NULL COMMENT '已应用的迁移文件名',
    MODIFY COLUMN `applied_at` datetime(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3) COMMENT '应用时间',
    ALGORITHM=INSTANT;
