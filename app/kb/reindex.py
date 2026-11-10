"""整库重建知识库索引（V11 章节字段上线后使用）。

用法（在 tf-agent 容器或本机虚拟环境里）：

    python -m app.kb.reindex --dry-run
    python -m app.kb.reindex --kb <kb_id>
    python -m app.kb.reindex --limit 20

只重建 ``review_status=approved`` 的文档；逐篇执行、失败不阻断后面的文档。
先 ``--dry-run`` 确认范围，再在低峰期执行。生产执行前应先给 ES 索引做快照。
"""

from __future__ import annotations

import argparse
import logging
import sys

from app.db import db_available, session_scope
from app.kb import ingest, store
from app.models import AgentKbDoc

logger = logging.getLogger(__name__)


def _load_targets(kb_id: str) -> list[tuple[str, str]]:
    """一次性取回待重建列表，避免长事务占着数据库连接。"""
    with session_scope() as db:
        query = db.query(AgentKbDoc.id, AgentKbDoc.title).filter(AgentKbDoc.review_status == "approved")
        if kb_id:
            query = query.filter(AgentKbDoc.kb_id == kb_id)
        return [(str(doc_id), str(title)) for doc_id, title in query.order_by(AgentKbDoc.id).all()]


def _console_safe() -> None:
    """Windows 控制台默认 GBK，打印中文标题会崩；容器内是 UTF-8，不受影响。"""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")
        except (AttributeError, ValueError):
            pass


def main(argv: list[str] | None = None) -> int:
    _console_safe()
    parser = argparse.ArgumentParser(description="重建知识库索引（只处理审核通过的文档）")
    parser.add_argument("--kb", default="", help="只重建指定知识库 ID")
    parser.add_argument("--limit", type=int, default=0, help="最多重建多少篇（0 表示全部）")
    parser.add_argument("--dry-run", action="store_true", help="只列出待重建文档，不写索引")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    if not db_available():
        print("审计库不可用，无法读取文档列表", file=sys.stderr)
        return 2

    rows = _load_targets(args.kb)
    if args.limit > 0:
        rows = rows[: args.limit]

    print(f"待重建文档 {len(rows)} 篇" + (f"（知识库 {args.kb}）" if args.kb else ""))
    for doc_id, title in rows:
        print(f"  - {doc_id}  {title}")

    # dry-run 只读，不碰索引；真正重建前先补映射，避免缺字段被 ES 拒绝
    if args.dry_run:
        return 0
    store.ensure_fields()

    failed = 0
    for index, (doc_id, title) in enumerate(rows, start=1):
        try:
            count = ingest.ingest_document_sync(doc_id)
            print(f"[{index}/{len(rows)}] {doc_id} 完成，{count} 块")
        except Exception as exc:  # noqa: BLE001 - 单篇失败继续，最后汇总
            failed += 1
            logger.exception("重建失败 %s %s", doc_id, title)
            print(f"[{index}/{len(rows)}] {doc_id} 失败：{exc}", file=sys.stderr)

    print(f"重建结束：成功 {len(rows) - failed} 篇，失败 {failed} 篇")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
