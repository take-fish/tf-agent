"""工具安全边界的单元测试：白名单、参数校验、写操作标记。"""

from __future__ import annotations

import pytest

from app.tools import register_all, registry
from app.tools.base import ToolResult, ToolSpec, obj_schema


@pytest.fixture(autouse=True)
def _tools():
    register_all()


def test_all_tools_registered():
    names = {spec.name for spec in registry.all_specs()}
    assert {
        "search_logs",
        "check_health",
        "query_view",
        "query_pipeline",
        "search_kb",
        "trigger_release",
        "restart_service",
    } <= names


def test_write_tools_are_flagged():
    for name in ("trigger_release", "restart_service"):
        assert registry.get(name).read_only is False
    for name in ("search_logs", "check_health", "query_view", "query_pipeline", "search_kb"):
        assert registry.get(name).read_only is True


def test_schema_shape():
    spec = registry.get("search_logs")
    schema = spec.schema()
    assert schema["type"] == "function"
    assert schema["function"]["name"] == "search_logs"
    assert schema["function"]["parameters"]["additionalProperties"] is False
    assert "properties" in schema["function"]["parameters"]


async def test_query_view_rejects_unknown_view(monkeypatch):
    from app.tools import db_query

    monkeypatch.setattr(db_query.settings, "db_dsn", "mysql+pymysql://x/y")
    monkeypatch.setattr(db_query.settings, "db_views", {"game_room_stats": "对局统计"})
    result = await db_query.query_view("mysql.user")
    assert result.ok is False
    assert "白名单" in result.text


async def test_query_view_requires_config(monkeypatch):
    from app.tools import db_query

    monkeypatch.setattr(db_query.settings, "db_dsn", "")
    result = await db_query.query_view("game_room_stats")
    assert result.ok is False


async def test_search_logs_requires_condition(monkeypatch):
    from app.tools import logs

    result = await logs.search_logs()
    assert result.ok is False
    assert "至少需要" in result.text


def test_registry_filters_by_scene(monkeypatch):
    monkeypatch.setattr(registry, "_config_cache", {})
    monkeypatch.setattr(registry, "_config_loaded_at", 1e18)  # 禁用 DB 读取

    class FakeIdentity:
        user_id = 1
        role = "USER"
        roles = ("USER",)
        permissions = ()
        is_admin = False

    specs = registry.allowed_tools(FakeIdentity(), ["search_logs", "search_kb"])
    assert {s.name for s in specs} == {"search_logs", "search_kb"}


def test_requires_config_tool_hidden_until_configured(monkeypatch):
    class FakeIdentity:
        user_id = 1
        role = "ADMIN"
        roles = ("ADMIN",)
        permissions = ()
        is_admin = True

    monkeypatch.setattr(registry, "load_configs", lambda force=False: {})
    assert registry.allowed_tools(FakeIdentity(), ["trigger_release"]) == []

    monkeypatch.setattr(
        registry,
        "load_configs",
        lambda force=False: {
            "trigger_release": {"enabled": True, "timeout_ms": 30000, "roles": [], "params": {}},
        },
    )
    assert [spec.name for spec in registry.allowed_tools(FakeIdentity(), ["trigger_release"])] == ["trigger_release"]


def test_obj_schema_requires():
    schema = obj_schema({"a": {"type": "string"}}, ["a"])
    assert schema["required"] == ["a"]
    assert schema["additionalProperties"] is False


def test_tool_result_error():
    result = ToolResult.error("boom")
    assert result.ok is False
    assert "boom" in result.text
    assert isinstance(ToolSpec, type)
