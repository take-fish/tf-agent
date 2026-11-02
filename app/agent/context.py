"""上下文预算：max-model-len（默认 8192）是"输入 + 输出"总预算，超了就裁剪，不报错。

裁剪分三层，从便宜到贵：

1. `trim_history`   —— 只保留最近 N 条，并把单条历史压到 2000 字（落库时也压）；
2. `fit_messages`   —— 还有超，按"助手消息 + 它的工具结果"整块丢最旧的，保证 tool_calls
   与 tool 消息成对，不会把 OpenAI 协议拼坏；
3. `cap_text_by_lines` —— 仍超，按行截断最长的工具结果，显式告诉模型"还有 N 行未显示"，
   而不是把 JSON / 表格从中间切开。

8192 这个数字来自 vLLM 的 `--max-model-len`，改之前先改部署。
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)

# 粗略估算：中文约 1.5 字/token，英文约 4 字/token，混合取 1.8
CHARS_PER_TOKEN = 1.8
# 单条工具结果的字符上限（喂回模型的部分）
TOOL_RESULT_MAX_CHARS = 1500
# 一轮里所有工具结果合计上限
ROUND_TOOL_CHARS = 3000
# 单条历史消息落库 / 回放时的字符上限，防止用户贴 2 万字长文把整个窗口顶爆
HISTORY_MESSAGE_MAX_CHARS = 2000


def est_tokens(text: str) -> int:
    return max(1, int(len(text or "") / CHARS_PER_TOKEN))


def message_chars(message: dict[str, Any]) -> int:
    """一条消息占的字符数（正文 + 工具调用的参数）。"""
    total = len(str(message.get("content") or ""))
    for call in message.get("tool_calls") or []:
        total += len(str(call.get("function", {}).get("arguments") or ""))
    return total


def total_chars(messages: list[dict[str, Any]]) -> int:
    return sum(message_chars(message) for message in messages)


def cap_text(text: str, limit: int = TOOL_RESULT_MAX_CHARS) -> str:
    text = text or ""
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n…（已截断，原始 {len(text)} 字）"


def cap_text_by_lines(text: str, limit: int = TOOL_RESULT_MAX_CHARS) -> str:
    """按行截断：宁可少几行，也不要把一行日志 / 一条 JSON 从中间切开。"""
    text = text or ""
    if len(text) <= limit:
        return text
    lines = text.splitlines()
    kept: list[str] = []
    used = 0
    for line in lines:
        if used + len(line) + 1 > limit:
            if not kept:
                # 单行就超了（例如一条超长日志），只能硬截
                kept.append(line[: max(1, limit)])
                used += max(1, limit)
            break
        kept.append(line)
        used += len(line) + 1
    if not kept:
        return text[:limit] + f"\n…（已截断，原始 {len(text)} 字）"
    rest = len(lines) - len(kept)
    note = f"\n…（还有 {rest} 行未显示，原始 {len(text)} 字）" if rest else f"\n…（已截断，原始 {len(text)} 字）"
    return "\n".join(kept) + note


def trim_history(history: list[dict[str, Any]], limit: int) -> list[dict[str, Any]]:
    """只保留最近 limit 条（user/assistant 各算一条），单条压到 2000 字。"""
    if limit <= 0:
        return []
    trimmed = history[-limit:]
    result: list[dict[str, Any]] = []
    for message in trimmed:
        content = message.get("content", "")
        if not content:
            continue
        result.append({"role": message.get("role"), "content": cap_text(str(content), HISTORY_MESSAGE_MAX_CHARS)})
    return result


def fit_messages(
    messages: list[dict[str, Any]],
    max_context_tokens: int,
    reserve_tokens: int = 0,
) -> list[dict[str, Any]]:
    """把 messages 裁到预算内（原地修改并返回）。

    预算 = max_context_tokens - reserve_tokens，单位 token，按 CHARS_PER_TOKEN 折算成字符。
    保护 system 与最后一条消息（当前问题 / 最新工具结果），只从历史里删。
    """
    if max_context_tokens <= 0:
        return messages
    budget = max(1200, int((max_context_tokens - reserve_tokens) * CHARS_PER_TOKEN))
    total = total_chars(messages)

    # 第一遍：从旧到新，整块丢弃（助手 tool_calls 消息 + 紧随其后的 tool 结果）
    index = 1
    while total > budget and index < len(messages) - 1:
        message = messages[index]
        if message.get("role") == "system":
            index += 1
            continue
        end = index + 1
        if message.get("tool_calls"):
            while end < len(messages) and messages[end].get("role") == "tool":
                end += 1
        elif message.get("role") == "tool":
            end = index + 1
        if end >= len(messages):
            break  # 剩下的块包含最后一条，不能动
        for removed in messages[index:end]:
            total -= message_chars(removed)
        del messages[index:end]

    # 第二遍：还超，按行截断最长的工具结果（不删，保留结论）
    if total > budget:
        tool_messages = [m for m in messages[1:] if m.get("role") == "tool"]
        for message in sorted(tool_messages, key=message_chars, reverse=True):
            if total <= budget:
                break
            content = str(message.get("content") or "")
            keep = max(200, len(content) - (total - budget) - 40)
            if keep >= len(content):
                continue
            capped = cap_text_by_lines(content, keep)
            total += len(capped) - len(content)
            message["content"] = capped

    # 第三遍：最后一条（通常是用户输入）仍然超，直接截
    if total > budget and messages:
        last = messages[-1]
        overflow = total - budget
        content = str(last.get("content") or "")
        keep = max(200, len(content) - overflow - 40)
        if keep < len(content):
            last["content"] = cap_text_by_lines(content, keep)

    return messages


def build_round_messages(
    system: str,
    history: list[dict[str, Any]],
    user_text: str,
    max_input_chars: int,
    *,
    max_context_tokens: int = 0,
    reserve_tokens: int = 0,
) -> list[dict[str, Any]]:
    messages: list[dict[str, Any]] = [{"role": "system", "content": system}]
    messages.extend(history)
    messages.append({"role": "user", "content": cap_text(user_text, max_input_chars)})
    return fit_messages(messages, max_context_tokens, reserve_tokens)


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
        chunk = cap_text_by_lines(text, min(TOOL_RESULT_MAX_CHARS, remaining))
        budget -= len(chunk)
        messages.append({"role": "tool", "tool_call_id": call_id, "content": chunk})
    return messages
