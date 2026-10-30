"""上下文预算与知识库切块的单元测试。"""

from __future__ import annotations

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
