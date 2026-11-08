from app.kb import eval_metrics


def _candidate(rank: int, label: int | None) -> dict:
    return {"rrf_rank": rank, "label": label}


def test_precision_and_recall_use_pooled_relevant_denominator():
    candidates = [
        _candidate(1, 1),
        _candidate(2, 0),
        _candidate(3, 2),
        _candidate(4, 1),
        _candidate(5, 0),
    ]
    assert eval_metrics.precision_at_k(candidates, 3) == 0.6667
    # 池内相关 3 条，前 3 条命中 2 条
    assert eval_metrics.recall_at_k(candidates, 3) == 0.6667
    assert eval_metrics.mrr_at_k(candidates, 3) == 1.0


def test_question_metrics_requires_every_candidate_labeled():
    partial = [_candidate(1, 1), _candidate(2, None)]
    result = eval_metrics.question_metrics(partial, 2)
    assert result["complete"] is False
    assert result["labeled_count"] == 1
    assert result["precision"] is None
    assert result["recall"] is None
    assert result["mrr"] is None


def test_question_metrics_without_relevant_chunk_has_no_recall_denominator():
    result = eval_metrics.question_metrics([_candidate(1, 0), _candidate(2, 0)], 2)
    assert result["complete"] is True
    assert result["precision"] == 0.0
    assert result["recall"] is None
    assert result["mrr"] == 0.0


def test_mrr_uses_first_relevant_rank_within_top_k():
    candidates = [_candidate(1, 0), _candidate(2, 0), _candidate(3, 1)]
    assert eval_metrics.mrr_at_k(candidates, 3) == round(1 / 3, 4)
    assert eval_metrics.mrr_at_k(candidates, 2) == 0.0


def test_aggregate_is_macro_average_over_complete_questions_only():
    complete_a = eval_metrics.question_metrics([_candidate(1, 1), _candidate(2, 0)], 2)
    complete_b = eval_metrics.question_metrics([_candidate(1, 1), _candidate(2, 1)], 2)
    incomplete = eval_metrics.question_metrics([_candidate(1, 1), _candidate(2, None)], 2)
    aggregate = eval_metrics.aggregate_metrics([complete_a, complete_b, incomplete])
    assert aggregate["question_count"] == 3
    assert aggregate["complete_count"] == 2
    assert aggregate["precision"] == 0.75
    assert aggregate["recall"] == 1.0
    assert aggregate["mrr"] == 1.0
    assert aggregate["relevant_candidates"] == 3
