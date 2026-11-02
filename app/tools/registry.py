"""工具注册表 + 配置过滤。

配置来自 fm_agent.agent_tool_config（管理台可改），进程内缓存 30 秒；
数据库不可用时退回代码里的默认值，不影响可用性。
"""

from __future__ import annotations

import logging
import time
from typing import Any

from app.tools.base import ToolSpec

logger = logging.getLogger(__name__)

_REGISTRY: dict[str, ToolSpec] = {}

_CONFIG_TTL = 30.0
_config_cache: dict[str, dict[str, Any]] = {}
_config_loaded_at = 0.0


def register(spec: ToolSpec) -> ToolSpec:
    _REGISTRY[spec.name] = spec
    return spec


def get(name: str) -> ToolSpec | None:
    return _REGISTRY.get(name)


def all_specs() -> list[ToolSpec]:
    return list(_REGISTRY.values())


def load_configs(force: bool = False) -> dict[str, dict[str, Any]]:
    """读取管理台配置（30s 缓存）。"""
    global _config_cache, _config_loaded_at
    now = time.monotonic()
    if not force and _config_cache and now - _config_loaded_at < _CONFIG_TTL:
        return _config_cache
    try:
        from app.db import db_available, session_scope
        from app.models import AgentToolConfig

        if not db_available():
            return _config_cache
        with session_scope() as db:
            rows = db.query(AgentToolConfig).all()
            _config_cache = {
                row.name: {
                    "enabled": bool(row.enabled),
                    "timeout_ms": row.timeout_ms,
                    "roles": [r.strip().upper() for r in (row.roles or "").split(",") if r.strip()],
                    "params": row.params or {},
                }
                for row in rows
            }
        _config_loaded_at = now
    except Exception as exc:  # noqa: BLE001 - 配置读不到就用默认值
        logger.debug("工具配置读取失败，使用默认值：%s", exc)
    return _config_cache


def allowed_tools(identity, scene_tools: list[str] | None = None) -> list[ToolSpec]:
    """按 场景白名单 + 管理台启用状态 + 角色 过滤。"""
    configs = load_configs()
    result: list[ToolSpec] = []
    for spec in _REGISTRY.values():
        if scene_tools is not None and spec.name not in scene_tools:
            continue
        cfg = configs.get(spec.name)
        # requires_config 的工具（发布 / 重启）必须在管理台配过才允许出现，
        # 否则模型会看到"调了才报错"的工具，白浪费一轮。
        if spec.requires_config and cfg is None:
            continue
        if cfg is not None:
            if not cfg["enabled"]:
                continue
            roles = cfg["roles"] or ["ADMIN", "USER"]
            user_roles = {r.upper() for r in identity.roles} | {identity.role.upper()}
            if roles and not (user_roles & set(roles)) and not identity.is_admin:
                continue
        result.append(spec)
    return result


def timeout_for(name: str) -> float:
    cfg = load_configs().get(name)
    if cfg and cfg.get("timeout_ms"):
        return max(1.0, cfg["timeout_ms"] / 1000.0)
    spec = _REGISTRY.get(name)
    return spec.timeout if spec else 20.0


def invalidate_cache() -> None:
    global _config_loaded_at
    _config_loaded_at = 0.0
