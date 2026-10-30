"""query_view —— 只读白名单视图查询（绝不接受自由 SQL）。"""

from __future__ import annotations

import logging
import re
from typing import Any

import anyio.to_thread
from sqlalchemy import create_engine, text

from app.config import settings
from app.tools.base import ToolResult, ToolSpec, obj_schema

logger = logging.getLogger(__name__)

_IDENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,63}$")
MAX_ROWS = 50
MAX_VALUE_CHARS = 60

_engine = None
_columns_cache: dict[str, set[str]] = {}


def _get_engine():
    global _engine
    if _engine is None:
        _engine = create_engine(settings.db_dsn, pool_pre_ping=True, pool_size=3, pool_recycle=1800)
    return _engine


def _load_columns(view: str) -> set[str]:
    if view in _columns_cache:
        return _columns_cache[view]
    with _get_engine().connect() as conn:
        rows = conn.execute(
            text(
                "SELECT COLUMN_NAME FROM information_schema.COLUMNS "
                "WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = :t"
            ),
            {"t": view},
        ).fetchall()
    cols = {r[0] for r in rows}
    _columns_cache[view] = cols
    return cols


def _run_query(view: str, filters: dict[str, Any], limit: int, order_by: str) -> tuple[list[str], list[dict]]:
    columns = _load_columns(view)
    if not columns:
        raise ValueError(f"视图 {view} 不存在或没有权限")

    where_parts: list[str] = []
    params: dict[str, Any] = {"_limit": limit}
    for idx, (key, value) in enumerate(filters.items()):
        if not _IDENT.match(key) or key not in columns:
            raise ValueError(f"字段 {key} 不在视图 {view} 的白名单列中")
        params[f"p{idx}"] = value
        where_parts.append(f"`{key}` = :p{idx}")

    sql = f"SELECT * FROM `{view}`"
    if where_parts:
        sql += " WHERE " + " AND ".join(where_parts)
    if order_by and _IDENT.match(order_by) and order_by in columns:
        sql += f" ORDER BY `{order_by}` DESC"
    sql += " LIMIT :_limit"

    with _get_engine().connect() as conn:
        result = conn.execute(text(sql), params)
        names = list(result.keys())
        rows = [dict(zip(names, row, strict=False)) for row in result.fetchall()]
    lines = []
    for row in rows:
        cells = []
        for col in names:
            value = row.get(col)
            text_value = "" if value is None else str(value).replace("\n", " ")
            cells.append(f"{col}={text_value[:MAX_VALUE_CHARS]}")
        lines.append(" | ".join(cells))
    return lines, rows


async def query_view(view: str, filters: dict[str, Any] | None = None, limit: int = 20) -> ToolResult:
    view = (view or "").strip()
    allowed = settings.db_views
    if not settings.db_dsn:
        return ToolResult.error("只读数据库未配置（FM_AGENT_DB_DSN），无法查询")
    if not allowed:
        return ToolResult.error("未配置只读视图白名单（FM_AGENT_DB_VIEWS），拒绝查询")
    if view not in allowed:
        return ToolResult.error(f"视图 {view} 不在白名单内，可选：{', '.join(sorted(allowed))}")

    filters = {k: v for k, v in (filters or {}).items() if v not in (None, "")}
    limit = max(1, min(int(limit or 20), MAX_ROWS))
    order_by = ""  # 白名单 value 是视图说明，不在工具层做排序，避免暴露非索引列

    try:
        lines, rows = await anyio.to_thread.run_sync(
            lambda: _run_query(view, filters, limit, str(order_by).strip())
        )
    except Exception as exc:  # noqa: BLE001
        return ToolResult.error(f"查询失败：{exc}")

    if not lines:
        return ToolResult(text=f"视图 {view} 无匹配数据（过滤条件 {filters or '无'}）", rows=0)
    return ToolResult(
        text=f"视图 {view} 命中 {len(lines)} 行：\n" + "\n".join(lines),
        rows=len(lines),
        data={"view": view, "filters": filters},
    )


spec = ToolSpec(
    name="query_view",
    description="查询只读数据库视图（白名单），返回最多 20 行。只能按视图已有字段做等值过滤，"
    "不能写 SQL、不能查非白名单视图。",
    parameters=obj_schema(
        {
            "view": {"type": "string", "description": "视图名，必须在白名单内"},
            "filters": {
                "type": "object",
                "description": "等值过滤条件，例如 {\"biz_date\": \"2026-10-30\"}",
                "additionalProperties": {"type": "string"},
            },
            "limit": {"type": "integer", "description": "返回行数，默认 20，最大 50", "minimum": 1, "maximum": 50},
        },
        ["view"],
    ),
    handler=query_view,
    read_only=True,
    timeout=15.0,
)
