"""聊天服务取「@名字」触发词：只校验签名，不要求 agent.chat.use。"""

from __future__ import annotations

from fastapi.testclient import TestClient

from app.agent import profile as profile_store
from app.auth import Identity, get_identity
from app.main import app


def _client(identity: Identity) -> TestClient:
    client = TestClient(app)
    app.dependency_overrides[get_identity] = lambda: identity
    return client


def teardown_function() -> None:
    app.dependency_overrides.clear()


def test_chat_identity_returns_admin_configured_name(monkeypatch):
    monkeypatch.setattr(
        profile_store,
        "load",
        lambda: {"name": "小鱼", "greeting": "x"},
    )
    client = _client(Identity(user_id=42, permissions=("agent.chat.use",)))

    resp = client.get("/api/v1/agent/chat-identity")

    assert resp.status_code == 200
    assert resp.json() == {"code": 0, "data": {"name": "小鱼", "mention": "@小鱼"}}


def test_chat_identity_needs_no_agent_permission(monkeypatch):
    """没有 agent.chat.use 的账号也能取名字，否则聊天侧永远不知道触发词。"""
    monkeypatch.setattr(profile_store, "load", lambda: {"name": "FishMatrix Agent"})
    client = _client(Identity(user_id=7, permissions=()))

    resp = client.get("/api/v1/agent/chat-identity")

    assert resp.status_code == 200
    assert resp.json()["data"]["name"] == "FishMatrix Agent"


def test_chat_identity_handles_blank_name(monkeypatch):
    monkeypatch.setattr(profile_store, "load", lambda: {"name": "   "})
    client = _client(Identity(user_id=1))

    resp = client.get("/api/v1/agent/chat-identity")

    assert resp.status_code == 200
    assert resp.json()["data"] == {"name": "", "mention": ""}
