"""写操作确认：无状态签名令牌（进程重启不丢，也不占内存队列）。

confirm_id 本身就是凭证：

    base64url(payload) + "." + base64url(HMAC-SHA256(secret, payload))

payload = {tool, args, user_id, env, scene, description, expires_at, nonce}

服务端不存任何东西，校验时只做四件事：签名、过期、用户一致、nonce 未被用过。
代价是"已用 nonce"只能记在进程内（重启后 60 秒窗口内理论上可重放一次），
换来的是：确认不因重启 / 滚动发布丢失，且参数被篡改一定验不过。
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import time
from dataclasses import dataclass, field

from app import nacos
from app.config import settings

TTL_SECONDS = 60
# nonce -> 过期时间；只用于防重放，进程重启后清空
_used_nonces: dict[str, float] = {}


@dataclass
class PendingAction:
    confirm_id: str
    tool: str
    arguments: dict
    user_id: int
    trace_id: str
    env: str
    scene: str
    description: str
    expires_at: float
    args_digest: str = field(default="")

    @property
    def expired(self) -> bool:
        return time.time() > self.expires_at


def _secret() -> bytes:
    secret = settings.confirm_secret or settings.identity_secret or "fm-agent-dev-confirm"
    return secret.encode()


def _b64encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def _b64decode(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def _sign(raw: bytes) -> str:
    return _b64encode(hmac.new(_secret(), raw, hashlib.sha256).digest())


def ttl_seconds() -> int:
    """确认有效期：Nacos 的 write-operations.confirm-ttl-seconds 优先。"""
    try:
        return max(10, int(nacos.write_operation("confirm-ttl-seconds", TTL_SECONDS)))
    except (TypeError, ValueError):
        return TTL_SECONDS


def digest_args(arguments: dict) -> str:
    canonical = json.dumps(arguments, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode()).hexdigest()[:32]


def _encode(action: PendingAction) -> str:
    payload = {
        "t": action.tool,
        "a": action.arguments,
        "u": action.user_id,
        "r": action.trace_id,
        "e": action.env,
        "s": action.scene,
        "d": action.description,
        "x": int(action.expires_at),
        "n": action.confirm_id.split(".")[0][:16],
    }
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    return f"{_b64encode(raw)}.{_sign(raw)}"


def create(
    *,
    tool: str,
    arguments: dict,
    user_id: int,
    trace_id: str,
    env: str,
    scene: str,
    description: str,
) -> PendingAction:
    _purge()
    now = time.time()
    action = PendingAction(
        confirm_id=secrets.token_hex(8),  # 先占位 nonce，下面 _encode 会把 nonce 放进载荷
        tool=tool,
        arguments=arguments,
        user_id=user_id,
        trace_id=trace_id,
        env=env,
        scene=scene,
        description=description,
        expires_at=now + ttl_seconds(),
        args_digest=digest_args(arguments),
    )
    action.confirm_id = _encode(action)
    return action


def _decode(token: str) -> PendingAction:
    try:
        body, signature = token.split(".", 1)
        raw = _b64decode(body)
        if not hmac.compare_digest(signature, _sign(raw)):
            raise ValueError("签名不匹配")
        payload = json.loads(raw)
        return PendingAction(
            confirm_id=str(payload["n"]),
            tool=str(payload["t"]),
            arguments=dict(payload["a"]),
            user_id=int(payload["u"]),
            trace_id=str(payload.get("r") or ""),
            env=str(payload["e"]),
            scene=str(payload["s"]),
            description=str(payload["d"]),
            expires_at=float(payload["x"]),
            args_digest=digest_args(dict(payload["a"])),
        )
    except KeyError as exc:  # 缺字段
        raise KeyError("确认请求不存在或已使用") from exc
    except Exception as exc:  # noqa: BLE001 - 任何解析/验签失败都按"无效"处理
        raise KeyError("确认请求不存在或已使用") from exc


def consume(confirm_id: str, user_id: int) -> PendingAction:
    _purge()
    action = _decode(confirm_id)
    if action.expired:
        raise TimeoutError("确认已过期，请重新发起")
    if action.user_id != user_id:
        raise PermissionError("该确认属于其他用户")
    if action.confirm_id in _used_nonces:
        raise KeyError("确认请求不存在或已使用")
    _used_nonces[action.confirm_id] = action.expires_at
    return action


def peek(confirm_id: str) -> PendingAction | None:
    try:
        action = _decode(confirm_id)
    except KeyError:
        return None
    return None if action.expired else action


def _purge() -> None:
    now = time.time()
    for key in [key for key, expires_at in _used_nonces.items() if expires_at < now]:
        _used_nonces.pop(key, None)
