from app.config import settings
from app.kb import es_backend


def _hit(doc_id: str, seq: int, score: float = 1.0) -> dict:
    return {
        "_id": f"{doc_id}:{seq}",
        "_score": score,
        "_source": {
            "doc_id": doc_id,
            "kb_id": "kb-1",
            "title": f"文档 {doc_id}",
            "seq": seq,
            "text": f"片段 {doc_id}-{seq}",
        },
    }


def test_search_pool_unions_channels_and_keeps_ranks(monkeypatch):
    knn_hits = [_hit("a", 0), _hit("b", 1)]
    text_hits = [_hit("b", 1), _hit("c", 0)]
    calls: list[dict] = []

    def fake_search(body):
        calls.append(body)
        return knn_hits if "knn" in body else text_hits

    monkeypatch.setattr(es_backend, "ensure", lambda dim=None: None)
    monkeypatch.setattr(es_backend, "_search_body", fake_search)
    monkeypatch.setattr(settings, "kb_hybrid", True)

    result = es_backend.search_pool([0.1], ["kb-1"], 20, "发布流程")

    assert [item["doc_id"] for item in result] == ["b", "a", "c"]
    by_doc = {item["doc_id"]: item for item in result}
    assert (by_doc["b"]["knn_rank"], by_doc["b"]["bm25_rank"]) == (2, 1)
    assert (by_doc["a"]["knn_rank"], by_doc["a"]["bm25_rank"]) == (1, None)
    assert (by_doc["c"]["knn_rank"], by_doc["c"]["bm25_rank"]) == (None, 2)
    assert [item["rrf_rank"] for item in result] == [1, 2, 3]
    assert len(calls) == 2
    assert calls[0]["knn"]["filter"] == {"terms": {"kb_id": ["kb-1"]}}
    assert calls[1]["query"]["bool"]["filter"] == [{"terms": {"kb_id": ["kb-1"]}}]


def test_search_pool_without_query_uses_knn_only(monkeypatch):
    knn_hits = [_hit("a", 0, 0.9)]
    calls: list[dict] = []

    def fake_search(body):
        calls.append(body)
        return knn_hits

    monkeypatch.setattr(es_backend, "ensure", lambda dim=None: None)
    monkeypatch.setattr(es_backend, "_search_body", fake_search)
    monkeypatch.setattr(settings, "kb_hybrid", True)

    result = es_backend.search_pool([0.1], None, 20, None)

    assert len(calls) == 1
    assert result[0]["score"] == 0.9
    assert result[0]["bm25_rank"] is None
    assert result[0]["rrf_rank"] == 1
