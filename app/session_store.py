"""会话读写：MySQL agent_session，最多保留 settings.history_limit 条消息。"""

from __future__ import annotations

import logging
import uuid
from typing import Any

import anyio.to_thread

from app.config import settings
from app.models import AgentSession

logger = logging.getLogger(__name__)


def new_session_id() -> str:
    return uuid.uuid4().hex


def _load(session_id: str, user_id: int) -> dict[str, Any]:
    empty = {"session_id": session_id, "messages": [], "scene": "ops", "env": settings.default_env}
    if not session_id:
        return empty
    try:
        from app.db import db_available, session_scope

        if not db_available():
            return empty
        with session_scope() as db:
            row = db.get(AgentSession, session_id)
            if row is None or row.user_id != user_id:
                return empty
            messages = row.messages or []
            return {
                "session_id": session_id,
                "messages": messages[-settings.history_limit :],
                "scene": row.scene or "ops",
                "env": row.env or settings.default_env,
            }
    except Exception as exc:  # noqa: BLE001
        logger.warning("会话读取失败：%s", exc)
        return empty


async def load(session_id: str, user_id: int) -> dict[str, Any]:
    return await anyio.to_thread.run_sync(lambda: _load(session_id, user_id))
