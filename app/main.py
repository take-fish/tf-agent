"""tf-agent 服务入口。

注意：必须是**单进程**（uvicorn 一个 worker）。并发闸门是进程内信号量，
多 worker 会让它失效，而 GPU 只有 1-2 路并发能力。
"""

from __future__ import annotations

import asyncio
import logging
import os
from contextlib import asynccontextmanager

import anyio.to_thread
from fastapi import FastAPI

from app import errors, llm, nacos
from app.api import admin, chat, confirm, kb, meta, profile
from app.config import settings
from app.db import init_db
from app.kb import embedder
from app.tools import register_all

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(_: FastAPI):
    register_all()
    init_db()
    # 嵌入模型预热放后台，不阻塞端口监听
    asyncio.create_task(anyio.to_thread.run_sync(embedder.warmup))
    # Nacos：拉配置 + 注册实例；连不上就降级（用默认参数、网关走静态兜底）
    nacos_runtime = nacos.manager()
    try:
        await nacos_runtime.start()
    except Exception as exc:  # noqa: BLE001 - 配置中心不可用不应阻止服务启动
        logger.warning("Nacos 初始化失败，使用默认参数继续启动：%s", exc)
    logger.info("tf-agent 启动完成（dev_mode=%s, max_concurrency=%s）", settings.dev_mode, settings.max_concurrency)
    yield
    try:
        await nacos_runtime.stop()
    except Exception as exc:  # noqa: BLE001
        logger.warning("Nacos 注销异常：%s", exc)
    await llm.close()


app = FastAPI(
    title="tf-agent",
    version=settings.app_version,
    docs_url="/api/v1/agent/docs",
    openapi_url="/api/v1/agent/openapi.json",
    lifespan=lifespan,
)

app.include_router(chat.router, prefix="/api/v1/agent", tags=["agent"])
app.include_router(confirm.router, prefix="/api/v1/agent", tags=["agent"])
app.include_router(kb.router, prefix="/api/v1/agent/kb", tags=["knowledge-base"])
app.include_router(admin.router, prefix="/api/v1/agent/admin", tags=["admin"])
app.include_router(profile.router, prefix="/api/v1/agent", tags=["profile"])
app.include_router(meta.router, prefix="/api/v1/agent", tags=["meta"])

# 统一错误响应（标准信封 + 错误码 + traceId）与请求链路日志
errors.install(app)


@app.get("/healthz")
def healthz() -> dict:
    return {"status": "ok", "service": "tf-agent", "version": settings.app_version}


@app.get("/api/v1/agent/ping")
def ping() -> dict:
    return {"code": 0, "msg": "success", "data": {"service": "tf-agent", "version": settings.app_version}}
