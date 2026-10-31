"""权限：复用用户中台权限码，agent 侧不做角色推断。"""

from __future__ import annotations

from types import SimpleNamespace

from fastapi.testclient import TestClient

from app.auth import (
    AGENT_PERMISSIONS,
    PERM_AGENT_USE,
    PERM_CONSOLE_VIEW,
    PERM_DEPLOY_TRIGGER,
    PERM_KB_MANAGE,
    PERM_PROFILE_MANAGE,
    Identity,
    dev_identity,
)
from app.config import settings
from app.main import app

client = TestClient(app)


def test_dotted_permission_is_exact():
    identity = Identity(user_id=7, permissions=(PERM_AGENT_USE,))
    assert identity.has_permission(PERM_AGENT_USE)
    assert not identity.has_permission(PERM_KB_MANAGE)
    assert not identity.has_permission(PERM_PROFILE_MANAGE)


def test_legacy_colon_permissions_are_accepted_during_migration():
    legacy = Identity(user_id=1, permissions=("agent:admin",))
    for code in AGENT_PERMISSIONS:
        assert legacy.has_permission(code), code


def test_role_alone_grants_nothing():
    # 只有角色、没有权限码时，不再按 ADMIN 角色放行
    admin_role_only = Identity(user_id=1, role="ADMIN", roles=("ADMIN",), permissions=())
    assert not admin_role_only.has_permission(PERM_KB_MANAGE)
    assert not admin_role_only.has_permission(PERM_CONSOLE_VIEW)


def test_dev_identity_decodes_gateway_headers():
    import base64

    role = base64.urlsafe_b64encode(b"ADMIN").rstrip(b"=").decode()
    identity = dev_identity(
        SimpleNamespace(
            headers={
                "x-fm-role": role,
                "x-fm-permissions": "agent.chat.use,agent.kb.manage",
                "x-fm-username": base64.urlsafe_b64encode(b"admin").rstrip(b"=").decode(),
            }
        )
    )
    assert identity.role == "ADMIN"
    assert identity.username == "admin"
    assert identity.has_permission(PERM_KB_MANAGE)
    assert not identity.has_permission(PERM_PROFILE_MANAGE)


def test_dev_identity_maps_role_to_permissions():
    admin = dev_identity(SimpleNamespace(headers={"x-fm-role": "ADMIN"}))
    assert set(admin.permissions) == set(AGENT_PERMISSIONS)

    user = dev_identity(SimpleNamespace(headers={"x-fm-role": "USER"}))
    assert user.permissions == (PERM_AGENT_USE,)

    custom = dev_identity(SimpleNamespace(headers={"x-fm-role": "USER", "x-fm-permissions": PERM_DEPLOY_TRIGGER}))
    assert custom.permissions == (PERM_DEPLOY_TRIGGER,)


def test_console_endpoint_requires_console_permission(monkeypatch):
    monkeypatch.setattr(settings, "dev_mode", True)
    denied = client.get("/api/v1/agent/admin/stats", headers={"X-FM-Permissions": PERM_AGENT_USE})
    assert denied.status_code == 403
    assert denied.json()["message"] == "需要 Agent 管理台权限"
