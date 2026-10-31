"""知识库接口：/api/v1/agent/kb/*。

权限模型（与前端呼应）：

- 普通员工（`agent:use`）：列知识库/文档、检索、上传文件。上传后进入**待审核**，
  不进索引、不参与 agent 检索；只能看到"已通过"的文档和自己上传的文档。
- 管理员：建/删知识库、分类管理、审核（通过即自动构建索引）、删除文档、
  单文档重建与整库重建索引、查看切块。

索引状态（status）与审核状态（review_status）是两个维度：
`waiting` 等审核；`pending/parsing/chunking/embedding` 构建中；`ready` 可检索；
`failed` 构建失败；`rejected` 已驳回。
"""

from __future__ import annotations

import logging
import uuid
from pathlib import Path
from typing import Literal

import anyio.to_thread
from fastapi import APIRouter, BackgroundTasks, Depends, File, Form, HTTPException, UploadFile, status
from pydantic import BaseModel, Field
from sqlalchemy import func

from app.auth import PERM_KB_MANAGE, Identity, require_agent_use, require_kb_manage
from app.config import settings
from app.db import db_available, session_scope
from app.kb import embedder, ingest, store
from app.models import AgentKb, AgentKbCategory, AgentKbChunk, AgentKbDoc

logger = logging.getLogger(__name__)
router = APIRouter()

MAX_UPLOAD_BYTES = 20 * 1024 * 1024
MAX_EDIT_CHARS = 200_000


class KbCreate(BaseModel):
    name: str = Field(min_length=1, max_length=60)
    description: str = ""


class KbUpdate(BaseModel):
    """管理员维护知识库：改名/描述。"""

    name: str | None = Field(default=None, min_length=1, max_length=60)
    description: str | None = Field(default=None, max_length=255)


class KbSearch(BaseModel):
    query: str = Field(min_length=1, max_length=500)
    kb_ids: list[str] = []
    top_k: int = 8


class CategoryCreate(BaseModel):
    name: str = Field(min_length=1, max_length=60)
    description: str = ""
    sort: int = 0


class CategoryUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=60)
    description: str | None = None
    sort: int | None = None


class DocUpdate(BaseModel):
    """编辑文档：标题、正文、对普通用户是否可见（可见性仅管理员可改）。"""

    title: str | None = Field(default=None, min_length=1, max_length=200)
    content: str | None = Field(default=None, max_length=MAX_EDIT_CHARS)
    visible: bool | None = None


class ReviewPayload(BaseModel):
    action: Literal["approve", "reject"]
    note: str = Field(default="", max_length=200)


def _require_db() -> None:
    if not db_available():
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "审计库不可用，知识库功能暂不可用")


def _doc_dict(row: AgentKbDoc) -> dict:
    return {
        "id": row.id,
        "kb_id": row.kb_id,
        "category_id": row.category_id,
        "title": row.title,
        "source": row.source,
        "format": row.format,
        "size_bytes": row.size_bytes,
        "status": row.status,
        "review_status": row.review_status,
        "review_note": row.review_note,
        "reviewed_by": row.reviewed_by,
        "reviewed_at": row.reviewed_at.isoformat() if row.reviewed_at else None,
        "chunk_count": row.chunk_count,
        "visible": bool(row.visible),
        "error": row.error,
        "created_by": row.created_by,
        "created_at": row.created_at.isoformat() if row.created_at else None,
    }


# ---------------------------------------------------------------- 知识库


def _list_kbs(identity: Identity) -> list[dict]:  # noqa: ARG001 - 保留签名，可见性已细化到文档
    # 可见性在「文档」级控制，知识库本身对所有人可见
    with session_scope() as db:
        rows = db.query(AgentKb).order_by(AgentKb.created_at.desc()).all()
        docs = db.query(AgentKbDoc.kb_id, AgentKbDoc.review_status, func.count(AgentKbDoc.id)).group_by(
            AgentKbDoc.kb_id, AgentKbDoc.review_status
        )
        counts: dict[str, dict[str, int]] = {}
        for kb_id, review_status, total in docs.all():
            bucket = counts.setdefault(kb_id, {"doc_count": 0, "approved_count": 0, "pending_review": 0})
            bucket["doc_count"] += int(total)
            if review_status == "approved":
                bucket["approved_count"] += int(total)
            elif review_status == "pending":
                bucket["pending_review"] += int(total)
        return [
            {
                "id": row.id,
                "name": row.name,
                "description": row.description,
                "embedding_model": row.embedding_model,
                "dim": row.dim,
                "doc_count": counts.get(row.id, {}).get("doc_count", 0),
                "approved_count": counts.get(row.id, {}).get("approved_count", 0),
                "pending_review": counts.get(row.id, {}).get("pending_review", 0),
                "created_at": row.created_at.isoformat() if row.created_at else None,
            }
            for row in rows
        ]


@router.get("")
async def list_kbs(identity: Identity = Depends(require_agent_use)):
    _require_db()
    return {"code": 0, "data": await anyio.to_thread.run_sync(lambda: _list_kbs(identity))}


@router.patch("/{kb_id}")
async def update_kb(kb_id: str, payload: KbUpdate, _: Identity = Depends(require_kb_manage)):
    """管理员维护知识库：改名/描述/是否对普通用户可见。"""
    _require_db()

    def _work() -> dict:
        with session_scope() as db:
            row = db.get(AgentKb, kb_id)
            if row is None:
                raise HTTPException(status.HTTP_404_NOT_FOUND, "知识库不存在")
            if payload.name is not None:
                row.name = payload.name.strip()
            if payload.description is not None:
                row.description = payload.description.strip()
            return {
                "id": row.id,
                "name": row.name,
                "description": row.description,
            }

    return {"code": 0, "data": await anyio.to_thread.run_sync(_work)}


@router.post("")
async def create_kb(payload: KbCreate, identity: Identity = Depends(require_kb_manage)):
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
async def delete_kb(kb_id: str, _: Identity = Depends(require_kb_manage)):
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


# ---------------------------------------------------------------- 分类


def _list_categories() -> list[dict]:
    with session_scope() as db:
        counts = dict(
            db.query(AgentKbDoc.category_id, func.count(AgentKbDoc.id))
            .filter(AgentKbDoc.category_id.isnot(None))
            .group_by(AgentKbDoc.category_id)
            .all()
        )
        rows = db.query(AgentKbCategory).order_by(AgentKbCategory.sort, AgentKbCategory.name).all()
        return [
            {
                "id": row.id,
                "name": row.name,
                "description": row.description,
                "sort": row.sort,
                "doc_count": int(counts.get(row.id, 0)),
                "created_at": row.created_at.isoformat() if row.created_at else None,
            }
            for row in rows
        ]


@router.get("/categories")
async def list_categories(_: Identity = Depends(require_agent_use)):
    _require_db()
    return {"code": 0, "data": await anyio.to_thread.run_sync(_list_categories)}


@router.post("/categories")
async def create_category(payload: CategoryCreate, identity: Identity = Depends(require_kb_manage)):
    _require_db()
    category_id = uuid.uuid4().hex[:32]

    def _work() -> None:
        with session_scope() as db:
            exists = db.query(AgentKbCategory).filter(AgentKbCategory.name == payload.name.strip()).first()
            if exists is not None:
                raise HTTPException(status.HTTP_409_CONFLICT, "分类名已存在")
            db.add(
                AgentKbCategory(
                    id=category_id,
                    name=payload.name.strip(),
                    description=payload.description.strip(),
                    sort=payload.sort,
                    created_by=identity.user_id,
                )
            )

    await anyio.to_thread.run_sync(_work)
    return {"code": 0, "data": {"id": category_id}}


@router.patch("/categories/{category_id}")
async def update_category(category_id: str, payload: CategoryUpdate, _: Identity = Depends(require_kb_manage)):
    _require_db()

    def _work() -> None:
        with session_scope() as db:
            row = db.get(AgentKbCategory, category_id)
            if row is None:
                raise HTTPException(status.HTTP_404_NOT_FOUND, "分类不存在")
            if payload.name is not None:
                duplicated = (
                    db.query(AgentKbCategory)
                    .filter(AgentKbCategory.name == payload.name.strip(), AgentKbCategory.id != category_id)
                    .first()
                )
                if duplicated is not None:
                    raise HTTPException(status.HTTP_409_CONFLICT, "分类名已存在")
                row.name = payload.name.strip()
            if payload.description is not None:
                row.description = payload.description.strip()
            if payload.sort is not None:
                row.sort = payload.sort

    await anyio.to_thread.run_sync(_work)
    return {"code": 0, "msg": "updated"}


@router.delete("/categories/{category_id}")
async def delete_category(category_id: str, _: Identity = Depends(require_kb_manage)):
    _require_db()

    def _work() -> None:
        with session_scope() as db:
            row = db.get(AgentKbCategory, category_id)
            if row is None:
                raise HTTPException(status.HTTP_404_NOT_FOUND, "分类不存在")
            # 分类删除不删文档，只把文档的归类清空，避免误删资料
            db.query(AgentKbDoc).filter(AgentKbDoc.category_id == category_id).update({AgentKbDoc.category_id: None})
            db.delete(row)

    await anyio.to_thread.run_sync(_work)
    return {"code": 0, "msg": "deleted"}


# ---------------------------------------------------------------- 文档


def _kb_row(db, kb_id: str, identity: Identity) -> AgentKb:  # noqa: ARG001
    row = db.get(AgentKb, kb_id)
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "知识库不存在")
    return row


def _doc_row(db, kb_id: str, doc_id: str, identity: Identity) -> AgentKbDoc:
    """取文档；对普通用户隐藏的文件按不存在处理（不泄漏其存在）。"""
    row = db.get(AgentKbDoc, doc_id)
    if row is None or row.kb_id != kb_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "文档不存在")
    if not row.visible and not identity.has_permission(PERM_KB_MANAGE):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "文档不存在")
    return row


def _list_docs(kb_id: str, identity: Identity, review: str = "") -> list[dict]:
    """所有用户都能看到知识库里的文档（含待审核/被驳回）；隐藏文件只有管理员看得到。"""
    with session_scope() as db:
        _kb_row(db, kb_id, identity)
        query = db.query(AgentKbDoc).filter(AgentKbDoc.kb_id == kb_id)
        if not identity.has_permission(PERM_KB_MANAGE):
            query = query.filter(AgentKbDoc.visible == 1)
        if review:
            query = query.filter(AgentKbDoc.review_status == review)
        rows = query.order_by(AgentKbDoc.created_at.desc()).all()
        return [_doc_dict(row) for row in rows]


def _doc_detail(kb_id: str, doc_id: str, identity: Identity) -> dict:
    with session_scope() as db:
        _kb_row(db, kb_id, identity)
        row = _doc_row(db, kb_id, doc_id, identity)
        data = _doc_dict(row)
        data["summary"] = row.error or ""
        data["content"] = row.content or ""
        return data


@router.get("/{kb_id}/docs")
async def list_docs(
    kb_id: str,
    review: str = "",
    identity: Identity = Depends(require_agent_use),
):
    _require_db()
    return {"code": 0, "data": await anyio.to_thread.run_sync(lambda: _list_docs(kb_id, identity, review))}


@router.get("/{kb_id}/docs/{doc_id}")
async def doc_detail(kb_id: str, doc_id: str, identity: Identity = Depends(require_agent_use)):
    """文档详情：所有用户都能看（所在知识库对用户隐藏时按 404）。"""
    _require_db()
    return {"code": 0, "data": await anyio.to_thread.run_sync(lambda: _doc_detail(kb_id, doc_id, identity))}


@router.put("/{kb_id}/docs/{doc_id}")
async def update_doc(
    kb_id: str,
    doc_id: str,
    payload: DocUpdate,
    background: BackgroundTasks,
    identity: Identity = Depends(require_agent_use),
):
    """编辑文档：上传者或知识库管理员可改标题/正文。

    正文改动会清掉旧切块并重新入库；未审核通过的文件改完回到"待审核"。
    """
    _require_db()

    can_manage = identity.has_permission(PERM_KB_MANAGE)
    if payload.visible is not None and not can_manage:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "只有知识库管理员能设置文件可见性")

    def _work() -> bool:
        with session_scope() as db:
            _kb_row(db, kb_id, identity)
            row = _doc_row(db, kb_id, doc_id, identity)
            if row.created_by != identity.user_id and not can_manage:
                raise HTTPException(status.HTTP_403_FORBIDDEN, "只能编辑自己上传的文档")

            if payload.visible is not None:
                row.visible = 1 if payload.visible else 0
            content_changed = payload.content is not None and payload.content != (row.content or "")
            if payload.title is not None:
                row.title = payload.title.strip()
            if content_changed:
                row.content = payload.content
                row.size_bytes = len((payload.content or "").encode("utf-8"))
                row.error = ""
                row.chunk_count = 0
                # 已审核的走后台重建，未审核的回到待审核（等管理员再看一遍）
                row.status = "pending" if row.review_status == "approved" else "waiting"
            return content_changed

    content_changed = await anyio.to_thread.run_sync(_work)
    if content_changed:
        await anyio.to_thread.run_sync(ingest.purge_index_sync, doc_id)

        def _approved() -> bool:
            with session_scope() as db:
                row = db.get(AgentKbDoc, doc_id)
                return bool(row and row.review_status == "approved")

        if await anyio.to_thread.run_sync(_approved):
            background.add_task(ingest.ingest_document, doc_id)
    return {"code": 0, "data": await anyio.to_thread.run_sync(lambda: _doc_detail(kb_id, doc_id, identity))}


@router.post("/{kb_id}/docs")
async def upload_doc(
    kb_id: str,
    file: UploadFile = File(...),
    title: str = Form(""),
    category_id: str = Form(""),
    identity: Identity = Depends(require_agent_use),
):
    """上传即进入待审核：不建索引，管理员通过后才会进知识库。"""
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
        with session_scope() as db:
            if db.get(AgentKb, kb_id) is None:
                raise HTTPException(status.HTTP_404_NOT_FOUND, "知识库不存在")
            if category_id and db.get(AgentKbCategory, category_id) is None:
                raise HTTPException(status.HTTP_400_BAD_REQUEST, "分类不存在")
            ingest.save_upload(doc_id, data)
            # 上传时就解析出正文，方便详情查看与在线编辑；解析失败不影响上传（入库时再试）
            try:
                content = ingest.extract_text(data, suffix)
            except Exception:  # noqa: BLE001
                logger.warning("上传时解析正文失败，入库时重试：doc=%s", doc_id)
                content = None
            db.add(
                AgentKbDoc(
                    id=doc_id,
                    kb_id=kb_id,
                    category_id=category_id or None,
                    title=doc_title,
                    content=content,
                    source=file.filename or "",
                    format=suffix,
                    size_bytes=len(data),
                    status="waiting",
                    review_status="pending",
                    created_by=identity.user_id,
                )
            )

    await anyio.to_thread.run_sync(_work)
    return {"code": 0, "data": {"id": doc_id, "status": "waiting", "review_status": "pending"}}


@router.get("/{kb_id}/review-queue")
async def review_queue(kb_id: str, identity: Identity = Depends(require_kb_manage)):
    """待审核队列（管理员）：所有员工上传、还没处理的文档。"""
    _require_db()
    return {
        "code": 0,
        "data": await anyio.to_thread.run_sync(lambda: _list_docs(kb_id, identity, "pending")),
    }


@router.post("/{kb_id}/docs/{doc_id}/review")
async def review_doc(
    kb_id: str,
    doc_id: str,
    payload: ReviewPayload,
    background: BackgroundTasks,
    identity: Identity = Depends(require_kb_manage),
):
    _require_db()

    def _work() -> str:
        with session_scope() as db:
            doc = db.get(AgentKbDoc, doc_id)
            if doc is None or doc.kb_id != kb_id:
                raise HTTPException(status.HTTP_404_NOT_FOUND, "文档不存在")
            doc.review_status = "approved" if payload.action == "approve" else "rejected"
            doc.reviewed_by = identity.user_id
            doc.reviewed_at = func.now()
            doc.review_note = payload.note
            if payload.action == "approve":
                doc.status = "pending"
                doc.error = ""
            else:
                doc.status = "rejected"
                doc.chunk_count = 0
            return doc.review_status

    result = await anyio.to_thread.run_sync(_work)
    if result == "approved":
        background.add_task(ingest.ingest_document, doc_id)
    elif result == "rejected":
        await ingest.purge_index(doc_id)
    return {"code": 0, "data": {"id": doc_id, "review_status": result}}


@router.post("/{kb_id}/docs/{doc_id}/rebuild")
async def rebuild_doc(kb_id: str, doc_id: str, background: BackgroundTasks, _: Identity = Depends(require_kb_manage)):
    _require_db()

    def _mark() -> None:
        with session_scope() as db:
            doc = db.get(AgentKbDoc, doc_id)
            if doc is None or doc.kb_id != kb_id:
                raise HTTPException(status.HTTP_404_NOT_FOUND, "文档不存在")
            if doc.review_status != "approved":
                raise HTTPException(status.HTTP_400_BAD_REQUEST, "只有审核通过的文档才能构建索引")
            doc.status = "pending"
            doc.error = ""

    await anyio.to_thread.run_sync(_mark)
    background.add_task(ingest.ingest_document, doc_id)
    return {"code": 0, "data": {"id": doc_id, "status": "pending"}}


@router.post("/{kb_id}/reindex")
async def reindex_kb(kb_id: str, background: BackgroundTasks, _: Identity = Depends(require_kb_manage)):
    """整库重建索引：管理员按钮。只重建"已通过"的文档。"""
    _require_db()

    def _mark() -> list[str]:
        with session_scope() as db:
            if db.get(AgentKb, kb_id) is None:
                raise HTTPException(status.HTTP_404_NOT_FOUND, "知识库不存在")
            rows = db.query(AgentKbDoc).filter(AgentKbDoc.kb_id == kb_id, AgentKbDoc.review_status == "approved").all()
            for doc in rows:
                doc.status = "pending"
                doc.error = ""
            return [doc.id for doc in rows]

    doc_ids = await anyio.to_thread.run_sync(_mark)
    for doc_id in doc_ids:
        background.add_task(ingest.ingest_document, doc_id)
    return {"code": 0, "data": {"queued": len(doc_ids)}}


@router.delete("/{kb_id}/docs/{doc_id}")
async def delete_doc(kb_id: str, doc_id: str, _: Identity = Depends(require_kb_manage)):
    _require_db()
    await ingest.delete_document(doc_id)
    return {"code": 0, "msg": "deleted"}


def _doc_chunks(doc_id: str, limit: int = 200) -> list[dict]:
    with session_scope() as db:
        rows = (
            db.query(AgentKbChunk).filter(AgentKbChunk.doc_id == doc_id).order_by(AgentKbChunk.seq).limit(limit).all()
        )
        return [{"id": r.id, "seq": r.seq, "text": r.text, "tokens": r.tokens} for r in rows]


@router.get("/{kb_id}/docs/{doc_id}/chunks")
async def doc_chunks(kb_id: str, doc_id: str, identity: Identity = Depends(require_agent_use)):
    """切块预览：详情页正文用；对普通用户隐藏的文件按 404。"""
    _require_db()

    def _work() -> list[dict]:
        with session_scope() as db:
            _kb_row(db, kb_id, identity)
            _doc_row(db, kb_id, doc_id, identity)
        return _doc_chunks(doc_id)

    return {"code": 0, "data": await anyio.to_thread.run_sync(_work)}


@router.post("/search")
async def search(payload: KbSearch, _: Identity = Depends(require_agent_use)):
    vector = (await embedder.embed([payload.query]))[0]
    hits = await anyio.to_thread.run_sync(
        lambda: store.search(vector, payload.kb_ids or None, payload.top_k, payload.query)
    )
    return {"code": 0, "data": hits}
