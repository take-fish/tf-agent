"""search_logs —— 按 trace_id / 服务名检索 Loki。"""

from __future__ import annotations

import json
import logging
import time
from datetime import UTC, datetime

import httpx

from app.config import settings
from app.tools.base import ToolResult, ToolSpec, obj_schema

logger = logging.getLogger(__name__)

MAX_LINE_CHARS = 200
STREAM_SELECTOR = 'stack="fishmatrix"'
# 这些是 `| json` 解析出来的字段，不能拼进 `{}` 流选择器（Loki 会报语法错误）。
JSON_FIELDS = {"service", "env", "trace_id", "request_id", "level", "message"}


def _quote(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"')


def _build_query(trace_id: str, service: str, keyword: str) -> str:
    """默认按流标签 stack=fishmatrix 选流，service 走 JSON 字段过滤。

    Alloy 只把 stack/container/compose_service/stream 写成流标签，
    日志里的 service 是 JSON 字段；直接写 `{service="tf-game"}` 会查不到。
    若 LOKI_SERVICE_LABEL 显式配成真实流标签，则按标签过滤。
    """
    label = (settings.loki_service_label or "").strip()
    use_label = bool(service and label and label not in JSON_FIELDS)
    query = f'{{{label}="{_quote(service)}"}}' if use_label else f"{{{STREAM_SELECTOR}}}"
    need_json = False
    if service and not use_label:
        query += " | json"
        need_json = True
        query += f' | service="{_quote(service)}"'
    if trace_id:
        if not need_json:
            query += " | json"
            need_json = True
        query += f' | trace_id="{_quote(trace_id)}"'
    if keyword:
        query += f' |= "{_quote(keyword)}"'
    return query


def _line_service(line: str) -> str:
    """从单行 JSON 日志里取 service；流标签中没有这个字段。"""
    try:
        payload = json.loads(line)
    except (TypeError, ValueError):
        return ""
    return str(payload.get("service") or "") if isinstance(payload, dict) else ""


def _fmt_ts(ns: str) -> str:
    try:
        ts = int(ns) / 1e9
        return datetime.fromtimestamp(ts, tz=UTC).astimezone().strftime("%m-%d %H:%M:%S")
    except Exception:  # noqa: BLE001
        return "?"


async def search_logs(
    trace_id: str = "",
    service: str = "",
    keyword: str = "",
    range_minutes: int = 30,
    limit: int = 20,
) -> ToolResult:
    trace_id = (trace_id or "").strip()
    service = (service or "").strip()
    keyword = (keyword or "").strip()
    limit = max(1, min(int(limit or 20), 50))
    range_minutes = max(1, min(int(range_minutes or 30), 24 * 60))

    if not trace_id and not service and not keyword:
        return ToolResult.error("至少需要 trace_id / service / keyword 之一，否则会扫全量日志")

    now = time.time()
    params = {
        "query": _build_query(trace_id, service, keyword),
        "start": str(int((now - range_minutes * 60) * 1e9)),
        "end": str(int(now * 1e9)),
        "limit": str(limit),
        "direction": "backward",
    }
    url = f"{settings.loki_url.rstrip('/')}/loki/api/v1/query_range"
    try:
        async with httpx.AsyncClient(timeout=settings.loki_timeout) as client:
            resp = await client.get(url, params=params)
            resp.raise_for_status()
            payload = resp.json()
    except httpx.HTTPStatusError as exc:
        return ToolResult.error(f"Loki 返回 {exc.response.status_code}: {exc.response.text[:200]}")
    except Exception as exc:  # noqa: BLE001
        return ToolResult.error(f"Loki 不可达（{settings.loki_url}）：{exc}")

    entries: list[tuple[str, str]] = []
    for stream in (payload.get("data") or {}).get("result") or []:
        labels = stream.get("stream") or {}
        stream_prefix = (
            labels.get("service")
            or labels.get("compose_service")
            or labels.get("container")
            or labels.get("job")
            or ""
        )
        for value in stream.get("values") or []:
            if len(value) < 2:
                continue
            line = value[1]
            prefix = _line_service(line) or stream_prefix
            entries.append((value[0], f"[{prefix}] {line}" if prefix else line))

    entries.sort(key=lambda item: int(item[0]), reverse=True)
    entries = entries[:limit]

    if not entries:
        cond = " ".join(
            filter(
                None,
                [
                    f"trace_id={trace_id}" if trace_id else "",
                    f"service={service}" if service else "",
                    f"keyword={keyword}" if keyword else "",
                ],
            )
        )
        return ToolResult(text=f"最近 {range_minutes} 分钟没有匹配的日志（{cond}）", rows=0)

    lines = []
    for ns, line in entries:
        clean = " ".join(line.split())[:MAX_LINE_CHARS]
        lines.append(f"{_fmt_ts(ns)} {clean}")
    return ToolResult(
        text="\n".join(lines),
        rows=len(lines),
        data={"query": params["query"], "range_minutes": range_minutes},
    )


spec = ToolSpec(
    name="search_logs",
    description="按 trace_id、服务名或关键词检索 Loki 日志，返回最近若干条（新的在前）。"
    "排查报错时先用 trace_id，不知道 trace_id 时用 service + keyword。",
    parameters=obj_schema(
        {
            "trace_id": {"type": "string", "description": "链路编号，例如 7d3f9a1c2b8e4f60"},
            "service": {"type": "string", "description": "服务名，例如 tf-game / tf-gateway / tf-user"},
            "keyword": {"type": "string", "description": "日志正文关键词，例如 ERROR / timeout"},
            "range_minutes": {
                "type": "integer",
                "description": "检索最近多少分钟，默认 30",
                "minimum": 1,
                "maximum": 1440,
            },
            "limit": {"type": "integer", "description": "最多返回条数，默认 20", "minimum": 1, "maximum": 50},
        }
    ),
    handler=search_logs,
    read_only=True,
    timeout=15.0,
)
