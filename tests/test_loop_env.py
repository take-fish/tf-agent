"""环境解析：环境由用户在对话里说明，工具参数优先于请求默认值。"""

from __future__ import annotations

from app.agent.loop import _effective_env


def test_tool_argument_wins_over_request_default():
    assert _effective_env({"env": "test"}, "prod") == "test"
    assert _effective_env({"env": "prod"}, "test") == "prod"


def test_argument_is_case_insensitive_and_trimmed():
    assert _effective_env({"env": " TEST "}, "prod") == "test"


def test_falls_back_to_default_when_missing_or_invalid():
    assert _effective_env({}, "prod") == "prod"
    assert _effective_env({"env": ""}, "test") == "test"
    assert _effective_env({"env": "staging"}, "prod") == "prod"
    assert _effective_env(None, "prod") == "prod"
