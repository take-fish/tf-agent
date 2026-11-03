"""网关身份校验。

规范串与 tf-gateway/auth.go:signIdentity 必须逐字节一致：

    canonical = f"{id}|{b64url(username)}|{b64url(nickname)}|{b64url(role)}|{roles}|{permissions}|{exp}"
    sig       = base64url_nopad(hmac_sha256(FM_IDENTITY_SECRET, canonical))

网关透传的 username/nickname/role 已经是 b64url 编码后的值，直接参与拼接。
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import time
from dataclasses import dataclass, field

from fastapi import HTTPException, Request, status

from app.config import settings

HEADER_USER_ID = "x-fm-user-id"
HEADER_USERNAME = "x-fm-username"
HEADER_NICKNAME = "x-fm-nickname"
HEADER_ROLE = "x-fm-role"
HEADER_ROLES = "x-fm-roles"
HEADER_PERMISSIONS = "x-fm-permissions"
HEADER_EXP = "x-fm-exp"
HEADER_SIGNATURE = "x-fm-signature"

ADMIN_ROLE = "ADMIN"

# 权限码统一由用户中台维护（auth_permission / auth_role_permission），
# agent 只消费网关透传的 X-FM-Permissions，不再自建角色判断。
PERM_AGENT_USE = "agent.chat.use"
PERM_KB_MANAGE = "agent.kb.manage"
PERM_PROFILE_MANAGE = "agent.profile.manage"
PERM_CONSOLE_VIEW = "agent.console.view"
PERM_DEPLOY_TRIGGER = "agent.deploy.trigger"

AGENT_PERMISSIONS = (
    PERM_AGENT_USE,
    PERM_KB_MANAGE,
    PERM_PROFILE_MANAGE,
    PERM_CONSOLE_VIEW,
    PERM_DEPLOY_TRIGGER,
)

# 兼容期：早期版本用过冒号写法，用户中台迁移完成前继续认，避免升级窗口内管理员掉权限。
LEGACY_PERMISSIONS: dict[str, tuple[str, ...]] = {
    PERM_AGENT_USE: ("agent:use", "agent:kb:write", "agent:admin"),
    PERM_KB_MANAGE: ("agent:kb:write", "agent:admin"),
    PERM_PROFILE_MANAGE: ("agent:admin", "agent:kb:write"),
    PERM_CONSOLE_VIEW: ("agent:admin",),
    PERM_DEPLOY_TRIGGER: ("agent:deploy", "agent:admin"),
}


@dataclass(frozen=True)
class Identity:
    user_id: int
    username: str = ""
    nickname: str = ""
    role: str = ""
    roles: tuple[str, ...] = field(default_factory=tuple)
    permissions: tuple[str, ...] = field(default_factory=tuple)

    @property
    def is_admin(self) -> bool:
        return self.role.upper() == ADMIN_ROLE or ADMIN_ROLE in {r.upper() for r in self.roles}

    def has_permission(self, code: str) -> bool:
        if code in self.permissions:
            return True
        return any(alias in self.permissions for alias in LEGACY_PERMISSIONS.get(code, ()))

    @property
    def display_name(self) -> str:
        return self.nickname or self.username or f"user-{self.user_id}"


def _b64url_decode(value: str) -> str:
    try:
        padded = value + "=" * (-len(value) % 4)
        return base64.urlsafe_b64decode(padded.encode()).decode("utf-8", "replace")
    except Exception:  # noqa: BLE001 - 解码失败按原文处理
        return value


def verify_signature(headers: dict[str, str]) -> Identity:
    user_id_raw = headers.get(HEADER_USER_ID, "")
    signature = headers.get(HEADER_SIGNATURE, "")
    exp_raw = headers.get(HEADER_EXP, "")

    if not settings.identity_secret:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "FM_IDENTITY_SECRET 未配置，拒绝服务")
    if not user_id_raw or not signature or not exp_raw:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "缺少网关身份头")

    try:
        user_id = int(user_id_raw)
        expires_at = int(exp_raw)
    except ValueError as exc:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "身份头格式错误") from exc

    if expires_at and time.time() > expires_at + settings.identity_ttl:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "身份已过期")

    username = headers.get(HEADER_USERNAME, "")
    nickname = headers.get(HEADER_NICKNAME, "")
    role = headers.get(HEADER_ROLE, "")
    roles_raw = headers.get(HEADER_ROLES, "")
    perms_raw = headers.get(HEADER_PERMISSIONS, "")

    canonical = f"{user_id}|{username}|{nickname}|{role}|{roles_raw}|{perms_raw}|{expires_at}"
    mac = hmac.new(settings.identity_secret.encode(), canonical.encode(), hashlib.sha256)
    expected = base64.urlsafe_b64encode(mac.digest()).rstrip(b"=").decode()

    if not hmac.compare_digest(expected, signature):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "身份签名校验失败")

    return Identity(
        user_id=user_id,
        username=_b64url_decode(username),
        nickname=_b64url_decode(nickname),
        role=_b64url_decode(role),
        roles=tuple(r for r in roles_raw.split(",") if r),
        permissions=tuple(p for p in perms_raw.split(",") if p),
    )


def _maybe_b64url(value: str) -> str:
    """网关透传的 username/nickname/role 是 b64url 编码的；解密失败就按原文用。"""
    if not value:
        return ""
    try:
        decoded = _b64url_decode(value)
    except Exception:  # noqa: BLE001
        return value
    return decoded if decoded.isprintable() else value


def dev_identity(request: Request) -> Identity:
    """开发模式身份：不校验签名，直接采信网关写好的身份头，仅本机调试用。

    这样本地/测试环境也能走真实的用户中台权限（X-FM-Permissions 由网关按
    auth_role_permission 下发），不需要知道 FM_IDENTITY_SECRET；
    也支持手动传 X-FM-Permissions 验证权限边界。正式环境 dev_mode 必须关闭。
    """
    raw_id = request.headers.get("x-fm-user-id", "1")
    try:
        user_id = int(raw_id)
    except ValueError:
        user_id = 1

    role = _maybe_b64url(request.headers.get("x-fm-role", "")).upper() or ADMIN_ROLE
    roles_raw = request.headers.get("x-fm-roles", "")
    roles = tuple(r.strip() for r in roles_raw.split(",") if r.strip()) or (role,)

    raw_perms = request.headers.get("x-fm-permissions", "")
    permissions = tuple(p.strip() for p in raw_perms.split(",") if p.strip())
    if not permissions:
        permissions = AGENT_PERMISSIONS if role == ADMIN_ROLE else (PERM_AGENT_USE,)

    return Identity(
        user_id=user_id,
        username=_maybe_b64url(request.headers.get("x-fm-username", "")) or f"dev-{user_id}",
        nickname=_maybe_b64url(request.headers.get("x-fm-nickname", "")) or "开发者",
        role=role,
        roles=roles,
        permissions=permissions,
    )


async def get_identity(request: Request) -> Identity:
    if settings.dev_mode:
        return dev_identity(request)
    return verify_signature({k.lower(): v for k, v in request.headers.items()})


def require_permission(code: str, message: str):
    """生成 FastAPI 依赖：校验用户中台下发的权限码。

    权限的唯一事实来源是 tf-user 的 auth_role_permission（经网关 X-FM-Permissions 透传），
    agent 侧不做任何角色推断，也不再维护自己的管理员判定。
    """

    async def dependency(request: Request) -> Identity:
        identity = await get_identity(request)
        if not identity.has_permission(code):
            raise HTTPException(status.HTTP_403_FORBIDDEN, message)
        return identity

    dependency.__name__ = f"require_{code.replace('.', '_')}"
    return dependency


require_agent_use = require_permission(PERM_AGENT_USE, "没有使用智能助手的权限")
require_kb_manage = require_permission(PERM_KB_MANAGE, "需要知识库管理权限")
require_profile_manage = require_permission(PERM_PROFILE_MANAGE, "需要展示管理权限")
require_admin = require_permission(PERM_CONSOLE_VIEW, "需要 Agent 管理台权限")
require_deploy_trigger = require_permission(PERM_DEPLOY_TRIGGER, "没有触发发布/重启的权限")

# 兼容旧导入名（kb.py 早期用的是 require_kb_write）
require_kb_write = require_kb_manage
