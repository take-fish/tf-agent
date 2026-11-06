"""对话接口：POST /api/v1/agent/chat（SSE 流式）。

两层闸门：入口限制"同时几个用户回合"（Nacos 可调），模型侧并发由 vLLM 的
--max-num-seqs 决定；这里只做排队，不打爆 GPU。
"""

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
from pydantic import BaseModel, Field, model_validator

from app import nacos, ocr
from app.agent import loop
from app.auth import Identity, require_agent_use
from app.config import settings
from app.metrics import CHAT_REQUESTS
from app.session_store import load as load_session
from app.session_store import new_session_id

logger = logging.getLogger(__name__)
router = APIRouter()

_rate_bucket: dict[int, list[float]] = defaultdict(list)


MAX_CHAT_IMAGES = 6
# 与 tf-web 的 Nginx 10MB 请求体上限留出余量；前端会把每张图压到约 800KB。
MAX_TOTAL_IMAGE_BASE64 = 8_000_000


class ChatImage(BaseModel):
    """单张图片：助手页可直接发多张，聊天端仍只发最近一张。"""

    mime: str = Field(pattern=r"^image/(png|jpeg|webp|gif)$")
    # 5MB 原图的标准 Base64 约 6.7MB；给一点余量，超过由 OCR 层再按字节校验。
    data: str = Field(min_length=16, max_length=8_000_000)


class ChatRequest(BaseModel):
    message: str = Field(min_length=1, max_length=20000)
    session_id: str = ""
    scene: str = ""  # 空 = auto：由用户在对话里描述，模型自己选工具
    env: str = Field(default="prod", pattern="^(prod|test)$")
    # image 是 tf-chat 的旧单图字段；images 是助手页的多图字段，两者合并后一起识别。
    image: ChatImage | None = None
    images: list[ChatImage] = Field(default_factory=list, max_length=MAX_CHAT_IMAGES)

    @model_validator(mode="after")
    def _merge_images(self) -> ChatRequest:
        merged = ([self.image] if self.image is not None else []) + list(self.images)
        if len(merged) > MAX_CHAT_IMAGES:
            raise ValueError(f"最多支持 {MAX_CHAT_IMAGES} 张图片")
        if sum(len(item.data) for item in merged) > MAX_TOTAL_IMAGE_BASE64:
            raise ValueError("图片总量过大，请减少数量或压缩后重试")
        self.images = merged
        return self


def sse(payload: dict[str, Any]) -> str:
    return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"


def _check_rate(user_id: int) -> None:
    limit = max(1, nacos.concurrency("rate-limit-per-minute", settings.rate_limit_per_minute))
    now = time.time()
    bucket = [t for t in _rate_bucket[user_id] if now - t < 60]
    if len(bucket) >= limit:
        raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, "请求过于频繁，请稍后再试")
    bucket.append(now)
    _rate_bucket[user_id] = bucket


class _ConcurrencyGate:
    """动态并发闸门：上限每次从 Nacos 读，扩缩容不用重启。

    release 是同步的——async generator 的 finally 里（包括客户端断开触发的
    GeneratorExit）不能 await，否则会报 "async generator ignored GeneratorExit"。
    """

    def __init__(self) -> None:
        self._active = 0
        self._waiters: list[asyncio.Future] = []

    def _limit(self) -> int:
        return max(1, nacos.concurrency("max-concurrency", settings.max_concurrency))

    async def acquire(self) -> None:
        queued = False
        while True:
            if self._active < self._limit():
                self._active += 1
                return
            if not queued and len(self._waiters) >= max(0, nacos.concurrency("queue-limit", settings.queue_limit)):
                raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, "模型繁忙，请稍后重试")
            future = asyncio.get_running_loop().create_future()
            self._waiters.append(future)
            queued = True
            try:
                await future
            finally:
                if future in self._waiters:
                    self._waiters.remove(future)

    def release(self) -> None:
        self._active = max(0, self._active - 1)
        for future in self._waiters:
            if not future.done():
                future.set_result(None)
                break


_gate = _ConcurrencyGate()


@router.post("/chat")
async def chat(req: ChatRequest, request: Request, identity: Identity = Depends(require_agent_use)):
    _check_rate(identity.user_id)
    await _gate.acquire()
    CHAT_REQUESTS.inc()

    session_id = req.session_id or new_session_id()
    trace_id = (request.headers.get("x-request-id") or uuid.uuid4().hex)[:16]
    images = [ocr.ImagePayload(mime=item.mime, data=item.data) for item in req.images]

    async def generate() -> AsyncIterator[str]:
        queue: asyncio.Queue[tuple[str, Any]] = asyncio.Queue()
        task: asyncio.Task | None = None
        try:
            # 会话在请求上下文里加载（DB 走线程池），再交给后台任务跑 loop
            session = await load_session(session_id, identity.user_id)
            history = session.get("messages") or []

            async def pump() -> None:
                try:
                    async for event in loop.stream_agent(
                        identity=identity,
                        scene_name=req.scene,
                        env=req.env,
                        user_text=req.message,
                        history=history,
                        session_id=session_id,
                        trace_id=trace_id,
                        images=images,
                    ):
                        await queue.put(("event", event))
                    await queue.put(("end", None))
                except asyncio.CancelledError:
                    raise
                except Exception as exc:  # noqa: BLE001
                    await queue.put(("error", exc))

            task = asyncio.create_task(pump())
            task.add_done_callback(lambda t: t.exception() if not t.cancelled() else None)
            while True:
                try:
                    async with asyncio.timeout(max(1.0, settings.sse_heartbeat_seconds)):
                        kind, payload = await queue.get()
                except TimeoutError:
                    # SSE 注释行：客户端会忽略，只用来保活
                    yield ": ping\n\n"
                    continue
                if kind == "event":
                    yield sse(payload)
                elif kind == "end":
                    break
                else:
                    logger.exception("对话处理失败", exc_info=payload)
                    yield sse({"type": "error", "message": f"服务内部错误：{payload}"})
                    break
        finally:
            if task is not None:
                task.cancel()
            _gate.release()

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
