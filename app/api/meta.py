"""元信息与可观测性：/tools /scenes /status /metrics。"""

from __future__ import annotations

import logging

import anyio.to_thread
from fastapi import APIRouter, Depends, Response
from prometheus_client import CONTENT_TYPE_LATEST, Counter, generate_latest

from app import llm, nacos
from app.agent import prompt
from app.auth import Identity, get_identity
from app.config import settings
from app.db import db_available
from app.kb import store
from app.tools import registry

logger = logging.getLogger(__name__)
router = APIRouter()

CHAT_REQUESTS = Counter("fm_agent_chat_requests_total", "对话请求数")
TOOL_CALLS = Counter("fm_agent_tool_calls_total", "工具调用数", ["tool", "ok"])


@router.get("/tools")
async def my_tools(identity: Identity = Depends(get_identity)):
    specs = registry.allowed_tools(identity, None)
    return {
        "code": 0,
        "data": [{"name": s.name, "description": s.description, "read_only": s.read_only} for s in specs],
    }


@router.get("/scenes")
async def scenes():
    return {
        "code": 0,
        "data": [{"name": s.name, "title": s.title, "tools": s.tools} for s in prompt.scenes().values()],
        "source": nacos.status().config_source,
    }


@router.get("/status")
async def status():
    db_ok = db_available()
    kb_ok = await anyio.to_thread.run_sync(store.health)
    kb_info = store.info()
    llm_ok = True
    llm_error = ""
    try:
        await llm.list_models()
    except Exception as exc:  # noqa: BLE001
        llm_ok = False
        llm_error = str(exc)
    return {
        "code": 0,
        "data": {
            "llm": {"ok": llm_ok, "base_url": settings.llm_base_url, "model": settings.llm_model, "error": llm_error},
            "db": {"ok": db_ok},
            # 兼容旧前端字段：qdrant 只是默认后端名，真实后端看 vector.backend
            "qdrant": {"ok": kb_ok, "url": kb_info.get("endpoint", "")},
            "vector": {**kb_info, "ok": kb_ok, "hybrid": settings.kb_hybrid},
            "tools": len(registry.all_specs()),
            "embedding": {"model": settings.embed_model, "dim": settings.embed_dim},
            "nacos": {
                "enabled": settings.nacos_enabled,
                "config_ok": nacos.status().config_ok,
                "config_source": nacos.status().config_source,
                "registered": nacos.status().registered,
                "service": settings.nacos_service_name,
                "instance": f"{nacos.manager().ip}:{settings.nacos_port}",
                "error": nacos.status().last_error,
            },
        },
    }


@router.get("/metrics")
async def metrics():
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)
