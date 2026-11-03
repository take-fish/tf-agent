"""确认令牌：无状态签名、防篡改、防重放、过期与用户绑定。"""

from __future__ import annotations

import pytest

from app.agent import confirm_store


@pytest.fixture(autouse=True)
def _clear_used():
    confirm_store._used_nonces.clear()
    yield
    confirm_store._used_nonces.clear()


def _create(user_id: int = 7, tool: str = "restart_service"):
    return confirm_store.create(
        tool=tool,
        arguments={"service": "tf-game", "env": "prod"},
        user_id=user_id,
        trace_id="trace-1",
        env="prod",
        scene="ops",
        description="重启服务：tf-game",
    )


def test_token_roundtrip_keeps_payload():
    action = _create()
    got = confirm_store.consume(action.confirm_id, 7)
    assert got.tool == "restart_service"
    assert got.arguments == {"service": "tf-game", "env": "prod"}
    assert got.trace_id == "trace-1"
    assert got.description == "重启服务：tf-game"


def test_token_is_not_reusable():
    action = _create()
    confirm_store.consume(action.confirm_id, 7)
    with pytest.raises(KeyError):
        confirm_store.consume(action.confirm_id, 7)


def test_tampered_payload_is_rejected():
    action = _create()
    body, signature = action.confirm_id.split(".", 1)
    tampered = ("A" + body[1:]) + "." + signature
    with pytest.raises(KeyError):
        confirm_store.consume(tampered, 7)


def test_other_user_cannot_confirm():
    action = _create(user_id=7)
    with pytest.raises(PermissionError):
        confirm_store.consume(action.confirm_id, 8)


def test_expired_token_is_rejected(monkeypatch):
    monkeypatch.setattr(confirm_store, "ttl_seconds", lambda: -1)
    action = _create()
    with pytest.raises(TimeoutError):
        confirm_store.consume(action.confirm_id, 7)


def test_garbage_token_is_rejected():
    for token in ("", "abc", "a.b.c", "x" * 50):
        with pytest.raises(KeyError):
            confirm_store.consume(token, 7)
