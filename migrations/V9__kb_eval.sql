-- 知识库检索评测：题集、问题、候选池快照、人工标注与评测运行快照。
-- 候选身份用 doc_id + seq，正文同时落一份快照，避免文档重建后历史标注失真。
CREATE TABLE IF NOT EXISTS agent_eval_set (
    id VARCHAR(40) NOT NULL,
    name VARCHAR(80) NOT NULL,
    description VARCHAR(255) NOT NULL DEFAULT '',
    kb_ids JSON NOT NULL,
    top_k INT NOT NULL DEFAULT 8,
    created_by BIGINT NULL,
    created_at DATETIME(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
    PRIMARY KEY (id)
) ENGINE = InnoDB DEFAULT CHARSET = utf8mb4 COLLATE = utf8mb4_0900_ai_ci;

CREATE TABLE IF NOT EXISTS agent_eval_question (
    id VARCHAR(40) NOT NULL,
    set_id VARCHAR(40) NOT NULL,
    query VARCHAR(500) NOT NULL,
    note VARCHAR(255) NOT NULL DEFAULT '',
    pooled_at DATETIME(3) NULL,
    created_by BIGINT NULL,
    created_at DATETIME(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
    PRIMARY KEY (id),
    KEY idx_eval_question_set (set_id)
) ENGINE = InnoDB DEFAULT CHARSET = utf8mb4 COLLATE = utf8mb4_0900_ai_ci;

CREATE TABLE IF NOT EXISTS agent_eval_candidate (
    id BIGINT NOT NULL AUTO_INCREMENT,
    question_id VARCHAR(40) NOT NULL,
    doc_id VARCHAR(40) NOT NULL,
    seq INT NOT NULL DEFAULT 0,
    title VARCHAR(255) NOT NULL DEFAULT '',
    text LONGTEXT NOT NULL,
    score DOUBLE NOT NULL DEFAULT 0,
    knn_rank INT NULL,
    bm25_rank INT NULL,
    rrf_rank INT NOT NULL DEFAULT 0,
    label SMALLINT NULL,
    label_source VARCHAR(8) NOT NULL DEFAULT '',
    labeled_by BIGINT NULL,
    labeled_at DATETIME(3) NULL,
    created_at DATETIME(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
    PRIMARY KEY (id),
    UNIQUE KEY uq_eval_candidate_chunk (question_id, doc_id, seq),
    KEY idx_eval_candidate_question_rank (question_id, rrf_rank)
) ENGINE = InnoDB DEFAULT CHARSET = utf8mb4 COLLATE = utf8mb4_0900_ai_ci;

CREATE TABLE IF NOT EXISTS agent_eval_run (
    id VARCHAR(40) NOT NULL,
    set_id VARCHAR(40) NOT NULL,
    top_k INT NOT NULL DEFAULT 8,
    question_count INT NOT NULL DEFAULT 0,
    complete_count INT NOT NULL DEFAULT 0,
    -- precision 是 MySQL 8 保留字，手写 DDL 必须加反引号
    `precision` DOUBLE NULL,
    recall DOUBLE NULL,
    mrr DOUBLE NULL,
    detail JSON NOT NULL,
    created_by BIGINT NULL,
    created_at DATETIME(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
    PRIMARY KEY (id),
    KEY idx_eval_run_set (set_id)
) ENGINE = InnoDB DEFAULT CHARSET = utf8mb4 COLLATE = utf8mb4_0900_ai_ci;
