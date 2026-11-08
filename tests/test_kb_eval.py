from app.kb import eval as kb_eval
from app.kb.es_backend import relevance_query


def test_query_terms_keep_cjk_bigrams_and_drop_stopword_pairs():
    terms = kb_eval.query_terms("周杰伦有哪些歌？")
    assert "周杰" in terms
    assert "杰伦" in terms
    assert "有哪" not in terms
    assert "哪些" not in terms


def test_query_terms_keep_latin_words_and_identifiers():
    terms = kb_eval.query_terms("How to deploy tf-game?")
    assert "deploy" in terms
    assert "tf-game" in terms
    assert "how" not in terms
    assert "to" not in terms


def test_relevance_threshold_is_half_of_terms():
    assert kb_eval.relevance_threshold(1) == 1
    assert kb_eval.relevance_threshold(4) == 2
    assert kb_eval.relevance_threshold(5) == 3


def test_evaluate_uses_whole_corpus_as_recall_denominator():
    metrics = kb_eval.evaluate(
        "周杰伦有哪些歌",
        [
            {"text": "周杰伦 - 千年泪\n周杰伦 - 发如雪"},
            {"text": "孙燕姿 - 天黑黑"},
            {"text": "张杰 - 三生三世"},
        ],
        total_relevant=5,
    )
    assert metrics is not None
    assert metrics["matched"] == 1
    assert metrics["retrieved"] == 3
    assert metrics["relevant"] == 5
    assert metrics["recall"] == 0.2
    assert metrics["precision"] == 0.3333


def test_evaluate_returns_none_without_terms_hits_or_denominator():
    assert kb_eval.evaluate("什么怎么", [{"text": "任意内容"}], 0) is None
    assert kb_eval.evaluate("发布流程", [], 3) is None
    assert kb_eval.evaluate("发布流程", [{"text": "发布流程说明"}], 0) is None


def test_relevance_query_counts_across_kb_filter():
    query = relevance_query(["发布", "流程", "tf-game"], ["kb-1"], 2)
    body = query["bool"]
    assert body["minimum_should_match"] == 2
    assert body["filter"] == [{"terms": {"kb_id": ["kb-1"]}}]
    assert [clause["match"]["text"]["query"] for clause in body["should"]] == [
        "发布",
        "流程",
        "tf-game",
    ]
