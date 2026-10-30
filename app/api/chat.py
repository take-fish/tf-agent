"""对话接口：POST /api/v1/agent/chat（SSE 流式）。"""

from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from collections import defaultdict
from collections.abc import AsyncIterator
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from app.agent import loop
from app.auth import Identity, get_identity
from app.config import settings
from app.session_store import load as load_session
from app.session_store import new_session_id

logger = logging.getLogger(__name__)
router = APIRouter()

_sem = asyncio.Semaphore(settings.max_concurrency)
_waiting = 0
_rate_bucket: dict[int, list[float]] = defaultdict(list)


class ChatRequest(BaseModel):
    message: str = Field(min_length=1, max_length=20000)
    session_id: str = ""
    scene: str = "ops"
    env: str = Field(default="prod", pattern="^(prod|test)$")


def sse(payload: dict[str, Any]) -> str:
    return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"


def _check_rate(user_id: int) -> None:
    now = time.time()
    bucket = [t for t in _rate_bucket[user_id] if now - t < 60]
    if len(bucket) >= settings.rate_limit_per_minute:
        raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, "请求过于频繁，请稍后再试")
    bucket.append(now)
    _rate_bucket[user_id] = bucket


async def _acquire() -> None:
    global _waiting
    if _sem.locked() and _waiting >= settings.queue_limit:
        raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, "模型繁忙，请稍后重试")
    _waiting += 1
    try:
        await _sem.acquire()
    finally:
        _waiting -= 1


@router.post("/chat")
async def chat(req: ChatRequest, request: Request, identity: Identity = Depends(get_identity)):
    _check_rate(identity.user_id)
    await _acquire()

    session_id = req.session_id or new_session_id()
    trace_id = (request.headers.get("x-request-id") or uuid.uuid4().hex)[:16]

    async def generate() -> AsyncIterator[str]:
        try:
            session = await load_session(session_id, identity.user_id)
            history = session.get("messages") or []
            async for event in loop.stream_agent(
                identity=identity,
                scene_name=req.scene,
                env=req.env,
                user_text=req.message,
                history=history,
                session_id=session_id,
                trace_id=trace_id,
            ):
                yield sse(event)
        except Exception as exc:  # noqa: BLE001 - 兜底：任何异常都要给前端一个可读事件
            logger.exception("对话处理失败")
            yield sse({"type": "error", "message": f"服务内部错误：{exc}"})
        finally:
            _sem.release()

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={
            "X-Accel-Buffering": "no",
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Session-Id": session_id,
            "X-Trace-Id": trace_id,
        },
    )
