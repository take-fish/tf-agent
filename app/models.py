"""审计库表结构（fm_agent）。"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import BigInteger, DateTime, Float, Integer, SmallInteger, String, UniqueConstraint, func
from sqlalchemy.dialects.mysql import LONGTEXT
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from sqlalchemy.types import JSON


class Base(DeclarativeBase):
    pass


class AgentSession(Base):
    """会话：只保留最近 N 条消息（默认 10），写入时裁剪。"""

    __tablename__ = "agent_session"

    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    user_id: Mapped[int] = mapped_column(BigInteger, nullable=False, index=True)
    scene: Mapped[str] = mapped_column(String(32), nullable=False, default="ops")
    env: Mapped[str] = mapped_column(String(8), nullable=False, default="prod")
    # 列表展示用：取会话里第一条用户消息
    title: Mapped[str] = mapped_column(String(120), nullable=False, default="")
    messages: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(3), nullable=False, server_default=func.now(), onupdate=func.now()
    )


class AgentToolCall(Base):
    """工具调用审计：参数摘要 + 行数 + 耗时，不落返回正文。"""

    __tablename__ = "agent_tool_call"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    trace_id: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    user_id: Mapped[int] = mapped_column(BigInteger, nullable=False, index=True)
    scene: Mapped[str] = mapped_column(String(32), nullable=False, default="ops")
    env: Mapped[str] = mapped_column(String(8), nullable=False, default="prod")
    tool: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    args_digest: Mapped[str] = mapped_column(String(512), nullable=False, default="")
    ok: Mapped[int] = mapped_column(SmallInteger, nullable=False, default=1)
    rows_affected: Mapped[int | None] = mapped_column(Integer)
    ms: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    need_confirm: Mapped[int] = mapped_column(SmallInteger, nullable=False, default=0)
    confirmed_by: Mapped[int | None] = mapped_column(BigInteger)
    created_at: Mapped[datetime] = mapped_column(DateTime(3), nullable=False, server_default=func.now())


class AgentLlmCall(Base):
    """LLM 轮次流水：token 用量 + 耗时，用来回答"这次回答花了多少"。"""

    __tablename__ = "agent_llm_call"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    trace_id: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    user_id: Mapped[int] = mapped_column(BigInteger, nullable=False, index=True)
    scene: Mapped[str] = mapped_column(String(32), nullable=False, default="")
    round: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    model: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    prompt_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    completion_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    ms: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(3), nullable=False, server_default=func.now())


class AgentToolConfig(Base):
    """工具配置：管理台可改，agent 缓存 30s 读取。"""

    __tablename__ = "agent_tool_config"

    name: Mapped[str] = mapped_column(String(64), primary_key=True)
    enabled: Mapped[int] = mapped_column(SmallInteger, nullable=False, default=1)
    timeout_ms: Mapped[int] = mapped_column(Integer, nullable=False, default=15000)
    roles: Mapped[str] = mapped_column(String(128), nullable=False, default="ADMIN,USER")
    params: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    updated_by: Mapped[int | None] = mapped_column(BigInteger)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(3), nullable=False, server_default=func.now(), onupdate=func.now()
    )


class AgentKb(Base):
    __tablename__ = "agent_kb"

    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    name: Mapped[str] = mapped_column(String(64), nullable=False)
    description: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    embedding_model: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    dim: Mapped[int] = mapped_column(Integer, nullable=False, default=768)
    created_by: Mapped[int | None] = mapped_column(BigInteger)
    created_at: Mapped[datetime] = mapped_column(DateTime(3), nullable=False, server_default=func.now())


class AgentKbDoc(Base):
    __tablename__ = "agent_kb_doc"

    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    kb_id: Mapped[str] = mapped_column(String(40), nullable=False, index=True)
    category_id: Mapped[str | None] = mapped_column(String(40), index=True)
    title: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    # 解析后的正文：支持在线编辑（改完重新切块），为空时回落解析原始文件
    content: Mapped[str | None] = mapped_column(LONGTEXT, nullable=True)
    # 正文指纹：会话沉淀按内容去重，避免同一轮复盘反复入库
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False, default="", index=True)
    # 对普通用户是否在列表 / 详情里展示（默认展示）。
    # 注意：隐藏只是"不展示"，文档仍然在向量库里、照常参与 search_kb 检索——
    # 它表达的是展示偏好，不是检索权限，不要拿它当访问控制用。
    visible: Mapped[int] = mapped_column(SmallInteger, nullable=False, default=1)
    source: Mapped[str] = mapped_column(String(512), nullable=False, default="")
    format: Mapped[str] = mapped_column(String(16), nullable=False, default="txt")
    size_bytes: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="pending", index=True)
    # 审核与索引是两个维度：内部员工上传后等管理员审核，通过才进索引
    review_status: Mapped[str] = mapped_column(String(16), nullable=False, default="pending", index=True)
    reviewed_by: Mapped[int | None] = mapped_column(BigInteger)
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(3))
    review_note: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    chunk_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    error: Mapped[str] = mapped_column(String(512), nullable=False, default="")
    created_by: Mapped[int | None] = mapped_column(BigInteger)
    created_at: Mapped[datetime] = mapped_column(DateTime(3), nullable=False, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(3), nullable=False, server_default=func.now(), onupdate=func.now()
    )


class AgentKbCategory(Base):
    __tablename__ = "agent_kb_category"

    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    name: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    description: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    sort: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_by: Mapped[int | None] = mapped_column(BigInteger)
    created_at: Mapped[datetime] = mapped_column(DateTime(3), nullable=False, server_default=func.now())


class AgentKbChunk(Base):
    __tablename__ = "agent_kb_chunk"

    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    doc_id: Mapped[str] = mapped_column(String(40), nullable=False, index=True)
    kb_id: Mapped[str] = mapped_column(String(40), nullable=False, index=True)
    seq: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    section: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    section_seq: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    text: Mapped[str] = mapped_column(LONGTEXT, nullable=False)
    tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)


class AgentProfile(Base):
    """Agent 展示配置（单行 id='default'）：名称 / 3D 形象 / 问候语 / 推荐问题。"""

    __tablename__ = "agent_profile"

    id: Mapped[str] = mapped_column(String(16), primary_key=True)
    config: Mapped[str] = mapped_column(LONGTEXT, nullable=False)
    updated_by: Mapped[int | None] = mapped_column(BigInteger)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(3), nullable=False, server_default=func.now(), onupdate=func.now()
    )


class AgentEvalSet(Base):
    """检索评测题集：限定知识库范围与 top-k 口径。"""

    __tablename__ = "agent_eval_set"

    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    name: Mapped[str] = mapped_column(String(80), nullable=False)
    description: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    kb_ids: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    top_k: Mapped[int] = mapped_column(Integer, nullable=False, default=8)
    created_by: Mapped[int | None] = mapped_column(BigInteger)
    created_at: Mapped[datetime] = mapped_column(DateTime(3), nullable=False, server_default=func.now())


class AgentEvalQuestion(Base):
    __tablename__ = "agent_eval_question"

    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    set_id: Mapped[str] = mapped_column(String(40), nullable=False, index=True)
    query: Mapped[str] = mapped_column(String(500), nullable=False)
    # fact 事实直答 / flow 流程与排障；老题默认 fact
    type: Mapped[str] = mapped_column(String(16), nullable=False, default="fact")
    note: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    pooled_at: Mapped[datetime | None] = mapped_column(DateTime(3))
    created_by: Mapped[int | None] = mapped_column(BigInteger)
    created_at: Mapped[datetime] = mapped_column(DateTime(3), nullable=False, server_default=func.now())


class AgentEvalCandidate(Base):
    """候选池快照：doc_id + seq 唯一，保存当时的正文，避免重建索引后标注失真。"""

    __tablename__ = "agent_eval_candidate"
    __table_args__ = (UniqueConstraint("question_id", "doc_id", "seq", name="uq_eval_candidate_chunk"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    question_id: Mapped[str] = mapped_column(String(40), nullable=False, index=True)
    doc_id: Mapped[str] = mapped_column(String(40), nullable=False)
    seq: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    section: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    section_seq: Mapped[int | None] = mapped_column(Integer)
    title: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    text: Mapped[str] = mapped_column(LONGTEXT, nullable=False)
    score: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    knn_rank: Mapped[int | None] = mapped_column(Integer)
    bm25_rank: Mapped[int | None] = mapped_column(Integer)
    rrf_rank: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    # 0 不相关 / 1 部分相关 / 2 高度相关；NULL 表示还没标
    label: Mapped[int | None] = mapped_column(SmallInteger)
    label_source: Mapped[str] = mapped_column(String(8), nullable=False, default="")
    labeled_by: Mapped[int | None] = mapped_column(BigInteger)
    labeled_at: Mapped[datetime | None] = mapped_column(DateTime(3))
    created_at: Mapped[datetime] = mapped_column(DateTime(3), nullable=False, server_default=func.now())


class AgentEvalRun(Base):
    """一次评测快照：汇总指标 + 逐题明细，用于历史对比。"""

    __tablename__ = "agent_eval_run"

    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    set_id: Mapped[str] = mapped_column(String(40), nullable=False, index=True)
    top_k: Mapped[int] = mapped_column(Integer, nullable=False, default=8)
    question_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    complete_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    precision: Mapped[float | None] = mapped_column(Float)
    recall: Mapped[float | None] = mapped_column(Float)
    mrr: Mapped[float | None] = mapped_column(Float)
    section_coverage: Mapped[float | None] = mapped_column(Float)
    detail: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    created_by: Mapped[int | None] = mapped_column(BigInteger)
    created_at: Mapped[datetime] = mapped_column(DateTime(3), nullable=False, server_default=func.now())
