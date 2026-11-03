"""统一错误响应 + 请求链路号（traceId）+ 结构化错误日志。

错误体与网关/Java 服务保持一致（前端 `lib/http.ts` 的 parseError 直接按它解析）：

    {"error": "AGENT-KB-404", "message": "知识库不存在", "traceId": "...",
     "requestId": "...", "path": "/api/v1/agent/kb/x", "timestamp": "...Z",
     "details": {"retryable": false, "fields": {"query": "不能为空"}}}

错误码规则：`AGENT-<SCOPE>-<HTTP 状态码>`，SCOPE 由请求路径决定
（CHAT / KB / PROFILE / CONFIRM / ADMIN / AGENT），既稳定又不用每个 raise 手写。
"""

from __future__ import annotations

import logging
import time
import uuid
from datetime import UTC, datetime
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from prometheus_client import Counter
from starlette.exceptions import HTTPException as StarletteHTTPException

logger = logging.getLogger("app.errors")

ERRORS = Counter("fm_agent_errors_total", "错误响应数（按错误码/范围/状态码）", ["code", "scope", "status"])

TRACE_HEADERS = ("x-request-id", "x-trace-id")

# 路径前缀 → 错误码里的业务范围；按顺序匹配，越具体越靠前
SCOPES: tuple[tuple[str, str], ...] = (
    ("/api/v1/agent/kb", "KB"),
    ("/api/v1/agent/chat", "CHAT"),
    ("/api/v1/agent/confirm", "CONFIRM"),
    ("/api/v1/agent/profile", "PROFILE"),
    ("/api/v1/agent/admin", "ADMIN"),
    ("/api/v1/agent", "AGENT"),
)

DEFAULT_MESSAGES: dict[int, str] = {
    400: "请求参数有误",
    401: "未登录或登录已失效",
    403: "没有操作权限",
    404: "资源不存在",
    405: "请求方法不被允许",
    409: "资源状态冲突",
    413: "请求体过大",
    415: "不支持的请求格式",
    422: "请求参数校验未通过",
    429: "请求过于频繁，请稍后再试",
    500: "服务内部错误",
    502: "上游服务不可用",
    503: "依赖服务暂不可用，请稍后重试",
    504: "上游服务超时",
}

# 这些状态码值得让用户「重试」；业务校验失败重试也没用
RETRYABLE = {429, 500, 502, 503, 504}


def scope_of(path: str) -> str:
    for prefix, scope in SCOPES:
        if path.startswith(prefix):
            return scope
    return "GENERAL"


def code_of(scope: str, status_code: int) -> str:
    # 基础范围不重复前缀：/api/v1/agent/xxx -> AGENT-404，而不是 AGENT-AGENT-404
    if scope == "AGENT":
        return f"AGENT-{status_code}"
    return f"AGENT-{scope}-{status_code}"


def trace_id_of(request: Request) -> str:
    trace = getattr(request.state, "trace_id", "") or ""
    if trace:
        return trace
    for name in TRACE_HEADERS:
        value = request.headers.get(name, "").strip()
        if value:
            return value[:64]
    return uuid.uuid4().hex[:16]


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _split_detail(detail: Any) -> tuple[str, str | None, dict[str, Any] | None]:
    """从 HTTPException.detail 里取 message / 自定义 code / 字段错误。"""
    if isinstance(detail, dict):
        message = str(detail.get("message") or "")
        code = str(detail.get("code") or "") or None
        fields = detail.get("fields") if isinstance(detail.get("fields"), dict) else None
        return message, code, fields
    if isinstance(detail, (list, tuple)):
        # FastAPI 原生 422 结构：[{loc, msg, type}]
        fields = {}
        for item in detail:
            if not isinstance(item, dict):
                continue
            loc = [str(part) for part in item.get("loc", []) if str(part) not in ("body", "query", "path")]
            key = ".".join(loc) or "body"
            fields[key] = str(item.get("msg") or "参数不合法")
        return "", None, fields or None
    return (str(detail) if detail else ""), None, None


def build_body(
    request: Request,
    status_code: int,
    message: str,
    code: str,
    trace_id: str,
    *,
    retryable: bool | None = None,
    fields: dict[str, Any] | None = None,
) -> dict[str, Any]:
    details: dict[str, Any] = {"retryable": status_code in RETRYABLE if retryable is None else retryable}
    if fields:
        details["fields"] = fields
    return {
        "error": code,
        "message": message,
        "traceId": trace_id,
        "requestId": trace_id,
        "path": request.url.path,
        "timestamp": _now(),
        "details": details,
    }


def _respond(
    request: Request,
    status_code: int,
    message: str = "",
    *,
    code: str | None = None,
    fields: dict[str, Any] | None = None,
    retryable: bool | None = None,
) -> JSONResponse:
    scope = scope_of(request.url.path)
    error_code = code or code_of(scope, status_code)
    text = message or DEFAULT_MESSAGES.get(status_code) or f"请求失败（HTTP {status_code}）"
    trace = trace_id_of(request)
    ERRORS.labels(code=error_code, scope=scope, status=str(status_code)).inc()
    logger.warning(
        "event=agent.error code=%s status=%s scope=%s trace_id=%s method=%s path=%s msg=%s",
        error_code,
        status_code,
        scope,
        trace,
        request.method,
        request.url.path,
        text,
    )
    return JSONResponse(
        status_code=status_code,
        content=build_body(request, status_code, text, error_code, trace, retryable=retryable, fields=fields),
        headers={"X-Request-ID": trace, "X-Trace-ID": trace},
    )


async def http_exception_handler(request: Request, exc: HTTPException) -> JSONResponse:
    message, custom_code, fields = _split_detail(exc.detail)
    return _respond(request, exc.status_code, message, code=custom_code, fields=fields)


async def validation_exception_handler(request: Request, exc: RequestValidationError) -> JSONResponse:
    fields: dict[str, str] = {}
    for item in exc.errors():
        loc = [str(part) for part in item.get("loc", []) if str(part) not in ("body", "query", "path")]
        fields[".".join(loc) or "body"] = str(item.get("msg") or "参数不合法")
    return _respond(request, 422, fields=fields)


async def unhandled_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    trace = trace_id_of(request)
    logger.exception(
        "event=agent.error code=AGENT-%s-500 status=500 trace_id=%s method=%s path=%s unhandled=%s",
        scope_of(request.url.path),
        trace,
        request.method,
        request.url.path,
        type(exc).__name__,
    )
    return _respond(request, 500)


class TraceMiddleware:
    """给每个响应打上 X-Request-ID / X-Trace-ID，并输出一条结构化访问日志。

    用纯 ASGI 实现（不用 BaseHTTPMiddleware）：SSE 响应在 body 开始前就要把
    头写出去，BaseHTTPMiddleware 在流式场景下改不了响应头。
    """

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        headers = {key.decode("latin-1").lower(): value.decode("latin-1") for key, value in scope.get("headers", [])}
        trace = (headers.get("x-request-id") or headers.get("x-trace-id") or uuid.uuid4().hex[:16])[:64]
        scope.setdefault("state", {})["trace_id"] = trace

        started = time.perf_counter()
        status_code = 500
        path = scope.get("path", "")

        async def send_wrapper(message):
            nonlocal status_code
            if message["type"] == "http.response.start":
                status_code = int(message.get("status", 500))
                raw = [
                    (k, v) for k, v in message.get("headers", []) if k.lower() not in (b"x-request-id", b"x-trace-id")
                ]
                raw.append((b"x-request-id", trace.encode("latin-1")))
                raw.append((b"x-trace-id", trace.encode("latin-1")))
                message["headers"] = raw
            await send(message)

        try:
            await self.app(scope, receive, send_wrapper)
        finally:
            if path.startswith("/api/"):
                logger.info(
                    "event=agent.request trace_id=%s method=%s path=%s status=%s ms=%d",
                    trace,
                    scope.get("method", ""),
                    path,
                    status_code,
                    int((time.perf_counter() - started) * 1000),
                )


def install(app: FastAPI) -> None:
    """注册全局异常处理与链路中间件（在 include_router 之后调用即可）。"""
    app.add_middleware(TraceMiddleware)
    # 注册 Starlette 基类：路由未命中（404）、方法不允许（405）也走同一套错误体
    app.add_exception_handler(StarletteHTTPException, http_exception_handler)
    app.add_exception_handler(RequestValidationError, validation_exception_handler)
    app.add_exception_handler(Exception, unhandled_exception_handler)
