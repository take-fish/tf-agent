"""向量库门面：按 FM_KB_BACKEND 分发到 Qdrant 或 Elasticsearch。

上层（ingest / api / tools）只认这 6 个函数，换后端不动业务代码：
  ensure / upsert_chunks / delete_doc / delete_kb / search / health
"""

from __future__ import annotations

import logging
from typing import Any

from app.config import settings
from app.kb import es_backend, qdrant_backend

logger = logging.getLogger(__name__)


def backend() -> str:
    return (settings.kb_backend or "qdrant").strip().lower()


def _impl():
    return es_backend if backend() == "es" else qdrant_backend


def ensure(dim: int | None = None) -> None:
    _impl().ensure(dim)


def upsert_chunks(
    kb_id: str,
    doc_id: str,
    title: str,
    chunks: list[dict[str, Any]],
    vectors: list[list[float]],
) -> None:
    _impl().upsert_chunks(kb_id, doc_id, title, chunks, vectors)


def delete_doc(doc_id: str) -> None:
    _impl().delete_doc(doc_id)


def delete_kb(kb_id: str) -> None:
    _impl().delete_kb(kb_id)


def search(
    vector: list[float],
    kb_ids: list[str] | None,
    top_k: int,
    query_text: str | None = None,
) -> list[dict[str, Any]]:
    return _impl().search(vector, kb_ids, top_k, query_text)


def health() -> bool:
    return _impl().health()


def info() -> dict[str, Any]:
    return _impl().info()
