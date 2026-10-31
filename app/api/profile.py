"""Agent 展示配置接口：/api/v1/agent/profile。

- GET：任何登录用户都能读（前端渲染名称、3D 形象、问候语、推荐问题）
- PUT：仅管理员，校验后写入 MySQL `agent_profile`
"""

from __future__ import annotations

import logging
from typing import Any

import anyio.to_thread
from fastapi import APIRouter, Depends, HTTPException, status

from app.agent import profile as profile_store
from app.auth import Identity, require_admin, require_agent_use
from app.db import db_available

logger = logging.getLogger(__name__)
router = APIRouter()


@router.get("/profile")
async def read_profile(_: Identity = Depends(require_agent_use)):
    return {"code": 0, "data": await anyio.to_thread.run_sync(profile_store.load)}


@router.put("/profile")
async def update_profile(payload: dict[str, Any], identity: Identity = Depends(require_admin)):
    if not db_available():
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "配置库不可用，暂不能保存")
    try:
        data = await anyio.to_thread.run_sync(lambda: profile_store.save(payload, identity.user_id))
    except profile_store.ProfileError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc
    return {"code": 0, "data": data}
