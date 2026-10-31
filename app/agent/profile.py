"""Agent 展示配置：名称、3D 形象、问候语、推荐问题、主题色。

存在 MySQL `agent_profile` 单行（id='default'）。读不到（没建表/库挂了/还没配置）
就回落 `DEFAULT_PROFILE`，保证前端永远有完整形象可用——配置是展示层的，不该拖垮对话。
"""

from __future__ import annotations

import copy
import json
import logging
import re
from typing import Any

from app.db import db_available, session_scope
from app.models import AgentProfile

logger = logging.getLogger(__name__)

PROFILE_ID = "default"
AVATAR_STYLES = ("orb", "cube", "ring")
_HEX = re.compile(r"^#[0-9a-fA-F]{6}$")
_MAX_SUGGESTIONS = 6

DEFAULT_PROFILE: dict[str, Any] = {
    "name": "FishMatrix Agent",
    "tagline": "内网智能运维助手",
    "avatar": {
        "style": "orb",
        "primary": "#58a6ff",
        "accent": "#a371f7",
        "size": 96,
        "spin": True,
    },
    "greeting": "你好，我是 FishMatrix 运维智能助手。查日志、看健康、查知识库、发布和重启都可以直接问我。",
    "suggestions": [
        "发布流程是什么？回滚怎么做？",
        "测试环境出问题，第一步看什么？",
        "tf-game 最近半小时有报错吗？",
        "知识库里的值班手册怎么写的？",
    ],
    "theme": "#58a6ff",
    "show_status_bar": True,
}


class ProfileError(ValueError):
    """配置不合法（返回 400，前端展示具体是哪一项）。"""


def defaults() -> dict[str, Any]:
    return copy.deepcopy(DEFAULT_PROFILE)


def _text(value: Any, field: str, max_len: int) -> str:
    text = str(value or "").strip()
    if not text:
        raise ProfileError(f"{field}不能为空")
    if len(text) > max_len:
        raise ProfileError(f"{field}最多 {max_len} 个字")
    return text


def _color(value: Any, field: str) -> str:
    color = str(value or "").strip()
    if not _HEX.match(color):
        raise ProfileError(f"{field}必须是 #RRGGBB 格式")
    return color.lower()


def normalize(raw: Any) -> dict[str, Any]:
    """合并默认值并逐项校验；不合法直接抛 ProfileError，避免脏配置写库。"""
    if not isinstance(raw, dict):
        raise ProfileError("配置必须是对象")

    merged = defaults()
    for key, value in raw.items():
        if key in merged:
            merged[key] = value

    avatar_raw = merged.get("avatar")
    if not isinstance(avatar_raw, dict):
        raise ProfileError("avatar 必须是对象")
    avatar = {**DEFAULT_PROFILE["avatar"], **avatar_raw}

    style = str(avatar.get("style") or "").strip().lower()
    if style not in AVATAR_STYLES:
        raise ProfileError(f"形象样式只能是 {' / '.join(AVATAR_STYLES)}")
    try:
        size = int(avatar.get("size") or 0)
    except (TypeError, ValueError) as exc:
        raise ProfileError("形象尺寸必须是数字") from exc
    if not 48 <= size <= 160:
        raise ProfileError("形象尺寸需在 48~160 之间")

    suggestions_raw = merged.get("suggestions")
    if not isinstance(suggestions_raw, list):
        raise ProfileError("suggestions 必须是数组")
    suggestions = [str(item).strip() for item in suggestions_raw if str(item).strip()]
    if len(suggestions) > _MAX_SUGGESTIONS:
        raise ProfileError(f"推荐问题最多 {_MAX_SUGGESTIONS} 条")
    for item in suggestions:
        if len(item) > 60:
            raise ProfileError("单条推荐问题最多 60 个字")

    return {
        "name": _text(merged.get("name"), "名称", 40),
        "tagline": _text(merged.get("tagline"), "一句话介绍", 60),
        "avatar": {
            "style": style,
            "primary": _color(avatar.get("primary"), "形象主色"),
            "accent": _color(avatar.get("accent"), "形象辅色"),
            "size": size,
            "spin": bool(avatar.get("spin", True)),
        },
        "greeting": _text(merged.get("greeting"), "问候语", 200),
        "suggestions": suggestions,
        "theme": _color(merged.get("theme"), "主题色"),
        "show_status_bar": bool(merged.get("show_status_bar", True)),
    }


def load() -> dict[str, Any]:
    """读配置；任何异常都回落默认值（配置坏了不影响对话）。"""
    if not db_available():
        return defaults()
    try:
        with session_scope() as db:
            row = db.get(AgentProfile, PROFILE_ID)
            if row is None or not row.config:
                return defaults()
            return normalize(json.loads(row.config))
    except Exception as exc:  # noqa: BLE001 - 配置是展示层，读失败不该 500
        logger.warning("读取 agent_profile 失败，使用默认配置：%s", exc)
        return defaults()


def save(raw: Any, user_id: int | None) -> dict[str, Any]:
    config = normalize(raw)
    payload = json.dumps(config, ensure_ascii=False)
    with session_scope() as db:
        row = db.get(AgentProfile, PROFILE_ID)
        if row is None:
            db.add(AgentProfile(id=PROFILE_ID, config=payload, updated_by=user_id))
        else:
            row.config = payload
            row.updated_by = user_id
    logger.info("agent_profile 已更新（by=%s）", user_id)
    return config
