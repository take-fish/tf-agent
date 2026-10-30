"""知识库管理接口：/api/v1/agent/kb/*。"""

from __future__ import annotations

import logging
import uuid
from pathlib import Path

import anyio.to_thread
from fastapi import APIRouter, BackgroundTasks, Depends, File, Form, HTTPException, UploadFile, status
from pydantic import BaseModel, Field
from sqlalchemy import func

from app.auth import Identity, require_kb_write
from app.config import settings
from app.db import db_available, session_scope
from app.kb import embedder, ingest, store
from app.models import AgentKb, AgentKbChunk, AgentKbDoc

logger = logging.getLogger(__name__)
router = APIRouter()

MAX_UPLOAD_BYTES = 20 * 1024 * 1024


class KbCreate(BaseModel):
    name: str = Field(min_length=1, max_length=60)
    description: str = ""


class KbSearch(BaseModel):
    query: str = Field(min_length=1, max_length=500)
    kb_ids: list[str] = []
    top_k: int = 8


def _require_db() -> None:
    if not db_available():
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "审计库不可用，知识库功能暂不可用")


def _list_kbs() -> list[dict]:
    with session_scope() as db:
        rows = db.query(AgentKb).order_by(AgentKb.created_at.desc()).all()
        counts = dict(
            db.query(AgentKbDoc.kb_id, func.count(AgentKbDoc.id)).group_by(AgentKbDoc.kb_id).all()
        )
        return [
            {
                "id": row.id,
                "name": row.name,
                "description": row.description,
                "embedding_model": row.embedding_model,
                "dim": row.dim,
                "doc_count": int(counts.get(row.id, 0)),
                "created_at": row.created_at.isoformat() if row.created_at else None,
            }
            for row in rows
        ]


@router.get("")
async def list_kbs(_: Identity = Depends(require_kb_write)):
    _require_db()
    return {"code": 0, "data": await anyio.to_thread.run_sync(_list_kbs)}


@router.post("")
async def create_kb(payload: KbCreate, identity: Identity = Depends(require_kb_write)):
    _require_db()
    kb_id = uuid.uuid4().hex[:32]

    def _work() -> str:
        with session_scope() as db:
            db.add(
                AgentKb(
                    id=kb_id,
                    name=payload.name.strip(),
                    description=payload.description.strip(),
                    embedding_model=settings.embed_model,
                    dim=settings.embed_dim,
                    created_by=identity.user_id,
                )
            )
        return kb_id

    await anyio.to_thread.run_sync(_work)
    return {"code": 0, "data": {"id": kb_id}}


@router.delete("/{kb_id}")
async def delete_kb(kb_id: str, _: Identity = Depends(require_kb_write)):
    _require_db()

    def _work() -> None:
        store.delete_kb(kb_id)
        with session_scope() as db:
            db.query(AgentKbChunk).filter(AgentKbChunk.kb_id == kb_id).delete()
            db.query(AgentKbDoc).filter(AgentKbDoc.kb_id == kb_id).delete()
            row = db.get(AgentKb, kb_id)
            if row is not None:
                db.delete(row)

    await anyio.to_thread.run_sync(_work)
    return {"code": 0, "msg": "deleted"}


def _list_docs(kb_id: str) -> list[dict]:
    with session_scope() as db:
        rows = (
            db.query(AgentKbDoc)
            .filter(AgentKbDoc.kb_id == kb_id)
            .order_by(AgentKbDoc.created_at.desc())
            .all()
        )
        return [
            {
                "id": row.id,
                "title": row.title,
                "format": row.format,
                "size_bytes": row.size_bytes,
                "status": row.status,
                "chunk_count": row.chunk_count,
                "error": row.error,
                "created_at": row.created_at.isoformat() if row.created_at else None,
            }
            for row in rows
        ]


@router.get("/{kb_id}/docs")
async def list_docs(kb_id: str, _: Identity = Depends(require_kb_write)):
    _require_db()
    return {"code": 0, "data": await anyio.to_thread.run_sync(lambda: _list_docs(kb_id))}


@router.post("/{kb_id}/docs")
async def upload_doc(
    kb_id: str,
    background: BackgroundTasks,
    file: UploadFile = File(...),
    title: str = Form(""),
    identity: Identity = Depends(require_kb_write),
):
    _require_db()
    suffix = Path(file.filename or "upload.txt").suffix.lstrip(".").lower() or "txt"
    if suffix not in ingest.SUPPORTED:
        supported = ", ".join(sorted(ingest.SUPPORTED))
        raise HTTPException(status.HTTP_400_BAD_REQUEST, f"不支持的格式：{suffix}，仅支持 {supported}")

    data = await file.read()
    if len(data) > MAX_UPLOAD_BYTES:
        raise HTTPException(status.HTTP_413_REQUEST_ENTITY_TOO_LARGE, "文件超过 20MB")

    doc_id = uuid.uuid4().hex[:32]
    doc_title = (title or Path(file.filename or "").stem or "未命名文档").strip()[:200]

    def _work() -> None:
        ingest.save_upload(doc_id, data)
        with session_scope() as db:
            db.add(
                AgentKbDoc(
                    id=doc_id,
                    kb_id=kb_id,
                    title=doc_title,
                    source=file.filename or "",
                    format=suffix,
                    size_bytes=len(data),
                    status="pending",
                    created_by=identity.user_id,
                )
            )

    await anyio.to_thread.run_sync(_work)
    background.add_task(ingest.ingest_document, doc_id)
    return {"code": 0, "data": {"id": doc_id, "status": "pending"}}


@router.post("/{kb_id}/docs/{doc_id}/rebuild")
async def rebuild_doc(kb_id: str, doc_id: str, background: BackgroundTasks, _: Identity = Depends(require_kb_write)):
    _require_db()

    def _mark() -> None:
        with session_scope() as db:
            doc = db.get(AgentKbDoc, doc_id)
            if doc is None or doc.kb_id != kb_id:
                raise HTTPException(status.HTTP_404_NOT_FOUND, "文档不存在")
            doc.status = "pending"
            doc.error = ""

    await anyio.to_thread.run_sync(_mark)
    background.add_task(ingest.ingest_document, doc_id)
    return {"code": 0, "data": {"id": doc_id, "status": "pending"}}


@router.delete("/{kb_id}/docs/{doc_id}")
async def delete_doc(kb_id: str, doc_id: str, _: Identity = Depends(require_kb_write)):
    _require_db()
    await ingest.delete_document(doc_id)
    return {"code": 0, "msg": "deleted"}


def _doc_chunks(doc_id: str, limit: int = 200) -> list[dict]:
    with session_scope() as db:
        rows = (
            db.query(AgentKbChunk)
            .filter(AgentKbChunk.doc_id == doc_id)
            .order_by(AgentKbChunk.seq)
            .limit(limit)
            .all()
        )
        return [{"id": r.id, "seq": r.seq, "text": r.text, "tokens": r.tokens} for r in rows]


@router.get("/{kb_id}/docs/{doc_id}/chunks")
async def doc_chunks(kb_id: str, doc_id: str, _: Identity = Depends(require_kb_write)):
    _require_db()
    return {"code": 0, "data": await anyio.to_thread.run_sync(lambda: _doc_chunks(doc_id))}


@router.post("/search")
async def search(payload: KbSearch, _: Identity = Depends(require_kb_write)):
    vector = (await embedder.embed([payload.query]))[0]
    hits = await anyio.to_thread.run_sync(
        lambda: store.search(vector, payload.kb_ids or None, payload.top_k)
    )
    return {"code": 0, "data": hits}
