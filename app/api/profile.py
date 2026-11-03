"""Agent 展示配置接口：/api/v1/agent/profile。

- GET：任何登录用户都能读（前端渲染名称、3D 形象、问候语、推荐问题）
- PUT：仅管理员，校验后写入 MySQL `agent_profile`
- GET /chat-identity：聊天服务专用，只校验网关签名，用于取「@名字」触发词
"""

from __future__ import annotations

import logging
from typing import Any

import anyio.to_thread
from fastapi import APIRouter, Depends, HTTPException, status

from app.agent import profile as profile_store
from app.auth import Identity, get_identity, require_agent_use, require_profile_manage
from app.db import db_available

logger = logging.getLogger(__name__)
router = APIRouter()


@router.get("/profile")
async def read_profile(_: Identity = Depends(require_agent_use)):
    return {"code": 0, "data": await anyio.to_thread.run_sync(profile_store.load)}


@router.get("/chat-identity")
async def chat_identity(_: Identity = Depends(get_identity)):
    """给聊天服务的触发词来源：管理员在「Agent 试衣间」改的名字。

    只校验网关签名、不要求 agent.chat.use：名字是展示信息，聊天服务必须在
    任何人 @ 之前就知道它，不能等某个有权限的用户先来读一次。
    """
    data = await anyio.to_thread.run_sync(profile_store.load)
    name = str(data.get("name") or "").strip()
    return {"code": 0, "data": {"name": name, "mention": f"@{name}" if name else ""}}


@router.put("/profile")
async def update_profile(payload: dict[str, Any], identity: Identity = Depends(require_profile_manage)):
    if not db_available():
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "配置库不可用，暂不能保存")
    try:
        data = await anyio.to_thread.run_sync(lambda: profile_store.save(payload, identity.user_id))
    except profile_store.ProfileError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc
    return {"code": 0, "data": data}
