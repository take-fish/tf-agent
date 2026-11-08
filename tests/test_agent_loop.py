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


async def test_invalid_tool_args_are_marked_unexecuted_and_get_correction(monkeypatch):
    """参数非法时工具没执行，模型必须被告知并得到一次纠错提示。"""
    seen_messages: list[list[dict]] = []

    async def probe():
        return ToolResult(text="不应执行")

    registry.register(_spec("probe", probe))

    async def fake_round(messages, tools, **kwargs):
        seen_messages.append([dict(message) for message in messages])
        if tools:
            yield ("tool_calls", [{"id": "c1", "name": "probe", "arguments": '{"a": "b": 1}'}])
        else:
            yield ("delta", "这次没有查到")

    try:
        monkeypatch.setattr(loop, "stream_round", fake_round)
        monkeypatch.setattr(loop.settings, "max_tool_rounds", 2)
        events = await _run(user_text="tf-game 最近半小时有报错吗？")
    finally:
        registry._REGISTRY.pop("probe", None)

    error_events = [event for event in events if event["type"] == "tool" and event["status"] == "error"]
    assert error_events and "本次调用未执行" in error_events[0]["message"]

    # 第二轮请求里必须带上纠错提示，要求重新调用或如实说没查到
    corrections = [message for message in seen_messages[1] if "【系统提示】" in str(message.get("content"))]
    assert corrections and "重新调用" in corrections[0]["content"]

    # 收尾轮前必须有证据核查，明确禁止编造
    guards = [message for message in seen_messages[-1] if "【最终回答依据核查】" in str(message.get("content"))]
    assert guards and "不得编造日志行" in guards[0]["content"]

    assert [event for event in events if event["type"] == "done"][-1]["text"] == "这次没有查到"


async def test_failed_tool_result_feeds_back_no_data(monkeypatch):
    """工具返回 ok=False（例如 Loki 不可达）时，回喂文本要明确“没有取得数据”。"""
    seen_messages: list[list[dict]] = []

    async def probe():
        return ToolResult.error("Loki 不可达")

    registry.register(_spec("probe", probe))

    async def fake_round(messages, tools, **kwargs):
        seen_messages.append([dict(message) for message in messages])
        if tools:
            yield ("tool_calls", [{"id": "c1", "name": "probe", "arguments": "{}"}])
        else:
            yield ("delta", "这次没有查到")

    try:
        monkeypatch.setattr(loop, "stream_round", fake_round)
        monkeypatch.setattr(loop.settings, "max_tool_rounds", 1)
        await _run(user_text="查一下 tf-game 最近的报错")
    finally:
        registry._REGISTRY.pop("probe", None)

    tool_messages = [message for message in seen_messages[-1] if message.get("role") == "tool"]
    assert tool_messages and "本次没有取得数据" in tool_messages[0]["content"]
    guards = [message for message in seen_messages[-1] if "【最终回答依据核查】" in str(message.get("content"))]
    assert guards and "Loki 不可达" in guards[0]["content"]


def test_evidence_guard_skips_plain_kb_question():
    assert loop._evidence_guard([], set(), "发布流程是什么？") == ""


def test_evidence_guard_flags_realtime_question_without_tools():
    text = loop._evidence_guard([], set(), "tf-game 最近半小时有报错吗？")
    assert "这次没有查到" in text
    assert "没有调用任何实时工具" in text
