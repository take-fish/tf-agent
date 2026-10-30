"""写操作确认队列（进程内，TTL 60 秒）。

只有真正执行前才校验：user_id 一致 + 未过期 + 参数哈希未被篡改。
"""

from __future__ import annotations

import hashlib
import json
import secrets
import time
from dataclasses import dataclass, field

TTL_SECONDS = 60


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


_pending: dict[str, PendingAction] = {}


def digest_args(arguments: dict) -> str:
    canonical = json.dumps(arguments, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode()).hexdigest()[:32]


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
    action = PendingAction(
        confirm_id=secrets.token_hex(8),
        tool=tool,
        arguments=arguments,
        user_id=user_id,
        trace_id=trace_id,
        env=env,
        scene=scene,
        description=description,
        expires_at=time.time() + TTL_SECONDS,
        args_digest=digest_args(arguments),
    )
    _pending[action.confirm_id] = action
    return action


def consume(confirm_id: str, user_id: int) -> PendingAction:
    action = _pending.get(confirm_id)
    if action is None:
        raise KeyError("确认请求不存在或已使用")
    if action.expired:
        _pending.pop(confirm_id, None)
        raise TimeoutError("确认已过期，请重新发起")
    if action.user_id != user_id:
        raise PermissionError("该确认属于其他用户")
    if digest_args(action.arguments) != action.args_digest:
        _pending.pop(confirm_id, None)
        raise PermissionError("参数已被篡改")
    _pending.pop(confirm_id, None)
    return action


def peek(confirm_id: str) -> PendingAction | None:
    action = _pending.get(confirm_id)
    if action and action.expired:
        _pending.pop(confirm_id, None)
        return None
    return action


def _purge() -> None:
    now = time.time()
    for key in [k for k, v in _pending.items() if v.expires_at < now]:
        _pending.pop(key, None)
