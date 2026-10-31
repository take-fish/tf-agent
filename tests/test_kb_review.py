"""知识库审核/分类接口的形状测试（不连数据库）。

权限与状态流转的真实校验在 MySQL 上做联调验证；这里守住两件容易被改坏的事：
1. 迁移 V2 能被正确拆分、并且不会把 UPDATE 和 ALTER 粘成一条；
2. 路由表里审核、分类、整库重建这些接口没有在重构中丢掉。
"""

from __future__ import annotations

from pathlib import Path

from app.api import kb
from app.migrate import discover, split_statements
from app.models import AgentKbDoc


def test_v2_migration_splits_cleanly():
    migrations = Path(__file__).resolve().parent.parent / "migrations"
    names = [name for name, _ in discover(migrations)]
    assert "V1__init.sql" in names and "V2__kb_review.sql" in names

    statements = split_statements((migrations / "V2__kb_review.sql").read_text(encoding="utf-8"))
    assert any(s.startswith("CREATE TABLE IF NOT EXISTS `agent_kb_category`") for s in statements)
    assert any("ADD COLUMN `review_status`" in s for s in statements)
    # 存量 ready 文档要补成 approved，否则升级后老文档会被当成"待审核"
    assert statements[-1].startswith("UPDATE `agent_kb_doc` SET `review_status` = 'approved'")


def test_kb_routes_expose_review_and_category_apis():
    paths = {route.path for route in kb.router.routes}
    for expected in (
        "/categories",
        "/categories/{category_id}",
        "/{kb_id}/docs",
        "/{kb_id}/docs/{doc_id}/review",
        "/{kb_id}/docs/{doc_id}/rebuild",
        "/{kb_id}/reindex",
        "/{kb_id}/review-queue",
        "/search",
    ):
        assert expected in paths, f"缺少接口 {expected}"


def test_doc_dict_carries_review_fields():
    row = AgentKbDoc(
        id="doc-1",
        kb_id="kb-1",
        title="手册",
        format="md",
        size_bytes=3,
        status="waiting",
        review_status="pending",
        review_note="",
        chunk_count=0,
        error="",
    )
    data = kb._doc_dict(row)
    assert data["review_status"] == "pending"
    assert data["status"] == "waiting"
    assert data["category_id"] is None
    assert data["reviewed_at"] is None
