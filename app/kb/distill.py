"""把一轮排查会话沉淀成知识库草稿。

边界：

- 只读取会话里的用户 / 助手消息，不读工具原始数据；
- 生成内容只写入 ``agent_kb_doc``，``review_status=pending``，绝不自动审核、绝不进索引；
- 写入前做基础脱敏，正文指纹用于去重；
- 模型输出只是草稿，管理员审核时可继续编辑。
"""

from __future__ import annotations

import hashlib
import logging
import re
import uuid
from typing import Any

from app import llm, nacos
from app.config import settings
from app.db import session_scope
from app.models import AgentKb, AgentKbDoc, AgentSession

logger = logging.getLogger(__name__)

_MAX_MESSAGE_CHARS = 1500
_MAX_TOTAL_CHARS = 6000
_DISTILL_KB_NAMES = ("会话沉淀", "运维沉淀", "知识沉淀")

_SECRET = re.compile(r"(?i)(token|password|passwd|secret|api[_-]?key|authorization)(\s*[:=]\s*)([^\s,;，；]+)")

_SYSTEM_PROMPT = """你是 FishMatrix 团队知识库编辑。请把一段智能助手排障会话整理成一篇可复用的 Markdown 文档。

要求：
1. 只写会话里已经确认的事实；推测必须标注“待确认”，不得编造命令输出、日志行、时间戳或服务状态。
2. 不写 token、密码、密钥、连接串等敏感信息。
3. 结构固定：# 标题、## 现象、## 结论、## 排查步骤、## 验证方式、## 未确认事项（没有就写“无”）。
4. 命令与配置用代码块；步骤用有序列表。
5. 全文 300-800 字，直接输出 Markdown，不要额外解释。"""


def enabled() -> bool:
    return bool(nacos.kb_param("distill-enabled", settings.kb_distill_enabled))


def redact(text: str) -> str:
    """把 ``token: xxx`` 这类赋值改成 ``token: ***``。"""
    return _SECRET.sub(lambda match: f"{match.group(1)}{match.group(2)}***", text or "")


def content_hash(content: str) -> str:
    normalized = "\n".join(line.rstrip() for line in (content or "").replace("\r\n", "\n").split("\n")).strip()
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def _dialog(messages: list[dict[str, Any]]) -> str:
    lines: list[str] = []
    total = 0
    for message in messages or []:
        role = str(message.get("role") or "")
        if role not in {"user", "assistant"}:
            continue
        text = str(message.get("content") or "").strip()
        if not text:
            continue
        text = redact(text)[:_MAX_MESSAGE_CHARS]
        label = "用户" if role == "user" else "助手"
        line = f"{label}：{text}"
        if total + len(line) > _MAX_TOTAL_CHARS:
            break
        lines.append(line)
        total += len(line)
    return "\n\n".join(lines)


def _title_from(markdown: str, fallback: str) -> str:
    for line in (markdown or "").splitlines():
        if line.startswith("# "):
            return line[2:].strip()[:200] or fallback
    return fallback[:200]


def _normalize(markdown: str, fallback_title: str) -> tuple[str, str]:
    text = redact(markdown or "").replace("\r\n", "\n").strip()
    title = _title_from(text, fallback_title)
    if not text.startswith("# "):
        text = f"# {title}\n\n{text}".strip()
    return title, text


def _session_row(session_id: str, user_id: int) -> dict[str, Any]:
    with session_scope() as db:
        row = db.get(AgentSession, session_id)
        if row is None or row.user_id != user_id:
            raise LookupError("会话不存在")
        return {
            "id": row.id,
            "scene": row.scene,
            "env": row.env,
            "title": row.title or "",
            "messages": list(row.messages or []),
        }


def _pick_kb_id(preferred: str) -> str:
    with session_scope() as db:
        if preferred:
            if db.get(AgentKb, preferred) is None:
                raise ValueError("知识库不存在")
            return preferred
        configured = str(nacos.kb_param("distill-kb-id", settings.kb_distill_kb_id) or "").strip()
        if configured:
            if db.get(AgentKb, configured) is None:
                raise ValueError("配置的沉淀知识库不存在")
            return configured
        row = db.query(AgentKb).filter(AgentKb.name.in_(_DISTILL_KB_NAMES)).order_by(AgentKb.created_at).first()
        if row is None:
            row = db.query(AgentKb).order_by(AgentKb.created_at).first()
        if row is None:
            raise ValueError("还没有知识库，无法沉淀")
        return str(row.id)


def _save_draft(
    *,
    kb_id: str,
    title: str,
    content: str,
    session_id: str,
    user_id: int,
) -> dict[str, Any]:
    digest = content_hash(content)
    with session_scope() as db:
        existing = (
            db.query(AgentKbDoc)
            .filter(AgentKbDoc.content_hash == digest, AgentKbDoc.review_status != "rejected")
            .order_by(AgentKbDoc.created_at)
            .first()
        )
        if existing is not None:
            return {
                "id": existing.id,
                "title": existing.title,
                "kb_id": existing.kb_id,
                "review_status": existing.review_status,
                "duplicate": True,
            }
        doc_id = uuid.uuid4().hex[:32]
        db.add(
            AgentKbDoc(
                id=doc_id,
                kb_id=kb_id,
                title=title,
                content=content,
                content_hash=digest,
                source=f"session/{session_id}",
                format="md",
                size_bytes=len(content.encode("utf-8")),
                status="waiting",
                review_status="pending",
                created_by=user_id,
            )
        )
        return {"id": doc_id, "title": title, "kb_id": kb_id, "review_status": "pending", "duplicate": False}


async def create_draft(
    session_id: str,
    *,
    user_id: int,
    kb_id: str = "",
) -> dict[str, Any]:
    """生成并保存一份待审核草稿。调用方负责权限与开关校验。"""
    try:
        session = _session_row(session_id, user_id)
    except LookupError as exc:
        raise ValueError("会话不存在") from exc

    dialog = _dialog(session["messages"])
    if not dialog:
        raise ValueError("会话里还没有可沉淀的对话")

    fallback_title = (session["title"] or "会话沉淀").strip()[:200]
    prompt = f"会话标题：{fallback_title}\n\n对话：\n{dialog}"
    try:
        output = await llm.complete(
            [
                {"role": "system", "content": _SYSTEM_PROMPT},
                {"role": "user", "content": prompt},
            ],
            temperature=0.2,
            max_tokens=1200,
        )
    except llm.LLMError as exc:
        raise RuntimeError(f"生成草稿失败：{exc}") from exc

    title, content = _normalize(output, fallback_title)
    target_kb = _pick_kb_id(kb_id)
    return _save_draft(kb_id=target_kb, title=title, content=content, session_id=session_id, user_id=user_id)
