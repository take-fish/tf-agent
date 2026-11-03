"""历史会话：列表 / 详情 / 删除（只看得到自己的会话）。"""

from __future__ import annotations

import logging
from typing import Any

import anyio.to_thread
from fastapi import APIRouter, Depends, HTTPException, Query, status

from app.auth import Identity, require_agent_use
from app.db import db_available, session_scope
from app.models import AgentSession

logger = logging.getLogger(__name__)
router = APIRouter()


def _require_db() -> None:
    if not db_available():
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "审计库不可用，暂时看不到历史会话")


def _title(messages: list[dict[str, Any]], stored: str = "") -> str:
    if stored:
        return stored
    for message in messages or []:
        if message.get("role") == "user" and message.get("content"):
            return " ".join(str(message["content"]).split())[:60]
    return "新会话"


def _own(db, session_id: str, user_id: int) -> AgentSession:
    row = db.get(AgentSession, session_id)
    if row is None or row.user_id != user_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "会话不存在")
    return row


@router.get("/sessions")
async def list_sessions(
    identity: Identity = Depends(require_agent_use),
    limit: int = Query(20, ge=1, le=100),
):
    """当前用户最近的历史会话（按更新时间倒序）。"""
    _require_db()

    def _work() -> list[dict]:
        with session_scope() as db:
            rows = (
                db.query(AgentSession)
                .filter(AgentSession.user_id == identity.user_id)
                .order_by(AgentSession.updated_at.desc())
                .limit(limit)
                .all()
            )
            return [
                {
                    "id": row.id,
                    "title": _title(row.messages or [], row.title or ""),
                    "scene": row.scene,
                    "message_count": len(row.messages or []),
                    "updated_at": row.updated_at.isoformat() if row.updated_at else None,
                }
                for row in rows
            ]

    return {"code": 0, "data": await anyio.to_thread.run_sync(_work)}


@router.get("/sessions/{session_id}")
async def get_session(session_id: str, identity: Identity = Depends(require_agent_use)):
    """载入某个历史会话的消息，用于继续对话。"""
    _require_db()

    def _work() -> dict:
        with session_scope() as db:
            row = _own(db, session_id, identity.user_id)
            return {
                "id": row.id,
                "title": _title(row.messages or [], row.title or ""),
                "scene": row.scene,
                "messages": row.messages or [],
                "updated_at": row.updated_at.isoformat() if row.updated_at else None,
            }

    return {"code": 0, "data": await anyio.to_thread.run_sync(_work)}


@router.delete("/sessions/{session_id}")
async def delete_session(session_id: str, identity: Identity = Depends(require_agent_use)):
    _require_db()

    def _work() -> None:
        with session_scope() as db:
            row = _own(db, session_id, identity.user_id)
            db.delete(row)

    await anyio.to_thread.run_sync(_work)
    return {"code": 0, "msg": "deleted"}
