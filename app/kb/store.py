"""向量库：Qdrant。一个 collection 存所有知识库，用 payload.kb_id 过滤。"""

from __future__ import annotations

import logging
import uuid
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

logger = logging.getLogger(__name__)

_client: QdrantClient | None = None
_collection_ready = False


def get_client() -> QdrantClient:
    global _client
    if _client is None:
        _client = QdrantClient(
            url=settings.qdrant_url,
            api_key=settings.qdrant_api_key or None,
            timeout=10.0,
        )
    return _client


def ensure_collection(dim: int | None = None) -> None:
    global _collection_ready
    if _collection_ready:
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
    _collection_ready = True


def point_id(chunk_id: str) -> str:
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"fm-kb/{chunk_id}"))


def upsert_chunks(
    kb_id: str,
    doc_id: str,
    title: str,
    chunks: list[dict[str, Any]],
    vectors: list[list[float]],
) -> None:
    ensure_collection(len(vectors[0]) if vectors else None)
    points = [
        PointStruct(
            id=point_id(chunk["id"]),
            vector=vector,
            payload={
                "kb_id": kb_id,
                "doc_id": doc_id,
                "title": title,
                "seq": chunk["seq"],
                "text": chunk["text"],
            },
        )
        for chunk, vector in zip(chunks, vectors, strict=False)
    ]
    if points:
        get_client().upsert(collection_name=settings.qdrant_collection, points=points, wait=True)


def delete_doc(doc_id: str) -> None:
    get_client().delete(
        collection_name=settings.qdrant_collection,
        points_selector=Filter(must=[FieldCondition(key="doc_id", match=MatchValue(value=doc_id))]),
        wait=True,
    )


def delete_kb(kb_id: str) -> None:
    get_client().delete(
        collection_name=settings.qdrant_collection,
        points_selector=Filter(must=[FieldCondition(key="kb_id", match=MatchValue(value=kb_id))]),
        wait=True,
    )


def search(vector: list[float], kb_ids: list[str] | None, top_k: int) -> list[dict[str, Any]]:
    ensure_collection(len(vector) if vector else None)
    must = []
    if kb_ids:
        must.append(FieldCondition(key="kb_id", match=MatchAny(any=kb_ids)))
    query_filter = Filter(must=must) if must else None

    hits = get_client().query_points(
        collection_name=settings.qdrant_collection,
        query=vector,
        query_filter=query_filter,
        limit=top_k,
        with_payload=True,
    ).points
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
