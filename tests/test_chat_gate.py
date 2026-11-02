"""入口并发闸门：Nacos 动态上限 + 队列上限。"""

from __future__ import annotations

import pytest
from fastapi import HTTPException

from app.api import chat


async def test_gate_enforces_dynamic_limit_and_queue(monkeypatch):
    values = {"max-concurrency": 1, "queue-limit": 0}
    monkeypatch.setattr(chat.nacos, "concurrency", lambda name, default: values.get(name, default))

    gate = chat._ConcurrencyGate()
    await gate.acquire()
    with pytest.raises(HTTPException):
        await gate.acquire()

    gate.release()
    await gate.acquire()
    gate.release()


async def test_gate_honours_remote_limit(monkeypatch):
    values = {"max-concurrency": 1}
    monkeypatch.setattr(chat.nacos, "concurrency", lambda name, default: values.get(name, default))

    gate = chat._ConcurrencyGate()
    await gate.acquire()
    values["max-concurrency"] = 2  # 热更新后不需要重启
    await gate.acquire()
    gate.release()
    gate.release()
