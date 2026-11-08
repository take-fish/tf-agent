from pathlib import Path

from app.api import eval as eval_api
from app.migrate import discover, split_statements


def test_eval_routes_cover_sets_questions_candidates_and_runs():
    paths = {route.path for route in eval_api.router.routes}
    for expected in (
        "/sets",
        "/sets/{set_id}",
        "/sets/{set_id}/questions",
        "/sets/{set_id}/pool",
        "/sets/{set_id}/runs",
        "/questions/{question_id}",
        "/questions/{question_id}/candidates",
        "/questions/{question_id}/pool",
        "/candidates/{candidate_id}",
        "/runs/{run_id}",
    ):
        assert expected in paths, f"缺少接口 {expected}"


def test_v9_migration_creates_eval_tables_and_candidate_unique_key():
    migrations = Path(__file__).resolve().parent.parent / "migrations"
    names = [name for name, _ in discover(migrations)]
    assert "V9__kb_eval.sql" in names

    statements = split_statements((migrations / "V9__kb_eval.sql").read_text(encoding="utf-8"))
    for table in (
        "agent_eval_set",
        "agent_eval_question",
        "agent_eval_candidate",
        "agent_eval_run",
    ):
        assert any(f"CREATE TABLE IF NOT EXISTS {table}" in statement for statement in statements)
    assert any("UNIQUE KEY uq_eval_candidate_chunk" in statement for statement in statements)

    # precision 是 MySQL 8 保留字，手写 DDL 不加反引号会直接 1064
    run_sql = next(statement for statement in statements if "CREATE TABLE IF NOT EXISTS agent_eval_run" in statement)
    assert "`precision` DOUBLE" in run_sql
