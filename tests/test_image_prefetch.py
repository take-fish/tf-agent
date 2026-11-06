"""图片预识别：OCR 文本注入 system、审计不落原文、失败降级。"""

from __future__ import annotations

import pytest

from app import ocr
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


@pytest.fixture(autouse=True)
def _isolate(monkeypatch):
    monkeypatch.setattr(registry, "load_configs", lambda force=False: {})
    monkeypatch.setattr(loop.nacos, "limit", lambda name, default: default)
    monkeypatch.setattr(loop.settings, "max_tool_rounds", 1)
    monkeypatch.setattr(loop.settings, "request_timeout", 10)
    monkeypatch.setattr(loop, "_should_prefetch_kb", lambda text: False)

    async def _noop(*args, **kwargs):
        return None

    monkeypatch.setattr(loop.audit, "record", _noop)
    monkeypatch.setattr(loop.audit, "record_llm", _noop)
    monkeypatch.setattr(loop, "_save_session", _noop)
    yield


def _payload() -> ocr.ImagePayload:
    return ocr.ImagePayload(mime="image/png", data="aGVsbG8=")


async def _run(**overrides):
    params = {
        "identity": _identity(),
        "scene_name": "",
        "env": "prod",
        "user_text": "这个报错是什么原因",
        "history": [],
        "session_id": "s1",
        "trace_id": "t1",
        "image": _payload(),
    }
    params.update(overrides)
    return [event async for event in loop.stream_agent(**params)]


async def test_ocr_text_is_injected_and_audited_without_content(monkeypatch):
    recorded: list[dict] = []

    async def fake_recognize(payload):
        return ocr.OcrResult(
            ok=True,
            mime=payload.mime,
            bytes=123,
            sha256="abcdef1234567890",
            text="NullPointerException at ChatService.java:42",
            line_count=1,
            ms=5,
        )

    async def fake_record(**kwargs):
        recorded.append(kwargs)

    monkeypatch.setattr(loop.ocr, "recognize", fake_recognize)
    monkeypatch.setattr(loop.audit, "record", fake_record)

    seen: list[list[dict]] = []

    async def fake_round(messages, tools, **kwargs):
        seen.append(messages)
        yield ("delta", "根据截图中的文字，是空指针。")

    monkeypatch.setattr(loop, "stream_round", fake_round)
    events = await _run()

    system = seen[0][0]["content"]
    assert "【图片文字识别结果】" in system
    assert "NullPointerException" in system
    assert "不可信数据" in system

    tools = [event for event in events if event["type"] == "tool"]
    assert tools[0]["call_id"] == "prefetch_read_image"
    assert tools[0]["name"] == "read_image"
    assert tools[0]["status"] == "done"
    assert tools[0]["rows"] == 1
    assert tools[0]["data"] == {
        "mime": "image/png",
        "bytes": 123,
        "chars": len("NullPointerException at ChatService.java:42"),
    }

    audit_args = recorded[0]["arguments"]
    assert audit_args == {"prefetch": True, "mime": "image/png", "bytes": 123, "sha256": "abcdef123456"}
    assert "NullPointerException" not in str(recorded[0])
    assert recorded[0]["tool"] == "read_image"


async def test_ocr_failure_degrades_without_injecting_content(monkeypatch):
    async def fake_recognize(payload):
        return ocr.OcrResult(
            ok=False,
            mime=payload.mime,
            bytes=0,
            sha256="",
            ms=2,
            error="图片识别超时",
        )

    monkeypatch.setattr(loop.ocr, "recognize", fake_recognize)
    seen: list[list[dict]] = []

    async def fake_round(messages, tools, **kwargs):
        seen.append(messages)
        yield ("delta", "图片没读出来，请粘贴文字。")

    monkeypatch.setattr(loop, "stream_round", fake_round)
    events = await _run()

    system = seen[0][0]["content"]
    assert "图片识别失败" in system
    assert "【图片文字识别结果】" in system

    tool = [event for event in events if event["type"] == "tool"][0]
    assert tool["status"] == "error"
    assert tool["message"] == "图片识别超时"

    done = [event for event in events if event["type"] == "done"][-1]
    assert done["text"] == "图片没读出来，请粘贴文字。"


async def test_ocr_empty_text_still_notes_no_text(monkeypatch):
    async def fake_recognize(payload):
        return ocr.OcrResult(ok=True, mime=payload.mime, bytes=10, sha256="x", text="", line_count=0, ms=1)

    monkeypatch.setattr(loop.ocr, "recognize", fake_recognize)
    seen: list[list[dict]] = []

    async def fake_round(messages, tools, **kwargs):
        seen.append(messages)
        yield ("delta", "没识别到文字")

    monkeypatch.setattr(loop, "stream_round", fake_round)
    await _run()
    assert "没有识别到可读文字" in seen[0][0]["content"]


async def test_no_image_does_not_emit_ocr_tool(monkeypatch):
    seen: list[list[dict]] = []

    async def fake_round(messages, tools, **kwargs):
        seen.append(messages)
        yield ("delta", "ok")

    monkeypatch.setattr(loop, "stream_round", fake_round)
    events = await _run(image=None)

    assert not [event for event in events if event.get("name") == "read_image"]
    assert "（用户上传图片的 OCR 文本" not in seen[0][0]["content"]
    assert "没有识别到可读文字" not in seen[0][0]["content"]


async def test_image_and_kb_notes_coexist(monkeypatch):
    async def fake_recognize(payload):
        return ocr.OcrResult(
            ok=True,
            mime=payload.mime,
            bytes=10,
            sha256="x",
            text="截图里的文字",
            line_count=1,
            ms=1,
        )

    async def kb(query, top_k=8):
        return ToolResult(text="知识库片段", rows=1, data={"chunks": []})

    previous = registry.get("search_kb")
    registry.register(
        ToolSpec(
            name="search_kb",
            description="kb",
            parameters=obj_schema({"query": {"type": "string"}, "top_k": {"type": "integer"}}, ["query"]),
            handler=kb,
            timeout=2.0,
        )
    )
    monkeypatch.setattr(loop, "_should_prefetch_kb", lambda text: True)
    monkeypatch.setattr(loop.ocr, "recognize", fake_recognize)
    seen: list[list[dict]] = []

    async def fake_round(messages, tools, **kwargs):
        seen.append(messages)
        yield ("delta", "ok")

    monkeypatch.setattr(loop, "stream_round", fake_round)
    try:
        await _run()
    finally:
        if previous is None:
            registry._REGISTRY.pop("search_kb", None)
        else:
            registry.register(previous)

    system = seen[0][0]["content"]
    assert "【图片文字识别结果】" in system
    assert "截图里的文字" in system
    assert "【知识库自动检索结果】" in system
    assert "知识库片段" in system
