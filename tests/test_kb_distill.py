"""会话沉淀：脱敏、指纹、草稿生成边界。"""

from __future__ import annotations

from app.kb import distill


def test_redact_hides_secret_values():
    text = "token: abc123\npassword=hello\napi_key: sk-xxx\n普通内容保留"
    result = distill.redact(text)
    assert "abc123" not in result
    assert "hello" not in result
    assert "sk-xxx" not in result
    assert "token: ***" in result
    assert "password=***" in result
    assert "普通内容保留" in result


def test_content_hash_ignores_line_endings_and_trailing_spaces():
    a = "# 标题\r\n\r\n内容  \r\n"
    b = "# 标题\n\n内容\n"
    assert distill.content_hash(a) == distill.content_hash(b)
    assert distill.content_hash(a) != distill.content_hash(b + "追加")


def test_normalize_adds_missing_title_and_keeps_markdown():
    title, content = distill._normalize("## 现象\n\n报错。", "tf-game 排障")
    assert title == "tf-game 排障"
    assert content.startswith("# tf-game 排障\n\n## 现象")


async def test_create_draft_only_uses_user_and_assistant_messages(monkeypatch):
    monkeypatch.setattr(
        distill,
        "_session_row",
        lambda session_id, user_id: {
            "id": session_id,
            "title": "tf-game 报错",
            "messages": [
                {"role": "user", "content": "tf-game 报错了，token: abc123"},
                {"role": "tool", "content": "工具原始输出不应出现"},
                {"role": "assistant", "content": "先看日志。"},
            ],
        },
    )
    captured: dict = {}

    async def fake_complete(messages, **kwargs):
        captured["messages"] = messages
        assert kwargs["max_tokens"] == 1200
        return "# tf-game 报错\n\n## 现象\n\n服务报错。\n\n## 结论\n\n待确认。"

    monkeypatch.setattr(distill.llm, "complete", fake_complete)
    monkeypatch.setattr(distill, "_pick_kb_id", lambda preferred: "kb-1")

    def fake_save(**kwargs):
        captured["save"] = kwargs
        return {"id": "doc-1", "title": kwargs["title"], "kb_id": kwargs["kb_id"], "duplicate": False}

    monkeypatch.setattr(distill, "_save_draft", fake_save)

    result = await distill.create_draft("s1", user_id=1)

    assert result["id"] == "doc-1"
    prompt = captured["messages"][-1]["content"]
    assert "用户：tf-game 报错了，token: ***" in prompt
    assert "助手：先看日志。" in prompt
    assert "工具原始输出不应出现" not in prompt
    assert captured["save"]["content"].startswith("# tf-game 报错")
    assert "工具原始输出" not in captured["save"]["content"]
