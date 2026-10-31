"""向量库无关的小工具：点 ID 与 payload 组装。

Qdrant 和 Elasticsearch 用同一套 ID 规则，同一份切块数据，
所以换后端时 ingest / 审核 / 重建索引的代码一行都不用改。
"""

from __future__ import annotations

import uuid
from typing import Any


def point_id(chunk_id: str) -> str:
    """切块 ID → 稳定的向量库文档 ID（重复入库幂等）。"""
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"fm-kb/{chunk_id}"))


def chunk_payload(kb_id: str, doc_id: str, title: str, chunk: dict[str, Any]) -> dict[str, Any]:
    """一条切块在向量库里携带的元数据（两个后端共用）。"""
    return {
        "kb_id": kb_id,
        "doc_id": doc_id,
        "title": title,
        "seq": chunk["seq"],
        "text": chunk["text"],
    }
