"""上下文预算：8192 是硬顶，超了就裁剪，不报错。"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)

# 粗略估算：中文约 1.5 字/token，英文约 4 字/token，混合取 1.8
CHARS_PER_TOKEN = 1.8
# 单条工具结果的字符上限（喂回模型的部分）
TOOL_RESULT_MAX_CHARS = 1500
# 一轮里所有工具结果合计上限
ROUND_TOOL_CHARS = 4000


def est_tokens(text: str) -> int:
    return max(1, int(len(text or "") / CHARS_PER_TOKEN))


def cap_text(text: str, limit: int = TOOL_RESULT_MAX_CHARS) -> str:
    text = text or ""
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n…（已截断，原始 {len(text)} 字）"


def trim_history(history: list[dict[str, Any]], limit: int) -> list[dict[str, Any]]:
    """只保留最近 limit 条（user/assistant 各算一条）。"""
    if limit <= 0:
        return []
    trimmed = history[-limit:]
    return [{"role": m.get("role"), "content": m.get("content", "")} for m in trimmed if m.get("content")]


def build_round_messages(
    system: str,
    history: list[dict[str, Any]],
    user_text: str,
    max_input_chars: int,
) -> list[dict[str, Any]]:
    messages: list[dict[str, Any]] = [{"role": "system", "content": system}]
    messages.extend(history)
    messages.append({"role": "user", "content": cap_text(user_text, max_input_chars)})
    return messages


def tool_call_message(calls: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "role": "assistant",
        "content": "",
        "tool_calls": [
            {
                "id": call["id"] or f"call_{idx}",
                "type": "function",
                "function": {"name": call["name"], "arguments": call["arguments"] or "{}"},
            }
            for idx, call in enumerate(calls)
        ],
    }


def tool_result_messages(results: list[tuple[str, str]]) -> list[dict[str, Any]]:
    """results: [(tool_call_id, text)]"""
    budget = ROUND_TOOL_CHARS
    messages: list[dict[str, Any]] = []
    for call_id, text in results:
        remaining = max(300, budget // max(1, len(results)))
        chunk = cap_text(text, min(TOOL_RESULT_MAX_CHARS, remaining))
        budget -= len(chunk)
        messages.append({"role": "tool", "tool_call_id": call_id, "content": chunk})
    return messages


def total_chars(messages: list[dict[str, Any]]) -> int:
    total = 0
    for message in messages:
        total += len(str(message.get("content") or ""))
        for call in message.get("tool_calls") or []:
            total += len(str(call.get("function", {}).get("arguments") or ""))
    return total
