"""向量库入口：Elasticsearch（BM25 + kNN 混合检索）。

Qdrant 已下线（2026-10-31）：数据是联调数据、可直接重建，检索质量瓶颈在
"错误码/编号"这类字面匹配，换 ES 后同一个引擎就能同时做 BM25 和向量召回。
这里的函数是业务层唯一入口（ingest / api / tools 只认这几个名字），
以后要换别的后端也只改这个文件。
"""

from __future__ import annotations

from typing import Any

from app.config import settings
from app.kb import es_backend

__all__ = [
    "backend",
    "count_relevant",
    "delete_doc",
    "delete_kb",
    "ensure",
    "health",
    "info",
    "search",
    "search_pool",
    "upsert_chunks",
]


def backend() -> str:
    return (settings.kb_backend or "es").strip().lower()


def ensure(dim: int | None = None) -> None:
    es_backend.ensure(dim)


def upsert_chunks(
    kb_id: str,
    doc_id: str,
    title: str,
    chunks: list[dict[str, Any]],
    vectors: list[list[float]],
) -> None:
    es_backend.upsert_chunks(kb_id, doc_id, title, chunks, vectors)


def delete_doc(doc_id: str) -> None:
    es_backend.delete_doc(doc_id)


def delete_kb(kb_id: str) -> None:
    es_backend.delete_kb(kb_id)


def search(
    vector: list[float],
    kb_ids: list[str] | None,
    top_k: int,
    query_text: str | None = None,
) -> list[dict[str, Any]]:
    return es_backend.search(vector, kb_ids, top_k, query_text)


def search_pool(
    vector: list[float],
    kb_ids: list[str] | None,
    size: int,
    query_text: str | None = None,
) -> list[dict[str, Any]]:
    return es_backend.search_pool(vector, kb_ids, size, query_text)


def count_relevant(
    terms: list[str],
    kb_ids: list[str] | None,
    min_should: int,
) -> int:
    return es_backend.count_relevant(terms, kb_ids, min_should)


def health() -> bool:
    return es_backend.health()


def info() -> dict[str, Any]:
    return es_backend.info()
