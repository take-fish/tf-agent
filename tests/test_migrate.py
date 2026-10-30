"""迁移与审计库降级路径的测试（不连真实数据库）。"""

from __future__ import annotations

import pytest

from app import db
from app.config import settings
from app.migrate import discover, split_statements


def test_split_statements_ignores_comments_and_quotes(tmp_path):
    sql = """
    -- 行注释里的分号;不该切断
    CREATE TABLE `a` (`id` INT, `note` VARCHAR(10) DEFAULT 'x;y');
    /* 块注释 ; 也一样 */
    INSERT INTO `a` VALUES (1, 'b;c');
    """
    statements = split_statements(sql)
    assert len(statements) == 2
    assert statements[0].startswith("CREATE TABLE `a`")
    assert "x;y" in statements[0]
    assert statements[1].startswith("INSERT INTO `a`")


def test_split_statements_without_trailing_semicolon():
    assert split_statements("SELECT 1") == ["SELECT 1"]


def test_discover_orders_by_version(tmp_path):
    (tmp_path / "V2__b.sql").write_text("SELECT 2", encoding="utf-8")
    (tmp_path / "V10__c.sql").write_text("SELECT 10", encoding="utf-8")
    (tmp_path / "V1__a.sql").write_text("SELECT 1", encoding="utf-8")
    (tmp_path / "not-a-migration.sql").write_text("SELECT 0", encoding="utf-8")
    names = [name for name, _ in discover(tmp_path)]
    assert names == ["V1__a.sql", "V2__b.sql", "V10__c.sql"]


def test_init_db_without_dsn_degrades(monkeypatch):
    monkeypatch.setattr(settings, "mysql_dsn", "")
    db.reset_for_tests()
    assert db.init_db() is False
    assert db.db_available() is False
    db.reset_for_tests()


def test_mask_dsn_hides_password():
    masked = db.mask_dsn("mysql+pymysql://fm_agent_app:secret@10.28.13.112:3306/fm_agent?charset=utf8mb4")
    assert "secret" not in masked
    assert "fm_agent_app:***@10.28.13.112:3306" in masked


@pytest.fixture(autouse=True)
def _clean_db_state():
    db.reset_for_tests()
    yield
    db.reset_for_tests()
