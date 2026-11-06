"""知识库预检索：不依赖模型主动选工具，命中后直接注入 system。"""

from __future__ import annotations

import pytest

from app.agent import loop
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


def _kb_spec(handler) -> ToolSpec:
    return ToolSpec(
        name="search_kb",
        description="kb",
        parameters=obj_schema({"query": {"type": "string"}, "top_k": {"type": "integer"}}, ["query"]),
        handler=handler,
        timeout=2.0,
    )


@pytest.fixture
def kb_tool():
    previous = registry.get("search_kb")
    yield lambda handler: registry.register(_kb_spec(handler))
    if previous is None:
        registry._REGISTRY.pop("search_kb", None)
    else:
        registry.register(previous)


@pytest.fixture(autouse=True)
def _isolate(monkeypatch):
    monkeypatch.setattr(registry, "load_configs", lambda force=False: {})
    monkeypatch.setattr(loop.nacos, "limit", lambda name, default: default)
    monkeypatch.setattr(loop.settings, "max_tool_rounds", 1)
    monkeypatch.setattr(loop.settings, "request_timeout", 10)

    async def _noop(*args, **kwargs):
        return None

    monkeypatch.setattr(loop.audit, "record", _noop)
    monkeypatch.setattr(loop.audit, "record_llm", _noop)
    monkeypatch.setattr(loop, "_save_session", _noop)
    yield


async def _run(**overrides):
    params = {
        "identity": _identity(),
        "scene_name": "",
        "env": "prod",
        "user_text": "有哪些游戏",
        "history": [],
        "session_id": "s1",
        "trace_id": "t1",
    }
    params.update(overrides)
    return [event async for event in loop.stream_agent(**params)]


def test_should_prefetch_skips_greetings():
    assert loop._should_prefetch_kb("有哪些游戏")
    assert loop._should_prefetch_kb("查一下 tf-game 最近的报错")
    assert not loop._should_prefetch_kb("")
    assert not loop._should_prefetch_kb("你好")
    assert not loop._should_prefetch_kb("谢谢你啊")
    assert not loop._should_prefetch_kb("hi")


async def test_question_prefetches_kb_and_injects_context(monkeypatch, kb_tool):
    calls: list[tuple[str, int]] = []
    recorded: list[dict] = []

    async def kb(query: str, top_k: int = 8):
        calls.append((query, top_k))
        return ToolResult(
            text="知识库检索结果：\n[1] 《06-游戏中心-对局与玩法》第3块\n五子棋、盲棋、竞技场、FPS。",
            rows=1,
            data={"chunks": []},
        )

    async def record(**kwargs):
        recorded.append(kwargs)

    kb_tool(kb)
    monkeypatch.setattr(loop.audit, "record", record)

    seen: list[list[dict]] = []

    async def fake_round(messages, tools, **kwargs):
        seen.append(messages)
        yield ("delta", "有五子棋、盲棋、竞技场和 FPS。")

    monkeypatch.setattr(loop, "stream_round", fake_round)
    events = await _run(user_text="有哪些游戏")

    system = seen[0][0]["content"]
    assert "【知识库自动检索结果】" in system
    assert "五子棋" in system
    assert "矛盾的旧结论" in system
    assert calls == [("有哪些游戏", 4)]
    assert recorded[0]["arguments"]["prefetch"] is True
    assert recorded[0]["tool"] == "search_kb"

    tools = [event for event in events if event["type"] == "tool"]
    assert tools[0]["call_id"] == "prefetch_search_kb"
    assert tools[0]["status"] == "done"
    assert tools[0]["rows"] == 1


async def test_prefetch_uses_full_chunk_text_not_truncated_tool_output(monkeypatch, kb_tool):
    """表格/清单块不能按工具输出的 300 字截断，否则后半段游戏丢失。"""
    full_text = "## 2. 游戏清单\n\n| 游戏 ID | 说明 |\n|---|---|\n" + "\n".join(
        f"| `game-{i}` | 第 {i} 个游戏 |" for i in range(12)
    )

    async def kb(query: str, top_k: int = 8):
        return ToolResult(
            text="知识库检索结果：\n[1] 《06-游戏中心-对局与玩法》第4块\n" + full_text[:300],
            rows=1,
            data={"chunks": [{"title": "06-游戏中心-对局与玩法", "seq": 3, "text": full_text}]},
        )

    kb_tool(kb)
    seen: list[list[dict]] = []

    async def fake_round(messages, tools, **kwargs):
        seen.append(messages)
        yield ("delta", "ok")

    monkeypatch.setattr(loop, "stream_round", fake_round)
    await _run(user_text="有哪些游戏")

    system = seen[0][0]["content"]
    assert "`game-11`" in system
    assert "第4块" in system


async def test_no_hits_does_not_inject_context(monkeypatch, kb_tool):
    async def kb(query: str, top_k: int = 8):
        return ToolResult(text="知识库里没有找到相关内容", rows=0)

    kb_tool(kb)
    seen: list[list[dict]] = []

    async def fake_round(messages, tools, **kwargs):
        seen.append(messages)
        yield ("delta", "没查到")

    monkeypatch.setattr(loop, "stream_round", fake_round)
    await _run(user_text="有哪些游戏")

    assert "（系统按用户原问题自动检索" not in seen[0][0]["content"]


async def test_prefetch_failure_does_not_block_answer(monkeypatch, kb_tool):
    async def kb(query: str, top_k: int = 8):
        raise RuntimeError("es down")

    kb_tool(kb)

    async def fake_round(messages, tools, **kwargs):
        yield ("delta", "仍然可以回答")

    monkeypatch.setattr(loop, "stream_round", fake_round)
    events = await _run(user_text="有哪些游戏")

    done = [event for event in events if event["type"] == "done"][-1]
    assert done["text"] == "仍然可以回答"
    assert not [event for event in events if event["type"] == "tool"]
