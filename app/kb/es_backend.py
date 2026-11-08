"""向量库后端：Elasticsearch（BM25 + kNN 混合检索）。

为什么换到 ES：知识库检索经常遇到"错误码/编号/函数名"这类字面匹配，
纯向量召回不稳；ES 在同一个引擎里同时有 BM25 和 dense_vector kNN，
两路各取 topN，再用 RRF 融合，答案质量和可解释性都更好。

分词用内置 cjk（中文 bigram），不依赖 IK 插件，镜像零定制；
以后觉得切分不够准，再把 analyzer 换成 ik_max_word / ik_smart。
"""

from __future__ import annotations

import json
import logging
from typing import Any

import httpx

from app.config import settings
from app.kb.common import chunk_payload, point_id

logger = logging.getLogger(__name__)

_client: httpx.Client | None = None
# RRF 的平滑常数，业界常用 60；两路排序做倒数和，避免单一打分尺度不可比
RRF_K = 60
_SOURCE_FIELDS = ["kb_id", "doc_id", "title", "seq", "text"]


def get_client() -> httpx.Client:
    global _client
    if _client is None:
        auth = (settings.es_username, settings.es_password) if settings.es_username else None
        _client = httpx.Client(
            base_url=settings.es_url.rstrip("/"),
            timeout=settings.es_timeout,
            auth=auth,
            headers={"Content-Type": "application/json"},
        )
    return _client


def close() -> None:
    global _client
    if _client is not None:
        _client.close()
        _client = None


def _index() -> str:
    return settings.es_index


def index_mapping(dim: int | None = None) -> dict[str, Any]:
    """索引 mapping：一个索引装所有知识库，用 kb_id 过滤（和 Qdrant 的 payload 一一对应）。"""
    return {
        "settings": {
            "number_of_shards": 1,
            "number_of_replicas": 0,
            "refresh_interval": "1s",
        },
        "mappings": {
            "properties": {
                "kb_id": {"type": "keyword"},
                "doc_id": {"type": "keyword"},
                "title": {"type": "text", "analyzer": "cjk", "fields": {"raw": {"type": "keyword"}}},
                "seq": {"type": "integer"},
                "text": {"type": "text", "analyzer": "cjk"},
                "vector": {
                    "type": "dense_vector",
                    "dims": dim or settings.embed_dim,
                    "index": True,
                    "similarity": "cosine",
                    "index_options": {"type": "hnsw", "m": 16, "ef_construction": 100},
                },
            }
        },
    }


def _check(resp: httpx.Response, action: str) -> httpx.Response:
    if resp.status_code >= 400:
        body = resp.text[:300]
        raise RuntimeError(f"ES {action} 失败 {resp.status_code}: {body}")
    return resp


def ensure(dim: int | None = None) -> None:
    """索引不存在就建（HEAD 幂等，代价很小，不做缓存以免索引被外部删掉后一直 404）。"""
    client = get_client()
    resp = client.head(f"/{_index()}")
    if resp.status_code == 200:
        return
    if resp.status_code != 404:
        _check(resp, "HEAD 索引")
    _check(client.put(f"/{_index()}", json=index_mapping(dim)), "创建索引")
    logger.info("创建 ES 索引 %s (dim=%s)", _index(), dim or settings.embed_dim)


def bulk_body(
    kb_id: str,
    doc_id: str,
    title: str,
    chunks: list[dict[str, Any]],
    vectors: list[list[float]],
) -> str:
    """组装 _bulk 的 NDJSON：同一 chunk_id 覆盖写，天然幂等。"""
    lines: list[str] = []
    for chunk, vector in zip(chunks, vectors, strict=False):
        lines.append(json.dumps({"index": {"_index": _index(), "_id": point_id(chunk["id"])}}, ensure_ascii=False))
        doc = chunk_payload(kb_id, doc_id, title, chunk)
        doc["vector"] = vector
        lines.append(json.dumps(doc, ensure_ascii=False))
    return "\n".join(lines) + "\n" if lines else ""


def upsert_chunks(
    kb_id: str,
    doc_id: str,
    title: str,
    chunks: list[dict[str, Any]],
    vectors: list[list[float]],
) -> None:
    body = bulk_body(kb_id, doc_id, title, chunks, vectors)
    if not body:
        return
    ensure()
    resp = get_client().post(
        "/_bulk",
        params={"refresh": "wait_for"},
        content=body.encode("utf-8"),
        headers={"Content-Type": "application/x-ndjson"},
    )
    _check(resp, "bulk 写入")
    payload = resp.json()
    if payload.get("errors"):
        first = next((item for item in payload.get("items", []) if item.get("index", {}).get("error")), {})
        raise RuntimeError(f"ES 部分写入失败：{json.dumps(first, ensure_ascii=False)[:300]}")


def _delete(query: dict[str, Any]) -> None:
    """索引不存在时删除无意义，按成功处理（重建索引即可恢复）。"""
    try:
        resp = get_client().post(
            f"/{_index()}/_delete_by_query",
            params={"refresh": "true", "conflicts": "proceed"},
            json={"query": query},
        )
        if resp.status_code == 404:
            return
        _check(resp, "删除")
    except Exception as exc:  # noqa: BLE001
        logger.warning("删除 ES 文档失败（忽略）：%s", exc)


def delete_doc(doc_id: str) -> None:
    _delete({"term": {"doc_id": doc_id}})


def delete_kb(kb_id: str) -> None:
    _delete({"term": {"kb_id": kb_id}})


def rrf_fuse(ranked_lists: list[list[dict[str, Any]]], k: int = RRF_K) -> list[dict[str, Any]]:
    """RRF 融合多路召回结果：score = Σ 1/(k + rank)，rank 从 1 开始。

    同一 chunk 在多路里出现就累加，所以被两路同时命中的片段排最前。
    返回的 score 归一化到 0~1（除以本批最大值），只用于展示可读性。
    """
    merged: dict[str, dict[str, Any]] = {}
    for hits in ranked_lists:
        for rank, hit in enumerate(hits, start=1):
            key = hit.get("_id") or f"{hit.get('doc_id')}:{hit.get('seq')}"
            item = merged.setdefault(key, {"hit": hit, "score": 0.0})
            item["score"] += 1.0 / (k + rank)
    fused = sorted(merged.values(), key=lambda item: item["score"], reverse=True)
    top = fused[0]["score"] if fused else 0.0
    return [{**item["hit"], "score": round(item["score"] / top, 4) if top else 0.0} for item in fused]


def _search_body(body: dict[str, Any]) -> list[dict[str, Any]]:
    resp = get_client().post(f"/{_index()}/_search", json=body)
    if resp.status_code == 404:  # 索引还没建
        return []
    _check(resp, "检索")
    return resp.json().get("hits", {}).get("hits", []) or []


def search(
    vector: list[float],
    kb_ids: list[str] | None,
    top_k: int,
    query_text: str | None = None,
) -> list[dict[str, Any]]:
    """两路召回 + RRF：kNN 保证语义相近，BM25 兜住错误码/编号这类字面命中。"""
    ensure()
    size = max(top_k * 2, 20)
    flt = {"terms": {"kb_id": list(kb_ids)}} if kb_ids else None

    knn: dict[str, Any] = {
        "field": "vector",
        "query_vector": vector,
        "k": size,
        "num_candidates": max(size * 10, 200),
    }
    if flt:
        knn["filter"] = flt
    knn_body: dict[str, Any] = {
        "knn": knn,
        "size": size,
        "_source": _SOURCE_FIELDS,
        "track_total_hits": False,
    }
    knn_hits = _search_body(knn_body)

    text_hits: list[dict[str, Any]] = []
    if query_text and settings.kb_hybrid:
        query: dict[str, Any] = {"bool": {"must": [{"match": {"text": {"query": query_text}}}]}}
        if flt:
            query["bool"]["filter"] = [flt]
        text_hits = _search_body({"query": query, "size": size, "_source": _SOURCE_FIELDS, "track_total_hits": False})

    if text_hits:
        hits = rrf_fuse([knn_hits, text_hits])
    else:
        # 没有关键词召回（或关闭混合）：直接用 kNN 相似度，0~1 可读
        hits = [{**hit, "score": round(float(hit.get("_score", 0.0)), 4)} for hit in knn_hits]

    return [
        {
            "doc_id": hit.get("_source", {}).get("doc_id", ""),
            "kb_id": hit.get("_source", {}).get("kb_id", ""),
            "title": hit.get("_source", {}).get("title", ""),
            "seq": hit.get("_source", {}).get("seq", 0),
            "text": hit.get("_source", {}).get("text", ""),
            "score": float(hit.get("score", 0.0)),
        }
        for hit in hits[:top_k]
    ]


def _source_key(hit: dict[str, Any]) -> tuple[str, int]:
    source = hit.get("_source") or {}
    return str(source.get("doc_id", "")), int(source.get("seq", 0) or 0)


def search_pool(
    vector: list[float],
    kb_ids: list[str] | None,
    size: int,
    query_text: str | None = None,
) -> list[dict[str, Any]]:
    """评测候选池：kNN + BM25 各取 size 条，去重并集并保留两路名次。

    与 ``search()`` 的区别是不截断到 top_k：评测需要看到“可能相关但没进前 k”
    的片段，才能人工标注出召回率的分母。返回顺序即 RRF 融合顺序。
    """
    ensure()
    size = min(100, max(1, int(size)))
    flt = {"terms": {"kb_id": list(kb_ids)}} if kb_ids else None

    knn: dict[str, Any] = {
        "field": "vector",
        "query_vector": vector,
        "k": size,
        "num_candidates": max(size * 10, 200),
    }
    if flt:
        knn["filter"] = flt
    knn_hits = _search_body(
        {
            "knn": knn,
            "size": size,
            "_source": _SOURCE_FIELDS,
            "track_total_hits": False,
        }
    )

    text_hits: list[dict[str, Any]] = []
    if query_text and settings.kb_hybrid:
        query: dict[str, Any] = {"bool": {"must": [{"match": {"text": {"query": query_text}}}]}}
        if flt:
            query["bool"]["filter"] = [flt]
        text_hits = _search_body({"query": query, "size": size, "_source": _SOURCE_FIELDS, "track_total_hits": False})

    if text_hits:
        fused = rrf_fuse([knn_hits, text_hits])
    else:
        fused = [{**hit, "score": round(float(hit.get("_score", 0.0)), 4)} for hit in knn_hits]

    knn_ranks = {_source_key(hit): rank for rank, hit in enumerate(knn_hits, start=1)}
    bm25_ranks = {_source_key(hit): rank for rank, hit in enumerate(text_hits, start=1)}
    result: list[dict[str, Any]] = []
    for rank, hit in enumerate(fused, start=1):
        source = hit.get("_source") or {}
        key = _source_key(hit)
        result.append(
            {
                "doc_id": source.get("doc_id", ""),
                "kb_id": source.get("kb_id", ""),
                "title": source.get("title", ""),
                "seq": int(source.get("seq", 0) or 0),
                "text": source.get("text", ""),
                "score": float(hit.get("score", 0.0)),
                "knn_rank": knn_ranks.get(key),
                "bm25_rank": bm25_ranks.get(key),
                "rrf_rank": rank,
            }
        )
    return result


def relevance_query(
    terms: list[str],
    kb_ids: list[str] | None,
    min_should: int,
) -> dict[str, Any]:
    """统计“相关片段”的查询：命中至少 min_should 个问题关键词。

    每个关键词单独 match，避免 cjk 分词把整句拆成一堆无关 bigram；
    拉丁多词（如 tf-game）要求全部 token 命中，和检索侧保持一致。
    """
    bool_query: dict[str, Any] = {
        "should": [{"match": {"text": {"query": term, "operator": "and"}}} for term in terms],
        "minimum_should_match": max(1, min(min_should, len(terms))),
    }
    if kb_ids:
        bool_query["filter"] = [{"terms": {"kb_id": list(kb_ids)}}]
    return {"bool": bool_query}


def count_relevant(
    terms: list[str],
    kb_ids: list[str] | None,
    min_should: int,
) -> int:
    """全库相关片段数——召回率的分母，不是本批召回结果。"""
    if not terms:
        return 0
    resp = get_client().post(
        f"/{_index()}/_count",
        json={"query": relevance_query(terms, kb_ids, min_should)},
    )
    if resp.status_code == 404:  # 索引还没建
        return 0
    _check(resp, "统计相关片段")
    return int(resp.json().get("count", 0))


def health() -> bool:
    try:
        resp = get_client().get("/_cluster/health")
        if resp.status_code >= 400:
            return False
        return resp.json().get("status") in {"green", "yellow"}
    except Exception as exc:  # noqa: BLE001
        logger.debug("ES 不可用：%s", exc)
        return False


def info() -> dict[str, Any]:
    return {"backend": "es", "endpoint": settings.es_url, "index": _index()}
