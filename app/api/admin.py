"""管理台接口：/api/v1/agent/admin/*（仅 ADMIN）。"""

from __future__ import annotations

import logging

import anyio.to_thread
from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel
from sqlalchemy import func

from app.auth import Identity, require_admin
from app.db import db_available, session_scope
from app.models import AgentSession, AgentToolCall, AgentToolConfig
from app.tools import registry

logger = logging.getLogger(__name__)
router = APIRouter()


def _require_db() -> None:
    if not db_available():
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "审计库不可用")


class ToolConfigUpdate(BaseModel):
    enabled: bool | None = None
    timeout_ms: int | None = None
    roles: list[str] | None = None


@router.get("/tool-calls")
async def list_tool_calls(
    _: Identity = Depends(require_admin),
    limit: int = Query(50, ge=1, le=500),
    tool: str = "",
    user_id: int | None = None,
    trace_id: str = "",
    only_failed: bool = False,
):
    _require_db()

    def _query() -> dict:
        with session_scope() as db:
            q = db.query(AgentToolCall)
            if tool:
                q = q.filter(AgentToolCall.tool == tool)
            if user_id:
                q = q.filter(AgentToolCall.user_id == user_id)
            if trace_id:
                q = q.filter(AgentToolCall.trace_id == trace_id)
            if only_failed:
                q = q.filter(AgentToolCall.ok == 0)
            rows = q.order_by(AgentToolCall.id.desc()).limit(limit).all()
            total = db.query(func.count(AgentToolCall.id)).scalar() or 0
            return {
                "total": int(total),
                "items": [
                    {
                        "id": r.id,
                        "trace_id": r.trace_id,
                        "user_id": r.user_id,
                        "scene": r.scene,
                        "env": r.env,
                        "tool": r.tool,
                        "args_digest": r.args_digest,
                        "ok": bool(r.ok),
                        "rows_affected": r.rows_affected,
                        "ms": r.ms,
                        "need_confirm": bool(r.need_confirm),
                        "confirmed_by": r.confirmed_by,
                        "created_at": r.created_at.isoformat() if r.created_at else None,
                    }
                    for r in rows
                ],
            }

    return {"code": 0, "data": await anyio.to_thread.run_sync(_query)}


@router.get("/tools")
async def list_tools(_: Identity = Depends(require_admin)):
    configs = registry.load_configs(force=True)
    specs = {spec.name: spec for spec in registry.all_specs()}
    names = list(specs) + [n for n in configs if n not in specs]
    items = []
    for name in names:
        spec = specs.get(name)
        cfg = configs.get(name, {})
        items.append(
            {
                "name": name,
                "description": spec.description[:120] if spec else "",
                "read_only": spec.read_only if spec else True,
                "enabled": cfg.get("enabled", True),
                "timeout_ms": cfg.get("timeout_ms", int((spec.timeout if spec else 15) * 1000)),
                "roles": cfg.get("roles", ["ADMIN", "USER"]),
                "configured": bool(cfg),
            }
        )
    return {"code": 0, "data": items}


@router.put("/tools/{name}")
async def update_tool(name: str, payload: ToolConfigUpdate, identity: Identity = Depends(require_admin)):
    _require_db()
    if registry.get(name) is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"工具 {name} 不存在")

    def _work() -> None:
        with session_scope() as db:
            row = db.get(AgentToolConfig, name)
            if row is None:
                row = AgentToolConfig(name=name, enabled=1, timeout_ms=15000, roles="ADMIN,USER", params={})
                db.add(row)
            if payload.enabled is not None:
                row.enabled = 1 if payload.enabled else 0
            if payload.timeout_ms is not None:
                row.timeout_ms = max(1000, min(payload.timeout_ms, 120000))
            if payload.roles is not None:
                row.roles = ",".join(r.upper() for r in payload.roles) or "ADMIN,USER"
            row.updated_by = identity.user_id

    await anyio.to_thread.run_sync(_work)
    registry.invalidate_cache()
    return {"code": 0, "msg": "updated"}


@router.get("/stats")
async def stats(_: Identity = Depends(require_admin)):
    _require_db()

    def _query() -> dict:
        with session_scope() as db:
            total_calls = db.query(func.count(AgentToolCall.id)).scalar() or 0
            failed = db.query(func.count(AgentToolCall.id)).filter(AgentToolCall.ok == 0).scalar() or 0
            sessions = db.query(func.count(AgentSession.id)).scalar() or 0
            by_tool = db.query(AgentToolCall.tool, func.count(AgentToolCall.id)).group_by(AgentToolCall.tool).all()
            return {
                "total_calls": int(total_calls),
                "failed_calls": int(failed),
                "sessions": int(sessions),
                "by_tool": {name: int(count) for name, count in by_tool},
            }

    return {"code": 0, "data": await anyio.to_thread.run_sync(_query)}
