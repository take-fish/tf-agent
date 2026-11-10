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


def _sections(rank: int, label: int, doc_id: str, section_seq: int) -> dict:
    return {"rrf_rank": rank, "label": label, "doc_id": doc_id, "section_seq": section_seq}


def test_section_coverage_counts_distinct_relevant_sections():
    candidates = [
        _sections(1, 1, "doc", 0),
        _sections(2, 0, "doc", 9),
        _sections(3, 1, "doc", 1),
        _sections(4, 2, "doc", 2),
    ]
    # 相关章节：doc#0、doc#1、doc#2，共 3 个；top-2 只覆盖 doc#0
    assert eval_metrics.section_coverage_at_k(candidates, 2) == 0.3333
    # top-3 覆盖 doc#0 与 doc#1
    assert eval_metrics.section_coverage_at_k(candidates, 3) == 0.6667


def test_section_coverage_is_none_without_section_metadata():
    assert eval_metrics.section_coverage_at_k([_candidate(1, 1)], 1) is None
    metrics = eval_metrics.question_metrics([_candidate(1, 1), _candidate(2, 0)], 2)
    assert metrics["section_coverage"] is None


def test_aggregate_by_type_splits_fact_and_flow():
    fact = {**eval_metrics.question_metrics([_candidate(1, 1), _candidate(2, 0)], 2), "type": "fact"}
    flow = {
        **eval_metrics.question_metrics(
            [_sections(1, 1, "doc", 0), _sections(2, 1, "doc", 1), _sections(3, 0, "doc", 2)],
            1,
        ),
        "type": "flow",
    }
    result = eval_metrics.aggregate_by_type([fact, flow])
    assert list(result) == ["fact", "flow"]
    assert result["fact"]["question_count"] == 1
    assert result["fact"]["section_coverage"] is None
    assert result["flow"]["section_coverage"] == 0.5
