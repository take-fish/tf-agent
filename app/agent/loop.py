"""AgentLoop：模型 ⇄ 工具，最多 N 轮；写操作在此处被拦成"待确认"。"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections.abc import AsyncIterator
from typing import Any

import anyio.to_thread

from app import audit, nacos
from app.agent import confirm_store, context, prompt
from app.config import settings
from app.llm import LLMError, stream_round
from app.tools import registry

logger = logging.getLogger(__name__)


def _parse_args(raw: str) -> dict[str, Any]:
    if not raw:
        return {}
    try:
        value = json.loads(raw)
    except json.JSONDecodeError:
        # 模型偶尔给半截 JSON，尽量救一下
        try:
            value = json.loads(raw[: raw.rfind("}") + 1])
        except Exception as exc:  # noqa: BLE001
            raise ValueError(f"工具参数不是合法 JSON：{raw[:120]}") from exc
    if not isinstance(value, dict):
        raise ValueError("工具参数必须是对象")
    return value


def _effective_env(arguments: dict[str, Any], default: str) -> str:
    """工具参数里带了 env 就以参数为准：环境由用户在对话里说明，而不是前端开关。"""
    value = str((arguments or {}).get("env") or "").strip().lower()
    return value if value in {"prod", "test"} else default


async def _execute(spec, arguments: dict[str, Any], env: str) -> tuple[Any, int]:
    """执行工具，返回 (ToolResult, 耗时 ms)。"""
    start = time.perf_counter()
    kwargs = dict(arguments)
    # 写工具统一注入 env
    if "env" in spec.parameters.get("properties", {}) and "env" not in kwargs:
        kwargs["env"] = env
    timeout = registry.timeout_for(spec.name)
    result = await asyncio.wait_for(spec.handler(**kwargs), timeout=timeout)
    return result, int((time.perf_counter() - start) * 1000)


async def stream_agent(
    *,
    identity,
    scene_name: str,
    env: str,
    user_text: str,
    history: list[dict[str, Any]],
    session_id: str,
    trace_id: str,
) -> AsyncIterator[dict[str, Any]]:
    scene = prompt.get_scene(scene_name)
    # 场景没限定工具（auto）时，把用户有权限的工具全部交给模型自己挑
    tools = registry.allowed_tools(identity, scene.tools or None)
    tool_schemas = [spec.schema() for spec in tools] or None
    system = prompt.build_system_prompt(scene, env=env, user_name=identity.display_name)
    messages = context.build_round_messages(
        system,
        context.trim_history(history, nacos.limit("history-limit", settings.history_limit)),
        user_text,
        nacos.limit("max-input-chars", settings.max_input_chars),
    )

    yield {"type": "start", "trace_id": trace_id, "scene": scene.name, "env": env, "tools": [t.name for t in tools]}

    final_text = ""
    usage: dict[str, Any] = {}
    deadline = time.monotonic() + settings.request_timeout
    max_rounds = nacos.limit("max-tool-rounds", settings.max_tool_rounds)

    for round_index in range(max_rounds):
        if time.monotonic() > deadline:
            yield {"type": "error", "message": "本次请求超时，请缩小问题范围后重试"}
            return

        calls: list[dict[str, Any]] | None = None
        try:
            async for kind, payload in stream_round(
                messages,
                tool_schemas,
                temperature=scene.temperature,
                max_tokens=scene.max_tokens,
            ):
                if kind == "delta":
                    final_text += payload
                    yield {"type": "delta", "text": payload}
                elif kind == "usage":
                    usage = payload
                elif kind == "tool_calls":
                    calls = payload
        except LLMError as exc:
            logger.warning("模型调用失败：%s", exc)
            yield {"type": "error", "message": str(exc)}
            return

        if not calls:
            break

        # ---- 写操作：拦下来，转成确认请求 ----
        write_call = None
        for call in calls:
            spec = registry.get(call["name"])
            if spec is not None and not spec.read_only:
                write_call = call
                break
        if write_call is not None:
            spec = registry.get(write_call["name"])
            try:
                arguments = _parse_args(write_call["arguments"])
            except ValueError as exc:
                yield {"type": "error", "message": str(exc)}
                return
            action = confirm_store.create(
                tool=spec.name,
                arguments=arguments,
                user_id=identity.user_id,
                trace_id=trace_id,
                env=_effective_env(arguments, env),
                scene=scene.name,
                description=_describe(spec.name, arguments),
            )
            await audit.record(
                trace_id=trace_id,
                user_id=identity.user_id,
                scene=scene.name,
                env=_effective_env(arguments, env),
                tool=spec.name,
                arguments=arguments,
                ok=True,
                rows=0,
                ms=0,
                need_confirm=True,
            )
            yield {
                "type": "confirm_required",
                "confirm_id": action.confirm_id,
                "tool": spec.name,
                "args": arguments,
                "description": action.description,
                "expires_in": confirm_store.TTL_SECONDS,
            }
            yield {
                "type": "done",
                "text": final_text,
                "usage": usage,
                "session_id": session_id,
                "pending_confirm": True,
            }
            return

        # ---- 只读工具：串行执行 ----
        messages.append(context.tool_call_message(calls))
        results: list[tuple[str, str]] = []
        for call in calls:
            spec = registry.get(call["name"])
            if spec is None:
                results.append((call["id"], f"工具 {call['name']} 不存在"))
                continue
            try:
                arguments = _parse_args(call["arguments"])
            except ValueError as exc:
                results.append((call["id"], str(exc)))
                yield {"type": "tool", "call_id": call["id"], "name": spec.name, "status": "error", "message": str(exc)}
                continue

            yield {"type": "tool", "call_id": call["id"], "name": spec.name, "status": "running", "args": arguments}
            try:
                result, ms = await _execute(spec, arguments, env)
            except TimeoutError:
                result, ms = None, 0
                results.append((call["id"], f"工具 {spec.name} 超时"))
                await audit.record(
                    trace_id=trace_id,
                    user_id=identity.user_id,
                    scene=scene.name,
                    env=_effective_env(arguments, env),
                    tool=spec.name,
                    arguments=arguments,
                    ok=False,
                    rows=0,
                    ms=0,
                )
                yield {
                    "type": "tool",
                    "call_id": call["id"],
                    "name": spec.name,
                    "status": "error",
                    "message": "执行超时",
                }
                continue
            except Exception as exc:  # noqa: BLE001 - 工具异常要回喂模型，不中断对话
                logger.exception("工具 %s 执行失败", spec.name)
                results.append((call["id"], f"工具 {spec.name} 执行失败：{exc}"))
                await audit.record(
                    trace_id=trace_id,
                    user_id=identity.user_id,
                    scene=scene.name,
                    env=_effective_env(arguments, env),
                    tool=spec.name,
                    arguments=arguments,
                    ok=False,
                    rows=0,
                    ms=0,
                )
                yield {"type": "tool", "call_id": call["id"], "name": spec.name, "status": "error", "message": str(exc)}
                continue

            results.append((call["id"], result.text))
            await audit.record(
                trace_id=trace_id,
                user_id=identity.user_id,
                scene=scene.name,
                env=_effective_env(arguments, env),
                tool=spec.name,
                arguments=arguments,
                ok=result.ok,
                rows=result.rows,
                ms=ms,
            )
            yield {
                "type": "tool",
                "call_id": call["id"],
                "name": spec.name,
                "status": "done",
                "ok": result.ok,
                "rows": result.rows,
                "ms": ms,
                "data": result.data,
            }

        messages.extend(context.tool_result_messages(results))
        logger.info("第 %s 轮工具执行完成，共 %s 个", round_index + 1, len(calls))

    await _save_session(session_id, identity.user_id, scene.name, env, history, user_text, final_text)
    yield {"type": "done", "text": final_text, "usage": usage, "session_id": session_id}


def _describe(tool: str, arguments: dict[str, Any]) -> str:
    if tool == "trigger_release":
        return (
            f"触发发布：仓库 {arguments.get('repo')}，分支 {arguments.get('ref', 'master')}，"
            f"环境 {arguments.get('env', 'prod')}"
        )
    if tool == "restart_service":
        return f"重启服务：{arguments.get('service')}，环境 {arguments.get('env', 'prod')}"
    return f"执行 {tool}：{json.dumps(arguments, ensure_ascii=False)[:120]}"


def _session_title(messages: list[dict[str, Any]], limit: int = 60) -> str:
    """会话标题：第一条用户消息，压成一行并截断。"""
    for message in messages:
        if message.get("role") != "user":
            continue
        text = " ".join(str(message.get("content") or "").split())
        if text:
            return text[:limit]
    return "新会话"


def _persist(
    session_id: str,
    user_id: int,
    scene: str,
    env: str,
    history: list[dict[str, Any]],
    user_text: str,
    assistant_text: str,
) -> None:
    """会话落库并裁到最近 N 条。"""
    try:
        from app.db import db_available, session_scope
        from app.models import AgentSession

        if not db_available():
            return
        messages = list(history)
        messages.append({"role": "user", "content": user_text})
        if assistant_text:
            messages.append({"role": "assistant", "content": assistant_text})
        messages = messages[-settings.history_limit :]

        with session_scope() as db:
            row = db.get(AgentSession, session_id)
            if row is None:
                row = AgentSession(id=session_id, user_id=user_id, scene=scene, env=env, messages=messages)
                db.add(row)
            else:
                row.scene = scene
                row.env = env
                row.messages = messages
    except Exception as exc:  # noqa: BLE001
        logger.warning("会话保存失败 %s: %s", session_id, exc)


async def _save_session(
    session_id: str,
    user_id: int,
    scene: str,
    env: str,
    history: list[dict[str, Any]],
    user_text: str,
    assistant_text: str,
) -> None:
    await anyio.to_thread.run_sync(
        lambda: _persist(session_id, user_id, scene, env, history, user_text, assistant_text)
    )


async def execute_confirmed(action: confirm_store.PendingAction, identity) -> AsyncIterator[dict[str, Any]]:
    """执行已被确认的写操作。"""
    spec = registry.get(action.tool)
    if spec is None:
        yield {"type": "error", "message": f"工具 {action.tool} 不存在"}
        return
    yield {"type": "tool", "name": spec.name, "status": "running", "args": action.arguments}
    try:
        result, ms = await _execute(spec, action.arguments, action.env)
    except Exception as exc:  # noqa: BLE001
        await audit.record(
            trace_id=action.trace_id,
            user_id=identity.user_id,
            scene=action.scene,
            env=action.env,
            tool=spec.name,
            arguments=action.arguments,
            ok=False,
            rows=0,
            ms=0,
            need_confirm=True,
            confirmed_by=identity.user_id,
        )
        yield {"type": "tool", "name": spec.name, "status": "error", "message": str(exc)}
        yield {"type": "error", "message": f"执行失败：{exc}"}
        return

    await audit.record(
        trace_id=action.trace_id,
        user_id=identity.user_id,
        scene=action.scene,
        env=action.env,
        tool=spec.name,
        arguments=action.arguments,
        ok=result.ok,
        rows=result.rows,
        ms=ms,
        need_confirm=True,
        confirmed_by=identity.user_id,
    )
    yield {
        "type": "tool",
        "name": spec.name,
        "status": "done",
        "ok": result.ok,
        "rows": result.rows,
        "ms": ms,
        "data": result.data,
    }
    yield {"type": "done", "text": result.text}
