"""写操作确认：POST /api/v1/agent/confirm。"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from app.agent import confirm_store, loop
from app.api.chat import sse
from app.auth import PERM_DEPLOY_TRIGGER, Identity, get_identity

logger = logging.getLogger(__name__)
router = APIRouter()


class ConfirmRequest(BaseModel):
    confirm_id: str


@router.post("/confirm")
async def confirm(req: ConfirmRequest, identity: Identity = Depends(get_identity)):
    if not identity.has_permission(PERM_DEPLOY_TRIGGER):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "没有触发发布/重启的权限")
    try:
        action = confirm_store.consume(req.confirm_id, identity.user_id)
    except KeyError:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "确认请求不存在或已使用") from None
    except TimeoutError:
        raise HTTPException(status.HTTP_410_GONE, "确认已过期，请重新发起") from None
    except PermissionError as exc:
        raise HTTPException(status.HTTP_403_FORBIDDEN, str(exc)) from None

    async def generate() -> AsyncIterator[str]:
        async for event in loop.execute_confirmed(action, identity):
            yield sse(event)

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={"X-Accel-Buffering": "no", "Cache-Control": "no-cache"},
    )
