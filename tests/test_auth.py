"""网关签名校验的单元测试：必须与 tf-gateway/auth.go 的规范串一致。"""

from __future__ import annotations

import base64
import hashlib
import hmac
import time

import pytest
from fastapi import HTTPException

from app import auth


def _sign(secret: str, user_id: int, username: str, nickname: str, role: str,
          roles: str, permissions: str, exp: int) -> dict[str, str]:
    def b64(value: str) -> str:
        return base64.urlsafe_b64encode(value.encode()).rstrip(b"=").decode()

    canonical = f"{user_id}|{b64(username)}|{b64(nickname)}|{b64(role)}|{roles}|{permissions}|{exp}"
    mac = hmac.new(secret.encode(), canonical.encode(), hashlib.sha256)
    return {
        "x-fm-user-id": str(user_id),
        "x-fm-username": b64(username),
        "x-fm-nickname": b64(nickname),
        "x-fm-role": b64(role),
        "x-fm-roles": roles,
        "x-fm-permissions": permissions,
        "x-fm-exp": str(exp),
        "x-fm-signature": base64.urlsafe_b64encode(mac.digest()).rstrip(b"=").decode(),
    }


@pytest.fixture(autouse=True)
def _secret(monkeypatch):
    monkeypatch.setattr(auth.settings, "identity_secret", "s3cr3t-for-test")
    monkeypatch.setattr(auth.settings, "identity_ttl", 120)


def test_verify_ok():
    headers = _sign("s3cr3t-for-test", 12, "alice", "爱丽丝", "ADMIN", "ADMIN,USER", "agent:admin",
                    int(time.time()) + 60)
    identity = auth.verify_signature(headers)
    assert identity.user_id == 12
    assert identity.nickname == "爱丽丝"
    assert identity.is_admin is True
    assert identity.has_permission("agent:admin") is True


def test_verify_bad_signature():
    headers = _sign("s3cr3t-for-test", 12, "alice", "爱丽丝", "USER", "USER", "", int(time.time()) + 60)
    headers["x-fm-roles"] = "ADMIN"  # 篡改角色
    with pytest.raises(HTTPException) as exc:
        auth.verify_signature(headers)
    assert exc.value.status_code == 401


def test_verify_expired():
    headers = _sign("s3cr3t-for-test", 1, "bob", "鲍勃", "USER", "USER", "", int(time.time()) - 600)
    with pytest.raises(HTTPException) as exc:
        auth.verify_signature(headers)
    assert exc.value.status_code == 401


def test_missing_secret(monkeypatch):
    monkeypatch.setattr(auth.settings, "identity_secret", "")
    with pytest.raises(HTTPException) as exc:
        auth.verify_signature({"x-fm-user-id": "1"})
    assert exc.value.status_code == 503
