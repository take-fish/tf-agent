"""向量库：ES 的纯函数与 store 入口。

不连真实 ES，只验证 mapping / bulk NDJSON / RRF 融合这些"错了会静默丢数据"的地方。
"""

from __future__ import annotations

import json

from app.kb import es_backend, store

CHUNKS = [
    {"id": "c1", "seq": 0, "text": "值班交接时间是每天 09:30"},
    {"id": "c2", "seq": 3, "text": "回滚步骤：先切流量，再回退镜像"},
]
VECTORS = [[0.1, 0.2], [0.3, 0.4]]


def test_index_mapping_uses_cosine_and_keyword_filters():
    mapping = es_backend.index_mapping()
    props = mapping["mappings"]["properties"]
    assert props["vector"]["type"] == "dense_vector"
    assert props["vector"]["dims"] == 768
    assert props["vector"]["similarity"] == "cosine"
    assert props["kb_id"]["type"] == "keyword"
    assert props["doc_id"]["type"] == "keyword"
    assert props["text"]["analyzer"] == "cjk"


def test_bulk_body_ids_are_stable_and_payload_complete():
    body = es_backend.bulk_body("kb1", "doc1", "值班交接规范", CHUNKS, VECTORS)
    lines = body.strip().split("\n")
    assert len(lines) == 4  # 两条 chunk → 2 组 index+doc
    action = json.loads(lines[0])
    doc = json.loads(lines[1])
    assert action["index"]["_id"] == es_backend.point_id("c1")
    assert doc["kb_id"] == "kb1"
    assert doc["doc_id"] == "doc1"
    assert doc["title"] == "值班交接规范"
    assert doc["seq"] == 0
    assert doc["vector"] == [0.1, 0.2]
    # 同一个 chunk 重新入库必须落到同一个 _id，重建索引才不会产生重复
    assert es_backend.bulk_body("kb1", "doc1", "t", CHUNKS, VECTORS).split("\n")[0] == lines[0]


def test_rrf_fuse_rewards_hits_present_in_both_lists():
    bm25 = [
        {"_id": "b", "text": "BM25 第一条"},
        {"_id": "a", "text": "BM25 第二条"},
    ]
    knn = [
        {"_id": "a", "text": "kNN 第一条"},
        {"_id": "c", "text": "kNN 第二条"},
    ]
    fused = es_backend.rrf_fuse([bm25, knn])
    assert [item["_id"] for item in fused] == ["a", "b", "c"]
    # 归一化后第一名是 1.0，其它依次变小
    assert fused[0]["score"] == 1.0
    assert fused[1]["score"] < 1.0


def test_rrf_fuse_handles_empty_and_single_lists():
    assert es_backend.rrf_fuse([]) == []
    only = es_backend.rrf_fuse([[{"_id": "x"}]])
    assert len(only) == 1 and only[0]["score"] == 1.0


def test_store_is_es_and_exposes_expected_entrypoints():
    assert store.backend() == "es"
    for name in ("ensure", "upsert_chunks", "delete_doc", "delete_kb", "search", "health", "info"):
        assert callable(getattr(store, name)), name


def test_store_health_tracks_es_backend(monkeypatch):
    called: list[str] = []
    monkeypatch.setattr(es_backend, "health", lambda: called.append("es") or True)
    assert store.health() is True
    assert called == ["es"]
