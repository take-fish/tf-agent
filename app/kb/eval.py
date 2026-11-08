"""知识库检索质量：面向整个知识库的关键词相关性判定。

线上没有人工标注的“标准答案”，这里用关键词覆盖定义相关性：
一个片段覆盖问题关键词的一半以上，就视为相关。指标按整个检索范围计算：

- 召回率 = 系统找回的相关片段 ÷ 知识库里所有相关片段
  （严格定义要求人工标注“哪些片段相关”；线上没有标注，这里由关键词规则判定）
- 精确率 = 系统找回的相关片段 ÷ 系统本次找回的片段总数

关键词与 ES 的 cjk 分词保持一致：中文取 bigram（全为停用字的 bigram 丢弃），
拉丁/数字取整词。全库相关片段数由 ES `_count` 统计，因此分母不是本批结果。
"""

from __future__ import annotations

import math
import re
from typing import Any

_CJK_RUN = re.compile(r"[㐀-䶿一-鿿豈-﫿]+")
_LATIN_WORD = re.compile(r"[a-z0-9][a-z0-9_.-]*")

# 中文疑问词、代词和虚词：只用于丢弃“全由停用字组成”的 bigram。
_CJK_STOP = set(
    "的了吗呢吧呀啊是有没在和与或及等你我他她它们这那哪什么怎样如何请问"
    "可以能会要需把被对为就都也还很太最更再又让给从到向于个些一下里中来去"
)

_LATIN_STOP = {
    "the",
    "and",
    "what",
    "how",
    "when",
    "where",
    "which",
    "who",
    "why",
    "is",
    "are",
    "was",
    "were",
    "be",
    "been",
    "do",
    "does",
    "did",
    "a",
    "an",
    "of",
    "to",
    "in",
    "on",
    "for",
    "with",
    "about",
    "please",
}


def query_terms(query: str) -> list[str]:
    """抽取能对上 ES cjk 分词的关键词。"""
    text = (query or "").lower()
    terms: list[str] = []
    seen: set[str] = set()

    def add(term: str) -> None:
        if term and term not in seen:
            seen.add(term)
            terms.append(term)

    for word in _LATIN_WORD.findall(text):
        word = word.strip("._-")
        if len(word) >= 2 and word not in _LATIN_STOP:
            add(word)

    for run in _CJK_RUN.findall(text):
        if len(run) == 1:
            if run not in _CJK_STOP:
                add(run)
            continue
        for index in range(len(run) - 1):
            bigram = run[index : index + 2]
            if any(char not in _CJK_STOP for char in bigram):
                add(bigram)
    return terms


def relevance_threshold(term_count: int) -> int:
    """覆盖一半以上关键词视为相关；单个关键词命中即相关。"""
    return max(1, math.ceil(term_count / 2))


def chunk_hits(text: str, terms: list[str]) -> int:
    lowered = (text or "").lower()
    return sum(1 for term in terms if term in lowered)


def evaluate(
    query: str,
    hits: list[dict[str, Any]],
    total_relevant: int,
) -> dict[str, Any] | None:
    """按全库相关数计算召回率/精确率；分母缺失时返回 None。"""
    terms = query_terms(query)
    if not terms or not hits or total_relevant <= 0:
        return None
    threshold = relevance_threshold(len(terms))
    matched = sum(1 for hit in hits if chunk_hits(hit.get("text", ""), terms) >= threshold)
    if matched == 0:
        return {
            "recall": 0.0,
            "precision": 0.0,
            "matched": 0,
            "retrieved": len(hits),
            "relevant": total_relevant,
            "terms": len(terms),
        }
    recall = min(1.0, matched / total_relevant)
    return {
        "recall": round(recall, 4),
        "precision": round(matched / len(hits), 4),
        "matched": matched,
        "retrieved": len(hits),
        "relevant": total_relevant,
        "terms": len(terms),
    }
