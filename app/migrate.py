"""审计库表结构迁移。

用法（用 fm_agent_migrate 账号，不要用 fm_app）：

    FM_AGENT_MIGRATE_DSN='mysql+pymysql://fm_agent_migrate:***@10.28.13.112:13306/fm_agent' \
        python -m app.migrate

按 `migrations/V*.sql` 的文件名顺序应用，已应用的版本记录在 `schema_version`。
业务容器只用 `fm_agent_app`（仅 DML），因此建表必须在这里完成，
`app/db.py` 启动时只校验表是否齐全，缺表会打 ERROR 并降级。
"""

from __future__ import annotations

import argparse
import logging
import os
import re
import sys
from pathlib import Path

from sqlalchemy import create_engine, text

logger = logging.getLogger(__name__)

MIGRATION_DIR = Path(__file__).resolve().parent.parent / "migrations"
VERSION_RE = re.compile(r"^V(\d+)__(.+)\.sql$")


def split_statements(sql: str) -> list[str]:
    """把 SQL 文件切成可逐条执行的语句。

    只处理本项目会用到的语法：`--` 行注释、`/* */` 块注释、单双引号与反引号内的分号。
    不处理存储过程（不需要，DDL 保持简单才能被 Flyway/手工执行兼容）。
    """
    statements: list[str] = []
    buffer: list[str] = []
    quote: str | None = None
    in_block_comment = False

    i = 0
    length = len(sql)
    while i < length:
        char = sql[i]
        nxt = sql[i + 1] if i + 1 < length else ""

        if in_block_comment:
            if char == "*" and nxt == "/":
                in_block_comment = False
                i += 2
                continue
            i += 1
            continue

        if quote:
            buffer.append(char)
            if char == quote:
                # 处理 '' / "" 转义
                if nxt == quote:
                    buffer.append(nxt)
                    i += 2
                    continue
                quote = None
            i += 1
            continue

        if char == "-" and nxt == "-":
            while i < length and sql[i] != "\n":
                i += 1
            continue

        if char == "/" and nxt == "*":
            in_block_comment = True
            i += 2
            continue

        if char in ("'", '"', "`"):
            quote = char
            buffer.append(char)
            i += 1
            continue

        if char == ";":
            statements.append("".join(buffer).strip())
            buffer = []
            i += 1
            continue

        buffer.append(char)
        i += 1

    tail = "".join(buffer).strip()
    if tail:
        statements.append(tail)
    return [statement for statement in statements if statement]


def discover(directory: Path = MIGRATION_DIR) -> list[tuple[str, Path]]:
    """返回按版本号升序排列的 (文件名, 路径)。"""
    items: list[tuple[int, str, Path]] = []
    for path in directory.glob("V*.sql"):
        match = VERSION_RE.match(path.name)
        if not match:
            logger.warning("忽略不符合命名规范的迁移文件：%s", path.name)
            continue
        items.append((int(match.group(1)), path.name, path))
    items.sort()
    return [(name, path) for _, name, path in items]


def apply(dsn: str, directory: Path = MIGRATION_DIR) -> list[str]:
    """应用未执行过的迁移，返回本次实际应用的版本文件名。"""
    engine = create_engine(dsn, future=True)
    applied: list[str] = []
    with engine.begin() as conn:
        conn.execute(
            text(
                "CREATE TABLE IF NOT EXISTS `schema_version` ("
                "  `version` VARCHAR(64) NOT NULL,"
                "  `applied_at` DATETIME(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3),"
                "  PRIMARY KEY (`version`)"
                ") ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci"
            )
        )
        done = {row[0] for row in conn.execute(text("SELECT `version` FROM `schema_version`"))}
        for name, path in discover(directory):
            if name in done:
                logger.info("跳过已应用：%s", name)
                continue
            statements = split_statements(path.read_text(encoding="utf-8"))
            logger.info("应用 %s（%d 条语句）", name, len(statements))
            for statement in statements:
                conn.execute(text(statement))
            conn.execute(text("INSERT INTO `schema_version` (`version`) VALUES (:v)"), {"v": name})
            applied.append(name)
    engine.dispose()
    return applied


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    parser = argparse.ArgumentParser(description="应用 fm_agent 表结构迁移")
    parser.add_argument(
        "--dsn",
        default=os.getenv("FM_AGENT_MIGRATE_DSN") or os.getenv("FM_AGENT_MYSQL_DSN", ""),
        help="迁移库 DSN（默认取 FM_AGENT_MIGRATE_DSN，其次 FM_AGENT_MYSQL_DSN）",
    )
    parser.add_argument("--dry-run", action="store_true", help="只列出待应用的文件")
    args = parser.parse_args(argv)

    if not args.dsn:
        print("未提供 DSN：请设置 FM_AGENT_MIGRATE_DSN", file=sys.stderr)
        return 2

    if args.dry_run:
        for name, _ in discover():
            print(name)
        return 0

    applied = apply(args.dsn)
    if applied:
        print("已应用：" + ", ".join(applied))
    else:
        print("没有需要应用的迁移")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
