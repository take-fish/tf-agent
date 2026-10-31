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
        return code in self.permissions

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


def dev_identity(request: Request) -> Identity:
    """开发模式身份：允许用 X-FM-User-Id / X-FM-Role 伪造，仅本机调试。"""
    raw_id = request.headers.get("x-fm-user-id", "1")
    raw_role = request.headers.get("x-fm-role", ADMIN_ROLE)
    try:
        user_id = int(raw_id)
    except ValueError:
        user_id = 1
    role = raw_role.upper() if raw_role.isascii() else ADMIN_ROLE
    return Identity(
        user_id=user_id,
        username=f"dev-{user_id}",
        nickname="开发者",
        role=role,
        roles=(role,),
        permissions=("agent:use", "agent:admin", "agent:kb:write"),
    )


async def get_identity(request: Request) -> Identity:
    if settings.dev_mode:
        return dev_identity(request)
    return verify_signature({k.lower(): v for k, v in request.headers.items()})


async def require_admin(request: Request) -> Identity:
    identity = await get_identity(request)
    if not identity.is_admin:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "需要管理员权限")
    return identity


async def require_agent_use(request: Request) -> Identity:
    """普通员工：能对话、能上传知识库文件（上传后要等管理员审核才进索引）。"""
    identity = await get_identity(request)
    allowed = (
        identity.is_admin
        or identity.has_permission("agent:use")
        or identity.has_permission("agent:admin")
        or identity.has_permission("agent:kb:write")
    )
    if not allowed:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "没有使用智能助手的权限")
    return identity


async def require_kb_write(request: Request) -> Identity:
    identity = await get_identity(request)
    if identity.is_admin or identity.has_permission("agent:kb:write") or identity.has_permission("agent:admin"):
        return identity
    raise HTTPException(status.HTTP_403_FORBIDDEN, "无权管理知识库")
