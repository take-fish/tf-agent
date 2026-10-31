"""向量库后端：Qdrant（一个 collection 存所有知识库，用 payload.kb_id 过滤）。"""

from __future__ import annotations

import logging
import time
from typing import Any

from qdrant_client import QdrantClient
from qdrant_client.models import (
    Distance,
    FieldCondition,
    Filter,
    MatchAny,
    MatchValue,
    PointStruct,
    VectorParams,
)

from app.config import settings
from app.kb.common import chunk_payload, point_id

logger = logging.getLogger(__name__)

_client: QdrantClient | None = None
_ready_at: float = 0.0
# 集合存在性缓存：集合可能被外部删掉（运维重建、误删），
# 缓存久了会把 404 一直带下去，所以只缓存 30 秒，并在失败时立刻失效重试。
_READY_TTL = 30.0


def get_client() -> QdrantClient:
    global _client
    if _client is None:
        _client = QdrantClient(
            url=settings.qdrant_url,
            api_key=settings.qdrant_api_key or None,
            timeout=10.0,
        )
    return _client


def reset_collection_cache() -> None:
    global _ready_at
    _ready_at = 0.0


def ensure_collection(dim: int | None = None) -> None:
    global _ready_at
    if time.monotonic() - _ready_at < _READY_TTL:
        return
    client = get_client()
    name = settings.qdrant_collection
    existing = {c.name for c in client.get_collections().collections}
    if name not in existing:
        client.create_collection(
            collection_name=name,
            vectors_config=VectorParams(size=dim or settings.embed_dim, distance=Distance.COSINE),
        )
        logger.info("创建向量集合 %s (dim=%s)", name, dim or settings.embed_dim)
    _ready_at = time.monotonic()


def ensure(dim: int | None = None) -> None:
    """门面统一入口。"""
    ensure_collection(dim)


def _retry_once(func, *args, **kwargs):
    """集合被外部删掉时，第一次调用会 404；失效缓存、确保集合存在后重试一次。"""
    ensure_collection()
    try:
        return func(*args, **kwargs)
    except Exception:
        reset_collection_cache()
        ensure_collection()
        return func(*args, **kwargs)


def upsert_chunks(
    kb_id: str,
    doc_id: str,
    title: str,
    chunks: list[dict[str, Any]],
    vectors: list[list[float]],
) -> None:
    points = [
        PointStruct(
            id=point_id(chunk["id"]),
            vector=vector,
            payload=chunk_payload(kb_id, doc_id, title, chunk),
        )
        for chunk, vector in zip(chunks, vectors, strict=False)
    ]
    if not points:
        return
    _retry_once(lambda: get_client().upsert(collection_name=settings.qdrant_collection, points=points, wait=True))


def delete_doc(doc_id: str) -> None:
    _delete(Filter(must=[FieldCondition(key="doc_id", match=MatchValue(value=doc_id))]))


def delete_kb(kb_id: str) -> None:
    _delete(Filter(must=[FieldCondition(key="kb_id", match=MatchValue(value=kb_id))]))


def _delete(points_filter: Filter) -> None:
    """集合本身不存在时删除是无意义的，按成功处理（重启后重建集合即可）。"""
    try:
        _retry_once(
            lambda: get_client().delete(
                collection_name=settings.qdrant_collection,
                points_selector=points_filter,
                wait=True,
            )
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("删除向量失败（忽略）：%s", exc)


def search(
    vector: list[float],
    kb_ids: list[str] | None,
    top_k: int,
    query_text: str | None = None,  # noqa: ARG001 - Qdrant 没有 BM25，保留签名以便换后端
) -> list[dict[str, Any]]:
    must = []
    if kb_ids:
        must.append(FieldCondition(key="kb_id", match=MatchAny(any=kb_ids)))
    query_filter = Filter(must=must) if must else None

    hits = _retry_once(
        lambda: (
            get_client()
            .query_points(
                collection_name=settings.qdrant_collection,
                query=vector,
                query_filter=query_filter,
                limit=top_k,
                with_payload=True,
            )
            .points
        )
    )
    return [
        {
            "doc_id": hit.payload.get("doc_id", ""),
            "kb_id": hit.payload.get("kb_id", ""),
            "title": hit.payload.get("title", ""),
            "seq": hit.payload.get("seq", 0),
            "text": hit.payload.get("text", ""),
            "score": float(hit.score),
        }
        for hit in hits
    ]


def health() -> bool:
    try:
        get_client().get_collections()
        return True
    except Exception as exc:  # noqa: BLE001
        logger.debug("Qdrant 不可用：%s", exc)
        return False


def info() -> dict[str, Any]:
    return {"backend": "qdrant", "endpoint": settings.qdrant_url, "index": settings.qdrant_collection}
