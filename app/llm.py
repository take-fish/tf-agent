"""vLLM(OpenAI 兼容) 客户端。

只做两件事：把消息发给模型、把流式 chunk 装配成"文本增量"或"工具调用"。
编排逻辑不在这里（见 app/agent/loop.py）。
"""

from __future__ import annotations

import json
import logging
from collections.abc import AsyncIterator
from typing import Any

import httpx

from app.config import settings

logger = logging.getLogger(__name__)

_client: httpx.AsyncClient | None = None


class LLMError(RuntimeError):
    """模型侧失败（连接、超时、非 2xx）。"""


def _get_client() -> httpx.AsyncClient:
    global _client
    if _client is None:
        _client = httpx.AsyncClient(
            base_url=settings.llm_base_url.rstrip("/"),
            timeout=httpx.Timeout(
                connect=settings.llm_connect_timeout,
                read=settings.llm_read_timeout,
                write=10.0,
                pool=5.0,
            ),
            headers={"Authorization": f"Bearer {settings.llm_api_key}"} if settings.llm_api_key else {},
        )
    return _client


async def close() -> None:
    global _client
    if _client is not None:
        await _client.aclose()
        _client = None


async def list_models() -> list[str]:
    try:
        resp = await _get_client().get("/models")
        resp.raise_for_status()
        return [item["id"] for item in resp.json().get("data", [])]
    except Exception as exc:  # noqa: BLE001
        raise LLMError(f"模型列表获取失败: {exc}") from exc


async def stream_round(
    messages: list[dict[str, Any]],
    tools: list[dict[str, Any]] | None = None,
    *,
    temperature: float = 0.2,
    max_tokens: int | None = None,
    tool_choice: str = "auto",
) -> AsyncIterator[tuple[str, Any]]:
    """跑一轮模型推理。

    产出事件：
      ("delta", str)          —— 正文增量，直接转发给前端
      ("tool_calls", list)    —— 本轮要执行的工具（已装配完整参数）
      ("usage", dict)         —— token 用量
      ("end", str)            —— 本轮结束，携带完整正文
    """
    payload: dict[str, Any] = {
        "model": settings.llm_model,
        "messages": messages,
        "stream": True,
        "stream_options": {"include_usage": True},
        "temperature": temperature,
        "max_tokens": max_tokens or settings.max_tokens,
    }
    if tools:
        payload["tools"] = tools
        payload["tool_choice"] = tool_choice

    slots: dict[int, dict[str, Any]] = {}
    content_parts: list[str] = []
    finish_reason: str | None = None
    usage: dict[str, Any] = {}

    try:
        async with _get_client().stream("POST", "/chat/completions", json=payload) as resp:
            if resp.status_code >= 400:
                body = (await resp.aread()).decode("utf-8", "replace")[:500]
                raise LLMError(f"模型返回 {resp.status_code}: {body}")
            async for line in resp.aiter_lines():
                if not line.startswith("data: "):
                    continue
                data = line[6:].strip()
                if not data or data == "[DONE]":
                    continue
                try:
                    chunk = json.loads(data)
                except json.JSONDecodeError:
                    continue
                if chunk.get("usage"):
                    usage = chunk["usage"]
                choices = chunk.get("choices") or []
                if not choices:
                    continue
                choice = choices[0]
                delta = choice.get("delta") or {}
                text = delta.get("content")
                if text:
                    content_parts.append(text)
                    yield ("delta", text)
                for item in delta.get("tool_calls") or []:
                    index = item.get("index", 0)
                    slot = slots.setdefault(index, {"id": "", "name": "", "arguments": ""})
                    if item.get("id"):
                        slot["id"] = item["id"]
                    fn = item.get("function") or {}
                    if fn.get("name"):
                        slot["name"] += fn["name"]
                    if fn.get("arguments"):
                        slot["arguments"] += fn["arguments"]
                if choice.get("finish_reason"):
                    finish_reason = choice["finish_reason"]
    except httpx.TimeoutException as exc:
        raise LLMError(f"模型超时: {exc}") from exc
    except httpx.HTTPError as exc:
        raise LLMError(f"模型连接失败: {exc}") from exc

    if usage:
        yield ("usage", usage)
    if slots:
        calls = [slots[i] for i in sorted(slots)]
        logger.info("本轮工具调用 finish=%s calls=%s", finish_reason, [c["name"] for c in calls])
        yield ("tool_calls", calls)
    yield ("end", "".join(content_parts))


async def complete(
    messages: list[dict[str, Any]],
    *,
    temperature: float = 0.2,
    max_tokens: int = 512,
) -> str:
    """非流式补全，给"摘要/改写"这类内部调用用。"""
    payload = {
        "model": settings.llm_model,
        "messages": messages,
        "temperature": temperature,
        "max_tokens": max_tokens,
        "stream": False,
    }
    try:
        resp = await _get_client().post("/chat/completions", json=payload)
        resp.raise_for_status()
        return resp.json()["choices"][0]["message"].get("content") or ""
    except Exception as exc:  # noqa: BLE001
        raise LLMError(f"模型调用失败: {exc}") from exc
