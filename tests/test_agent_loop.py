"""AgentLoop 行为：最后一轮收尾、只读工具并行、读写同轮、模型重试。"""

from __future__ import annotations

import asyncio
import time

import pytest

from app import llm
from app.agent import confirm_store, loop
from app.auth import Identity
from app.tools import registry
from app.tools.base import ToolResult, ToolSpec, obj_schema


def _identity() -> Identity:
    return Identity(
        user_id=1,
        username="tester",
        role="ADMIN",
        roles=("ADMIN",),
        permissions=("agent.chat.use",),
    )


def _spec(name: str, handler, read_only: bool = True) -> ToolSpec:
    return ToolSpec(
        name=name,
        description=name,
        parameters=obj_schema({}, []),
        handler=handler,
        read_only=read_only,
        timeout=2.0,
    )


@pytest.fixture(autouse=True)
def _isolate(monkeypatch):
    # 不碰 DB：工具配置返回空，审计与落库改成 no-op
    monkeypatch.setattr(registry, "load_configs", lambda force=False: {})
    monkeypatch.setattr(loop.nacos, "limit", lambda name, default: default)
    monkeypatch.setattr(loop.nacos, "write_operation", lambda name, default: default)

    async def _noop_record(**kwargs):
        return None

    async def _noop_llm(**kwargs):
        return None

    async def _noop_save(*args, **kwargs):
        return None

    monkeypatch.setattr(loop.audit, "record", _noop_record)
    monkeypatch.setattr(loop.audit, "record_llm", _noop_llm)
    monkeypatch.setattr(loop, "_save_session", _noop_save)
    yield


async def _run(**overrides):
    params = {
        "identity": _identity(),
        "scene_name": "",
        "env": "prod",
        "user_text": "查一下 tf-game 最近的报错",
        "history": [],
        "session_id": "s1",
        "trace_id": "t1",
    }
    params.update(overrides)
    return [event async for event in loop.stream_agent(**params)]


async def test_last_round_forces_answer_instead_of_dropping_it(monkeypatch):
    seen: list[object] = []

    async def fake_round(messages, tools, **kwargs):
        seen.append(tools)
        if tools:
            yield ("tool_calls", [{"id": "", "name": "probe", "arguments": "{}"}])
        else:
            yield ("delta", "最终总结")

    async def probe():
        return ToolResult(text="工具结果")

    registry.register(_spec("probe", probe))
    try:
        monkeypatch.setattr(loop, "stream_round", fake_round)
        monkeypatch.setattr(loop.settings, "max_tool_rounds", 1)
        events = await _run()
    finally:
        registry._REGISTRY.pop("probe", None)

    # 第一轮给了 tools，收尾轮没有给
    assert seen[0] is not None
    assert seen[-1] is None
    done = [event for event in events if event["type"] == "done"][-1]
    assert done["text"] == "最终总结"
    assert [event["name"] for event in events if event["type"] == "tool" and event["status"] == "done"] == ["probe"]


async def test_read_only_tools_run_in_parallel(monkeypatch):
    barrier = asyncio.Barrier(2)
    started: list[float] = []

    async def slow():
        started.append(time.monotonic())
        async with asyncio.timeout(1.0):
            await barrier.wait()
        return ToolResult(text="ok")

    registry.register(_spec("slow_a", slow))
    registry.register(_spec("slow_b", slow))

    async def fake_round(messages, tools, **kwargs):
        if tools:
            yield (
                "tool_calls",
                [
                    {"id": "c1", "name": "slow_a", "arguments": "{}"},
                    {"id": "c2", "name": "slow_b", "arguments": "{}"},
                ],
            )
        else:
            yield ("delta", "done")

    try:
        monkeypatch.setattr(loop, "stream_round", fake_round)
        monkeypatch.setattr(loop.settings, "max_tool_rounds", 1)
        events = await _run()
    finally:
        registry._REGISTRY.pop("slow_a", None)
        registry._REGISTRY.pop("slow_b", None)

    # 两个 handler 都进了 barrier，说明是并发执行；串行时第一个会卡到超时
    assert len(started) == 2
    assert max(started) - min(started) < 0.5
    assert len([event for event in events if event["type"] == "tool" and event["status"] == "done"]) == 2


async def test_read_and_write_in_same_round_runs_read_first(monkeypatch):
    executed: list[str] = []

    async def read_tool():
        executed.append("read")
        return ToolResult(text="日志查到了")

    async def write_tool(env: str = "prod"):
        executed.append("write")
        return ToolResult(text="已重启")

    registry.register(_spec("probe_read", read_tool))
    registry.register(_spec("probe_write", write_tool, read_only=False))

    async def fake_round(messages, tools, **kwargs):
        if tools:
            yield (
                "tool_calls",
                [
                    {"id": "r1", "name": "probe_read", "arguments": "{}"},
                    {"id": "w1", "name": "probe_write", "arguments": '{"env": "prod"}'},
                ],
            )
        else:  # pragma: no cover - 确认路径不会走到下一轮
            yield ("delta", "不会到这里")

    try:
        monkeypatch.setattr(loop, "stream_round", fake_round)
        events = await _run()
    finally:
        registry._REGISTRY.pop("probe_read", None)
        registry._REGISTRY.pop("probe_write", None)

    assert executed == ["read"]
    confirm = [event for event in events if event["type"] == "confirm_required"]
    assert len(confirm) == 1
    assert confirm[0]["tool"] == "probe_write"
    action = confirm_store.peek(confirm[0]["confirm_id"])
    assert action is not None and action.tool == "probe_write"
    assert [event for event in events if event["type"] == "done"][-1]["pending_confirm"] is True


async def test_stream_round_retries_before_first_delta(monkeypatch):
    attempts = {"n": 0}

    async def fake_once(payload):
        attempts["n"] += 1
        if attempts["n"] == 1:
            raise llm.LLMError("模型连接失败: boom")
        yield ("delta", "hi")
        yield ("end", "hi")

    monkeypatch.setattr(llm, "_stream_once", fake_once)
    events = [event async for event in llm.stream_round([], None)]

    assert ("delta", "hi") in events
    assert attempts["n"] == 2


async def test_stream_round_does_not_retry_after_delta(monkeypatch):
    async def fake_once(payload):
        yield ("delta", "x")
        raise llm.LLMError("late failure")

    monkeypatch.setattr(llm, "_stream_once", fake_once)
    events: list[tuple[str, object]] = []
    with pytest.raises(llm.LLMError):
        async for event in llm.stream_round([], None):
            events.append(event)

    assert events == [("delta", "x")]


async def test_stream_round_does_not_retry_on_4xx(monkeypatch):
    attempts = {"n": 0}

    async def fake_once(payload):
        attempts["n"] += 1
        raise llm.LLMError("模型返回 400: context too long")
        yield ("delta", "unreachable")

    monkeypatch.setattr(llm, "_stream_once", fake_once)
    with pytest.raises(llm.LLMError):
        async for _ in llm.stream_round([], None):
            pass

    assert attempts["n"] == 1
