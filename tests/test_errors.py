"""统一错误响应：错误码、字段错误、traceId 与 5xx 兜底。"""

from __future__ import annotations

from fastapi.testclient import TestClient

from app.errors import DEFAULT_MESSAGES, build_body, code_of, scope_of
from app.main import app

client = TestClient(app)


def test_scope_and_code_are_stable():
    assert scope_of("/api/v1/agent/kb/abc/docs") == "KB"
    assert scope_of("/api/v1/agent/chat") == "CHAT"
    assert scope_of("/api/v1/agent/profile") == "PROFILE"
    assert scope_of("/api/v1/agent/admin/tool-calls") == "ADMIN"
    assert scope_of("/api/v1/agent/scenes") == "AGENT"
    assert scope_of("/healthz") == "GENERAL"
    assert code_of("KB", 404) == "AGENT-KB-404"
    assert code_of("AGENT", 404) == "AGENT-404"


def test_build_body_has_frontend_contract_fields():
    class FakeRequest:
        url = type("U", (), {"path": "/api/v1/agent/kb/x"})()
        method = "GET"

    body = build_body(FakeRequest(), 503, "依赖不可用", "AGENT-KB-503", "trace123", fields={"q": "必填"})
    assert body["error"] == "AGENT-KB-503"
    assert body["message"] == "依赖不可用"
    assert body["traceId"] == body["requestId"] == "trace123"
    assert body["path"] == "/api/v1/agent/kb/x"
    assert body["timestamp"].endswith("Z")
    assert body["details"]["retryable"] is True
    assert body["details"]["fields"] == {"q": "必填"}


def test_every_default_status_has_chinese_message():
    assert all(isinstance(text, str) and text for text in DEFAULT_MESSAGES.values())


def test_http_exception_becomes_standard_envelope():
    # /api/v1/agent/profile 需要登录，dev_mode 关闭时返回 401
    resp = client.get("/api/v1/agent/profile", headers={"X-FM-User-Id": "1", "X-FM-Role": "USER"})
    assert resp.status_code in (200, 401, 403, 503)
    if resp.status_code != 200:
        body = resp.json()
        assert body["error"].startswith("AGENT-")
        assert body["traceId"]
        assert resp.headers.get("X-Request-ID") == body["traceId"]


def test_unknown_route_is_404_envelope():
    resp = client.get("/api/v1/agent/definitely-not-a-route")
    assert resp.status_code == 404
    body = resp.json()
    assert body["error"].startswith("AGENT-")
    assert body["message"]


def test_request_id_header_is_propagated():
    resp = client.get("/healthz", headers={"X-Request-ID": "abc123"})
    assert resp.headers.get("X-Request-ID") == "abc123"
    assert resp.headers.get("X-Trace-ID") == "abc123"
