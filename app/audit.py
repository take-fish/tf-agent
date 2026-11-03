"""审计：工具调用一行一条，只记参数摘要/行数/耗时，不记返回正文。"""

from __future__ import annotations

import json
import logging
from typing import Any

import anyio.to_thread

from app.models import AgentToolCall

logger = logging.getLogger(__name__)

DIGEST_VALUE_CHARS = 80


def digest_args(arguments: dict[str, Any]) -> str:
    safe: dict[str, Any] = {}
    for key, value in (arguments or {}).items():
        if isinstance(value, str):
            safe[key] = value[:DIGEST_VALUE_CHARS]
        elif isinstance(value, list):
            safe[key] = [str(v)[:DIGEST_VALUE_CHARS] for v in value[:5]]
        elif isinstance(value, dict):
            safe[key] = {k: str(v)[:DIGEST_VALUE_CHARS] for k, v in list(value.items())[:8]}
        else:
            safe[key] = str(value)[:DIGEST_VALUE_CHARS]
    return json.dumps(safe, ensure_ascii=False)[:500]


def _write(**kwargs: Any) -> None:
    try:
        from app.db import db_available, session_scope

        if not db_available():
            return
        with session_scope() as db:
            db.add(AgentToolCall(**kwargs))
    except Exception as exc:  # noqa: BLE001 - 审计失败不影响主流程
        logger.warning("审计写入失败：%s", exc)


async def record(
    *,
    trace_id: str,
    user_id: int,
    scene: str,
    env: str,
    tool: str,
    arguments: dict[str, Any],
    ok: bool,
    rows: int | None,
    ms: int,
    need_confirm: bool = False,
    confirmed_by: int | None = None,
) -> None:
    await anyio.to_thread.run_sync(
        lambda: _write(
            trace_id=trace_id[:32],
            user_id=user_id,
            scene=scene[:32],
            env=env[:8],
            tool=tool[:64],
            args_digest=digest_args(arguments),
            ok=1 if ok else 0,
            rows_affected=rows,
            ms=ms,
            need_confirm=1 if need_confirm else 0,
            confirmed_by=confirmed_by,
        )
    )


def _write_llm(**kwargs: Any) -> None:
    try:
        from app.db import db_available, session_scope
        from app.models import AgentLlmCall

        if not db_available():
            return
        with session_scope() as db:
            db.add(AgentLlmCall(**kwargs))
    except Exception as exc:  # noqa: BLE001 - 指标写失败不影响对话
        logger.warning("LLM 流水写入失败：%s", exc)


async def record_llm(
    *,
    trace_id: str,
    user_id: int,
    scene: str,
    round_index: int,
    model: str,
    prompt_tokens: int,
    completion_tokens: int,
    ms: int,
) -> None:
    """一次模型轮次的 token / 耗时流水。"""
    await anyio.to_thread.run_sync(
        lambda: _write_llm(
            trace_id=trace_id[:32],
            user_id=user_id,
            scene=scene[:32],
            round=round_index,
            model=model[:64],
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            ms=ms,
        )
    )
