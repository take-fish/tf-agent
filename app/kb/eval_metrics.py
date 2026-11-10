"""人工标注后的检索指标（P@k / R@k / MRR）。

与 ``app.kb.eval`` 的在线近似指标不同：这里只使用人工标注的候选池。
召回率分母是**候选池内所有标注为相关的片段数**（pooled recall），
不是整个知识库；页面与文档必须把这一点说清楚。
"""

from __future__ import annotations

from typing import Any


def _ordered(candidates: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return sorted(candidates, key=lambda item: int(item.get("rrf_rank") or 0))


def _is_relevant(item: dict[str, Any]) -> bool:
    label = item.get("label")
    return label is not None and int(label) >= 1


def _round(value: float | None) -> float | None:
    return None if value is None else round(value, 4)


def precision_at_k(candidates: list[dict[str, Any]], k: int) -> float | None:
    top = _ordered(candidates)[: max(1, int(k))]
    if not top:
        return None
    hits = sum(1 for item in top if _is_relevant(item))
    return _round(hits / len(top))


def recall_at_k(candidates: list[dict[str, Any]], k: int) -> float | None:
    ordered = _ordered(candidates)
    relevant = sum(1 for item in ordered if _is_relevant(item))
    if relevant == 0:
        return None
    top = ordered[: max(1, int(k))]
    hits = sum(1 for item in top if _is_relevant(item))
    return _round(hits / relevant)


def mrr_at_k(candidates: list[dict[str, Any]], k: int) -> float | None:
    top = _ordered(candidates)[: max(1, int(k))]
    for rank, item in enumerate(top, start=1):
        if _is_relevant(item):
            return _round(1.0 / rank)
    return 0.0 if top else None


def _section_key(item: dict[str, Any]) -> tuple[str, int] | None:
    """相关章节的稳定标识；老候选没有章节字段时返回 None，不参与计算。"""
    section_seq = item.get("section_seq")
    if section_seq is None:
        return None
    return str(item.get("doc_id") or ""), int(section_seq)


def section_coverage_at_k(candidates: list[dict[str, Any]], k: int) -> float | None:
    """top-k 覆盖到的相关章节数 ÷ 标注为相关的章节总数。

    分母来自人工标注为相关的片段所在章节；同一章节有多块只算一个。
    用于流程类问题：答案常常分散在多个小节，单看片段召回率看不出“缺了哪一节”。
    """
    ordered = _ordered(candidates)
    expected = {key for item in ordered if _is_relevant(item) for key in [_section_key(item)] if key}
    if not expected:
        return None
    top = ordered[: max(1, int(k))]
    got = {key for item in top for key in [_section_key(item)] if key}
    return _round(len(expected & got) / len(expected))


def question_metrics(candidates: list[dict[str, Any]], top_k: int) -> dict[str, Any]:
    """返回逐题指标；候选没标完时指标为 None，但仍返回标注进度。"""
    ordered = _ordered(candidates)
    labeled = [item for item in ordered if item.get("label") is not None]
    relevant = sum(1 for item in ordered if _is_relevant(item))
    complete = bool(ordered) and len(labeled) == len(ordered)
    return {
        "complete": complete,
        "candidate_count": len(ordered),
        "labeled_count": len(labeled),
        "relevant_count": relevant,
        "precision": precision_at_k(ordered, top_k) if complete else None,
        "recall": recall_at_k(ordered, top_k) if complete else None,
        "mrr": mrr_at_k(ordered, top_k) if complete else None,
        "section_coverage": section_coverage_at_k(ordered, top_k) if complete else None,
    }


def aggregate_metrics(items: list[dict[str, Any]]) -> dict[str, Any]:
    """宏平均：P 统计全部完整题；R / MRR 只统计池内至少 1 条相关的完整题。"""
    complete = [item for item in items if item.get("complete")]
    precisions = [float(item["precision"]) for item in complete if item.get("precision") is not None]
    with_relevant = [item for item in complete if int(item.get("relevant_count") or 0) > 0]
    recalls = [float(item["recall"]) for item in with_relevant if item.get("recall") is not None]
    mrrs = [float(item["mrr"]) for item in with_relevant if item.get("mrr") is not None]
    coverages = [
        float(item["section_coverage"]) for item in complete if item.get("section_coverage") is not None
    ]
    return {
        "question_count": len(items),
        "complete_count": len(complete),
        "total_candidates": sum(int(item.get("candidate_count") or 0) for item in items),
        "labeled_candidates": sum(int(item.get("labeled_count") or 0) for item in items),
        "relevant_candidates": sum(int(item.get("relevant_count") or 0) for item in complete),
        "precision": _round(sum(precisions) / len(precisions)) if precisions else None,
        "recall": _round(sum(recalls) / len(recalls)) if recalls else None,
        "mrr": _round(sum(mrrs) / len(mrrs)) if mrrs else None,
        "section_coverage": _round(sum(coverages) / len(coverages)) if coverages else None,
    }


def aggregate_by_type(items: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """按 fact / flow 分组做宏平均，便于分别看事实准确率与流程覆盖度。"""
    groups: dict[str, list[dict[str, Any]]] = {}
    for item in items:
        key = str(item.get("type") or "fact")
        groups.setdefault(key, []).append(item)
    order = ["fact", "flow"]
    keys = [key for key in order if key in groups] + sorted(key for key in groups if key not in order)
    return {key: aggregate_metrics(groups[key]) for key in keys}
