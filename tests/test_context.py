"""上下文预算与知识库切块的单元测试。"""

from __future__ import annotations

import json

from app.agent import context
from app.kb.ingest import chunk_text


def test_trim_history_keeps_last_n():
    history = [{"role": "user", "content": f"m{i}"} for i in range(20)]
    trimmed = context.trim_history(history, 10)
    assert len(trimmed) == 10
    assert trimmed[0]["content"] == "m10"
    assert trimmed[-1]["content"] == "m19"


def test_trim_history_drops_empty():
    history = [{"role": "user", "content": ""}, {"role": "assistant", "content": "ok"}]
    assert context.trim_history(history, 10) == [{"role": "assistant", "content": "ok"}]


def test_cap_text():
    assert context.cap_text("abc", 10) == "abc"
    capped = context.cap_text("x" * 100, 10)
    assert capped.startswith("x" * 10)
    assert "已截断" in capped


def test_tool_result_budget():
    messages = context.tool_result_messages([(f"call{i}", "y" * 5000) for i in range(4)])
    total = sum(len(m["content"]) for m in messages)
    assert total <= context.ROUND_TOOL_CHARS + 4 * 40


def test_cap_text_by_lines_keeps_whole_lines_and_reports_rest():
    text = "\n".join(f"line{i}" for i in range(20))
    capped = context.cap_text_by_lines(text, 40)
    assert "还有" in capped
    assert capped.splitlines()[0] == "line0"
    assert len(capped) < len(text)


def test_cap_text_by_lines_hard_cuts_single_long_line():
    capped = context.cap_text_by_lines("x" * 1000, 50)
    assert len(capped) < 120
    assert "已截断" in capped


def test_fit_messages_drops_oldest_and_keeps_last_question():
    messages = [{"role": "system", "content": "s" * 100}]
    for _ in range(10):
        messages.append({"role": "user", "content": "u" * 300})
        messages.append({"role": "assistant", "content": "a" * 300})
    messages.append({"role": "user", "content": "q" * 300})

    context.fit_messages(messages, max_context_tokens=300)

    assert messages[0]["role"] == "system"
    assert messages[-1] == {"role": "user", "content": "q" * 300}
    assert len(messages) < 22


def test_fit_messages_never_leaves_orphan_tool_messages():
    messages = [
        {"role": "system", "content": "s"},
        {"role": "user", "content": "u"},
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [{"id": "c1", "type": "function", "function": {"name": "x", "arguments": "{}"}}],
        },
        {"role": "tool", "tool_call_id": "c1", "content": "t" * 500},
        {"role": "assistant", "content": "a" * 500},
        {"role": "user", "content": "q" * 500},
    ]

    context.fit_messages(messages, max_context_tokens=100)

    tool_ids = {m["tool_call_id"] for m in messages if m.get("role") == "tool"}
    call_ids = {c["id"] for m in messages if m.get("tool_calls") for c in m["tool_calls"]}
    assert tool_ids <= call_ids


def test_tool_call_message_normalizes_invalid_arguments():
    bad = '{"trace_id": "service": "tf-game", "keyword": "ERROR", "limit": 20}'
    message = context.tool_call_message(
        [
            {"id": "c1", "name": "search_logs", "arguments": bad},
            {"id": "c2", "name": "search_logs", "arguments": '{"service":"tf-game","limit":20}'},
            {"id": "c3", "name": "search_logs", "arguments": ""},
            {"id": "c4", "name": "search_logs", "arguments": "[1,2]"},
        ]
    )

    args = [call["function"]["arguments"] for call in message["tool_calls"]]
    assert args == ["{}", '{"service":"tf-game","limit":20}', "{}", "{}"]
    # 回灌给模型的历史必须是可解析的 JSON，否则 vLLM 会 400
    assert all(isinstance(json.loads(arg), dict) for arg in args)


def test_normalize_arguments_keeps_chinese_readable():
    assert context.normalize_arguments('{"query": "五子棋 赛事"}') == '{"query":"五子棋 赛事"}'


def test_trim_history_caps_single_message():
    history = [{"role": "user", "content": "x" * 5000}]
    trimmed = context.trim_history(history, 10)
    assert len(trimmed[0]["content"]) <= context.HISTORY_MESSAGE_MAX_CHARS + 40


def test_chunk_text_splits_paragraphs():
    text = "\n\n".join(["段落一" * 40, "段落二" * 40, "段落三" * 40])
    chunks = chunk_text(text, size=200, overlap=40)
    assert len(chunks) >= 2
    assert all(len(c) <= 220 for c in chunks)


def test_chunk_text_long_paragraph_with_overlap():
    text = "甲" * 1000
    chunks = chunk_text(text, size=300, overlap=50)
    assert len(chunks) >= 4
    assert chunks[0][-50:] == chunks[1][:50]


def test_chunk_text_empty():
    assert chunk_text("   \n\n  ") == []


def test_chunk_text_keeps_parent_heading_with_child_sections():
    text = (
        "## 2. 游戏清单\n\n### 2.1 五子棋\n\n五子棋对局与观战。\n\n"
        "### 2.2 盲棋\n\n盲棋隐藏棋子。\n\n### 2.3 竞技场\n\n竞技场实时对战。"
    )
    chunks = chunk_text(text, size=400, overlap=50)
    assert len(chunks) == 1
    assert "## 2. 游戏清单" in chunks[0]
    assert "### 2.1 五子棋" in chunks[0]
    assert "盲棋隐藏棋子" in chunks[0]


def test_chunk_text_splits_on_sibling_headings():
    text = "# 一\n\n内容一\n\n# 二\n\n内容二\n\n## 二点一\n\n子内容"
    chunks = chunk_text(text, size=400, overlap=50)
    assert len(chunks) == 3
    assert chunks[0] == "# 一\n\n内容一"
    assert chunks[1] == "# 二\n\n内容二"
    assert chunks[2].startswith("## 二点一")


def test_chunk_text_merges_heading_only_document_title():
    text = "# 06 游戏\n\n## 1. 概览\n\n总览内容。\n\n## 2. 清单\n\n### 2.1 五子棋\n\n对局。"
    chunks = chunk_text(text, size=400, overlap=50)
    assert len(chunks) == 2
    assert chunks[0].startswith("# 06 游戏")
    assert "## 1. 概览" in chunks[0]
    assert "## 2. 清单" in chunks[1]
    assert "### 2.1 五子棋" in chunks[1]
