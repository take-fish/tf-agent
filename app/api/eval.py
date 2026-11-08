"""知识库检索评测：题集 / 候选池 / 人工标注 / 指标快照。

口径（与前端文案一致）：

- 每道题从 kNN + BM25 各取若干条合成候选池，人工给池内每条打 0/1/2；
- 召回率分母是**池内标注为相关的片段数**，属于 pooled recall，不是全库召回；
- 只有池内全部片段都标完的题才进入汇总，避免把“没标”当成“不相关”。
"""

from __future__ import annotations

import functools
import logging
import uuid
from collections import defaultdict
from datetime import datetime
from typing import Any, Literal

import anyio.to_thread
from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.auth import Identity, require_kb_manage
from app.db import db_available, session_scope
from app.kb import embedder, eval_metrics, store
from app.models import AgentEvalCandidate, AgentEvalQuestion, AgentEvalRun, AgentEvalSet, AgentKb

logger = logging.getLogger(__name__)
router = APIRouter()

MAX_IMPORT_QUESTIONS = 100
MAX_POOL_BATCH = 50
MAX_QUERY_CHARS = 500


class EvalSetCreate(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    description: str = Field(default="", max_length=255)
    kb_ids: list[str] = Field(default_factory=list)
    top_k: int = Field(default=8, ge=1, le=20)


class QuestionImport(BaseModel):
    queries: list[str] = Field(default_factory=list)


class CandidateLabelUpdate(BaseModel):
    label: Literal[0, 1, 2] | None = None


def _require_db() -> None:
    if not db_available():
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "审计库不可用，评测功能暂不可用")


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


def _set_dict(row: AgentEvalSet) -> dict[str, Any]:
    return {
        "id": row.id,
        "name": row.name,
        "description": row.description,
        "kb_ids": [str(item) for item in (row.kb_ids or [])],
        "top_k": row.top_k,
        "created_at": _iso(row.created_at),
    }


def _candidate_dict(row: AgentEvalCandidate) -> dict[str, Any]:
    return {
        "id": row.id,
        "question_id": row.question_id,
        "doc_id": row.doc_id,
        "seq": row.seq,
        "title": row.title,
        "text": row.text,
        "score": row.score,
        "knn_rank": row.knn_rank,
        "bm25_rank": row.bm25_rank,
        "rrf_rank": row.rrf_rank,
        "label": row.label,
        "label_source": row.label_source,
        "labeled_at": _iso(row.labeled_at),
    }


def _run_dict(row: AgentEvalRun, *, include_detail: bool = False) -> dict[str, Any]:
    data = {
        "id": row.id,
        "set_id": row.set_id,
        "top_k": row.top_k,
        "question_count": row.question_count,
        "complete_count": row.complete_count,
        "precision": row.precision,
        "recall": row.recall,
        "mrr": row.mrr,
        "created_at": _iso(row.created_at),
    }
    if include_detail:
        data["detail"] = row.detail
    return data


def _require_set(db: Session, set_id: str) -> AgentEvalSet:
    row = db.get(AgentEvalSet, set_id)
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "评测题集不存在")
    return row


def _require_question(db: Session, question_id: str) -> AgentEvalQuestion:
    row = db.get(AgentEvalQuestion, question_id)
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "评测问题不存在")
    return row


def _group_candidates(db: Session, question_ids: list[str]) -> dict[str, list[AgentEvalCandidate]]:
    if not question_ids:
        return {}
    rows = (
        db.query(AgentEvalCandidate)
        .filter(AgentEvalCandidate.question_id.in_(question_ids))
        .order_by(AgentEvalCandidate.rrf_rank, AgentEvalCandidate.id)
        .all()
    )
    grouped: dict[str, list[AgentEvalCandidate]] = defaultdict(list)
    for row in rows:
        grouped[row.question_id].append(row)
    return grouped


def _question_summary(
    question: AgentEvalQuestion,
    candidates: list[AgentEvalCandidate],
    top_k: int,
) -> dict[str, Any]:
    metrics = eval_metrics.question_metrics(
        [{"rrf_rank": row.rrf_rank, "label": row.label} for row in candidates],
        top_k,
    )
    return {
        "id": question.id,
        "query": question.query,
        "note": question.note,
        "pooled_at": _iso(question.pooled_at),
        **metrics,
    }


def _question_summaries(
    questions: list[AgentEvalQuestion],
    grouped: dict[str, list[AgentEvalCandidate]],
    top_k: int,
) -> list[dict[str, Any]]:
    return [_question_summary(question, grouped.get(question.id, []), top_k) for question in questions]


def _replace_candidates(question_id: str, hits: list[dict[str, Any]]) -> None:
    """替换单题候选池（标签随旧候选一起清空）。"""
    with session_scope() as db:
        db.query(AgentEvalCandidate).filter(AgentEvalCandidate.question_id == question_id).delete(
            synchronize_session=False
        )
        for hit in hits:
            db.add(
                AgentEvalCandidate(
                    question_id=question_id,
                    doc_id=str(hit.get("doc_id", "")),
                    seq=int(hit.get("seq", 0) or 0),
                    title=str(hit.get("title", ""))[:255],
                    text=str(hit.get("text", "")),
                    score=float(hit.get("score", 0.0) or 0.0),
                    knn_rank=hit.get("knn_rank"),
                    bm25_rank=hit.get("bm25_rank"),
                    rrf_rank=int(hit.get("rrf_rank", 0) or 0),
                )
            )
        question = db.get(AgentEvalQuestion, question_id)
        if question is not None:
            question.pooled_at = datetime.now()


def _pool_size(top_k: int) -> int:
    return min(50, max(20, int(top_k) * 2))


# ---------------------------------------------------------------- 题集


@router.get("/sets")
async def list_sets(_: Identity = Depends(require_kb_manage)):
    _require_db()

    def _work() -> list[dict[str, Any]]:
        with session_scope() as db:
            sets = db.query(AgentEvalSet).order_by(AgentEvalSet.created_at.desc()).limit(50).all()
            if not sets:
                return []
            questions = db.query(AgentEvalQuestion).filter(AgentEvalQuestion.set_id.in_([row.id for row in sets])).all()
            grouped = _group_candidates(db, [row.id for row in questions])
            by_set: dict[str, list[AgentEvalQuestion]] = defaultdict(list)
            for question in questions:
                by_set[question.set_id].append(question)
            result = []
            for row in sets:
                items = _question_summaries(by_set.get(row.id, []), grouped, row.top_k)
                result.append(
                    {
                        **_set_dict(row),
                        "question_count": len(items),
                        "pooled_count": sum(1 for item in items if item["pooled_at"]),
                        "complete_count": sum(1 for item in items if item["complete"]),
                    }
                )
            return result

    return {"code": 0, "data": await anyio.to_thread.run_sync(_work)}


@router.post("/sets")
async def create_set(payload: EvalSetCreate, identity: Identity = Depends(require_kb_manage)):
    _require_db()

    def _work() -> dict[str, Any]:
        kb_ids = list(dict.fromkeys(item.strip() for item in payload.kb_ids if item.strip()))
        with session_scope() as db:
            if kb_ids:
                found = {row[0] for row in db.query(AgentKb.id).filter(AgentKb.id.in_(kb_ids)).all()}
                missing = [item for item in kb_ids if item not in found]
                if missing:
                    raise HTTPException(status.HTTP_400_BAD_REQUEST, f"知识库不存在：{missing[0]}")
            row = AgentEvalSet(
                id=uuid.uuid4().hex[:32],
                name=payload.name.strip(),
                description=payload.description.strip(),
                kb_ids=kb_ids,
                top_k=payload.top_k,
                created_by=identity.user_id,
            )
            db.add(row)
            db.flush()
            return {**_set_dict(row), "question_count": 0, "pooled_count": 0, "complete_count": 0}

    return {"code": 0, "data": await anyio.to_thread.run_sync(_work)}


@router.get("/sets/{set_id}")
async def get_set(set_id: str, _: Identity = Depends(require_kb_manage)):
    _require_db()

    def _work() -> dict[str, Any]:
        with session_scope() as db:
            row = _require_set(db, set_id)
            questions = (
                db.query(AgentEvalQuestion)
                .filter(AgentEvalQuestion.set_id == set_id)
                .order_by(AgentEvalQuestion.created_at, AgentEvalQuestion.id)
                .all()
            )
            grouped = _group_candidates(db, [question.id for question in questions])
            items = _question_summaries(questions, grouped, row.top_k)
            return {**_set_dict(row), "questions": items, "metrics": eval_metrics.aggregate_metrics(items)}

    return {"code": 0, "data": await anyio.to_thread.run_sync(_work)}


@router.delete("/sets/{set_id}")
async def delete_set(set_id: str, _: Identity = Depends(require_kb_manage)):
    _require_db()

    def _work() -> None:
        with session_scope() as db:
            _require_set(db, set_id)
            question_ids = [
                row[0] for row in db.query(AgentEvalQuestion.id).filter(AgentEvalQuestion.set_id == set_id).all()
            ]
            if question_ids:
                db.query(AgentEvalCandidate).filter(AgentEvalCandidate.question_id.in_(question_ids)).delete(
                    synchronize_session=False
                )
            db.query(AgentEvalQuestion).filter(AgentEvalQuestion.set_id == set_id).delete(synchronize_session=False)
            db.query(AgentEvalRun).filter(AgentEvalRun.set_id == set_id).delete(synchronize_session=False)
            row = db.get(AgentEvalSet, set_id)
            if row is not None:
                db.delete(row)

    await anyio.to_thread.run_sync(_work)
    return {"code": 0, "msg": "deleted"}


# ---------------------------------------------------------------- 问题


@router.post("/sets/{set_id}/questions")
async def import_questions(
    set_id: str,
    payload: QuestionImport,
    identity: Identity = Depends(require_kb_manage),
):
    _require_db()
    if len(payload.queries) > MAX_IMPORT_QUESTIONS:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, f"单次最多导入 {MAX_IMPORT_QUESTIONS} 条问题")

    cleaned: list[str] = []
    seen: set[str] = set()
    for index, raw in enumerate(payload.queries):
        query = (raw or "").strip()
        if not query or query in seen:
            continue
        if len(query) > MAX_QUERY_CHARS:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, f"第 {index + 1} 条问题超过 {MAX_QUERY_CHARS} 字")
        seen.add(query)
        cleaned.append(query)

    def _work() -> dict[str, Any]:
        with session_scope() as db:
            _require_set(db, set_id)
            existing = {
                row[0] for row in db.query(AgentEvalQuestion.query).filter(AgentEvalQuestion.set_id == set_id).all()
            }
            added: list[str] = []
            for query in cleaned:
                if query in existing:
                    continue
                question_id = uuid.uuid4().hex[:32]
                db.add(
                    AgentEvalQuestion(
                        id=question_id,
                        set_id=set_id,
                        query=query,
                        created_by=identity.user_id,
                    )
                )
                added.append(question_id)
            return {"added": len(added), "skipped": len(payload.queries) - len(added), "ids": added}

    return {"code": 0, "data": await anyio.to_thread.run_sync(_work)}


@router.delete("/questions/{question_id}")
async def delete_question(question_id: str, _: Identity = Depends(require_kb_manage)):
    _require_db()

    def _work() -> None:
        with session_scope() as db:
            _require_question(db, question_id)
            db.query(AgentEvalCandidate).filter(AgentEvalCandidate.question_id == question_id).delete(
                synchronize_session=False
            )
            question = db.get(AgentEvalQuestion, question_id)
            if question is not None:
                db.delete(question)

    await anyio.to_thread.run_sync(_work)
    return {"code": 0, "msg": "deleted"}


@router.get("/questions/{question_id}/candidates")
async def list_candidates(question_id: str, _: Identity = Depends(require_kb_manage)):
    _require_db()

    def _work() -> dict[str, Any]:
        with session_scope() as db:
            question = _require_question(db, question_id)
            rows = (
                db.query(AgentEvalCandidate)
                .filter(AgentEvalCandidate.question_id == question_id)
                .order_by(AgentEvalCandidate.rrf_rank, AgentEvalCandidate.id)
                .all()
            )
            return {
                "question": {
                    "id": question.id,
                    "set_id": question.set_id,
                    "query": question.query,
                    "pooled_at": _iso(question.pooled_at),
                },
                "candidates": [_candidate_dict(row) for row in rows],
            }

    return {"code": 0, "data": await anyio.to_thread.run_sync(_work)}


# ---------------------------------------------------------------- 候选池


@router.post("/questions/{question_id}/pool")
async def pool_question(question_id: str, _: Identity = Depends(require_kb_manage)):
    _require_db()

    with session_scope() as db:
        question = _require_question(db, question_id)
        eval_set = _require_set(db, question.set_id)
        query = question.query
        kb_ids = [str(item) for item in (eval_set.kb_ids or [])] or None
        top_k = eval_set.top_k

    try:
        vector = (await embedder.embed([query]))[0]
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, f"向量化失败：{exc}") from exc

    try:
        hits = await anyio.to_thread.run_sync(
            functools.partial(store.search_pool, vector, kb_ids, _pool_size(top_k), query)
        )
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, f"候选池生成失败：{exc}") from exc

    await anyio.to_thread.run_sync(functools.partial(_replace_candidates, question_id, hits))
    return {"code": 0, "data": {"id": question_id, "candidate_count": len(hits), "pooled_at": _iso(datetime.now())}}


@router.post("/sets/{set_id}/pool")
async def pool_set(set_id: str, _: Identity = Depends(require_kb_manage)):
    """批量池化尚未池化的问题；单题失败不阻断其他题。"""
    _require_db()

    with session_scope() as db:
        eval_set = _require_set(db, set_id)
        kb_ids = [str(item) for item in (eval_set.kb_ids or [])] or None
        top_k = eval_set.top_k
        questions = (
            db.query(AgentEvalQuestion)
            .filter(AgentEvalQuestion.set_id == set_id, AgentEvalQuestion.pooled_at.is_(None))
            .order_by(AgentEvalQuestion.created_at, AgentEvalQuestion.id)
            .limit(MAX_POOL_BATCH)
            .all()
        )
        targets = [(question.id, question.query) for question in questions]
        total_unpolled = (
            db.query(AgentEvalQuestion)
            .filter(AgentEvalQuestion.set_id == set_id, AgentEvalQuestion.pooled_at.is_(None))
            .count()
        )

    if not targets:
        return {"code": 0, "data": {"pooled": 0, "failed": [], "skipped": 0}}

    try:
        vectors = await embedder.embed([query for _, query in targets])
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, f"向量化失败：{exc}") from exc

    size = _pool_size(top_k)
    pooled = 0
    failed: list[dict[str, str]] = []
    for (question_id, query), vector in zip(targets, vectors, strict=False):
        try:
            hits = await anyio.to_thread.run_sync(functools.partial(store.search_pool, vector, kb_ids, size, query))
            await anyio.to_thread.run_sync(functools.partial(_replace_candidates, question_id, hits))
            pooled += 1
        except Exception as exc:  # noqa: BLE001 - 单题失败不影响其他题
            logger.warning("评测候选池化失败 question=%s：%s", question_id, exc)
            failed.append({"id": question_id, "query": query, "error": str(exc)[:200]})

    return {
        "code": 0,
        "data": {
            "pooled": pooled,
            "failed": failed,
            "skipped": max(0, total_unpolled - len(targets)),
        },
    }


# ---------------------------------------------------------------- 标注


@router.patch("/candidates/{candidate_id}")
async def label_candidate(
    candidate_id: int,
    payload: CandidateLabelUpdate,
    identity: Identity = Depends(require_kb_manage),
):
    _require_db()

    def _work() -> dict[str, Any]:
        with session_scope() as db:
            row = db.get(AgentEvalCandidate, candidate_id)
            if row is None:
                raise HTTPException(status.HTTP_404_NOT_FOUND, "候选片段不存在")
            row.label = payload.label
            row.label_source = "human" if payload.label is not None else ""
            row.labeled_by = identity.user_id if payload.label is not None else None
            row.labeled_at = datetime.now() if payload.label is not None else None
            return _candidate_dict(row)

    return {"code": 0, "data": await anyio.to_thread.run_sync(_work)}


# ---------------------------------------------------------------- 评测快照


@router.post("/sets/{set_id}/runs")
async def create_run(set_id: str, identity: Identity = Depends(require_kb_manage)):
    _require_db()

    def _work() -> dict[str, Any]:
        with session_scope() as db:
            eval_set = _require_set(db, set_id)
            questions = (
                db.query(AgentEvalQuestion)
                .filter(AgentEvalQuestion.set_id == set_id)
                .order_by(AgentEvalQuestion.created_at, AgentEvalQuestion.id)
                .all()
            )
            grouped = _group_candidates(db, [question.id for question in questions])
            items = _question_summaries(questions, grouped, eval_set.top_k)
            metrics = eval_metrics.aggregate_metrics(items)
            if metrics["complete_count"] == 0:
                raise HTTPException(
                    status.HTTP_400_BAD_REQUEST,
                    "还没有标注完整的题：请把候选池里的片段全部标为 0/1/2 后再生成快照",
                )
            row = AgentEvalRun(
                id=uuid.uuid4().hex[:32],
                set_id=set_id,
                top_k=eval_set.top_k,
                question_count=metrics["question_count"],
                complete_count=metrics["complete_count"],
                precision=metrics["precision"],
                recall=metrics["recall"],
                mrr=metrics["mrr"],
                detail={
                    "set": _set_dict(eval_set),
                    "metrics": metrics,
                    "questions": items,
                },
                created_by=identity.user_id,
            )
            db.add(row)
            db.flush()
            return _run_dict(row)

    return {"code": 0, "data": await anyio.to_thread.run_sync(_work)}


@router.get("/sets/{set_id}/runs")
async def list_runs(set_id: str, _: Identity = Depends(require_kb_manage)):
    _require_db()

    def _work() -> list[dict[str, Any]]:
        with session_scope() as db:
            _require_set(db, set_id)
            rows = (
                db.query(AgentEvalRun)
                .filter(AgentEvalRun.set_id == set_id)
                .order_by(AgentEvalRun.created_at.desc())
                .limit(30)
                .all()
            )
            return [_run_dict(row) for row in rows]

    return {"code": 0, "data": await anyio.to_thread.run_sync(_work)}


@router.get("/runs/{run_id}")
async def get_run(run_id: str, _: Identity = Depends(require_kb_manage)):
    _require_db()

    def _work() -> dict[str, Any]:
        with session_scope() as db:
            row = db.get(AgentEvalRun, run_id)
            if row is None:
                raise HTTPException(status.HTTP_404_NOT_FOUND, "评测快照不存在")
            return _run_dict(row, include_detail=True)

    return {"code": 0, "data": await anyio.to_thread.run_sync(_work)}
