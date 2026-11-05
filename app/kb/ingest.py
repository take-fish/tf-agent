"""知识库入库：解析 → 切块 → 向量化 → 写 ES + MySQL。"""

from __future__ import annotations

import io
import logging
import re
import uuid
from pathlib import Path

import anyio.to_thread

from app import nacos
from app.config import settings
from app.db import session_scope
from app.kb import embedder, store
from app.models import AgentKbChunk, AgentKbDoc

logger = logging.getLogger(__name__)

SUPPORTED = {"txt", "md", "markdown", "pdf", "docx"}


def extract_text(data: bytes, fmt: str) -> str:
    fmt = (fmt or "txt").lower()
    if fmt in {"txt", "md", "markdown"}:
        return data.decode("utf-8", "replace")
    if fmt == "pdf":
        from pypdf import PdfReader

        reader = PdfReader(io.BytesIO(data))
        return "\n\n".join((page.extract_text() or "") for page in reader.pages)
    if fmt == "docx":
        from docx import Document

        document = Document(io.BytesIO(data))
        return "\n\n".join(p.text for p in document.paragraphs if p.text.strip())
    raise ValueError(f"不支持的格式：{fmt}")


_HEADING = re.compile(r"^(#{1,6})\s+\S")
# H1/H2 视为章节边界；H3 及以下留在所属章节里，避免父子标题被切成碎片
_SECTION_LEVEL = 2


def _split_sections(text: str) -> list[str]:
    """按 markdown 标题切章节，标题与正文留在一起（语义更完整）。

    只在 H1/H2 处开新段，H3+ 子标题继续留在父章节内；否则
    "## 2. 游戏清单" 后面紧跟 "### 2.1 五子棋" 时，父标题会变成
    只有一个标题、没有正文的碎片，检索命中后模型拿不到内容。
    只有一个标题、没有正文的段会并入下一段（通常是文档标题）。
    """
    sections: list[str] = []
    current: list[str] = []
    for line in text.split("\n"):
        match = _HEADING.match(line)
        heading_level = len(match.group(1)) if match else 0
        if match and heading_level <= _SECTION_LEVEL and any(x.strip() for x in current):
            sections.append("\n".join(current).strip())
            current = [line]
        else:
            current.append(line)
    if any(x.strip() for x in current):
        sections.append("\n".join(current).strip())

    merged: list[str] = []
    pending = ""
    for section in [s for s in sections if s]:
        if pending:
            section = f"{pending}\n\n{section}"
            pending = ""
        lines = [line for line in section.splitlines() if line.strip()]
        if len(lines) == 1 and _HEADING.match(lines[0]):
            pending = section
        else:
            merged.append(section)
    if pending:
        if merged:
            merged[-1] = f"{merged[-1]}\n\n{pending}"
        else:
            merged.append(pending)
    return merged


def _pack(block: str, size: int, overlap: int) -> list[str]:
    """把一段文本按段落打包到 size 以内，超长段落带 overlap 硬切。"""
    paragraphs = [p.strip() for p in re.split(r"\n\s*\n", block) if p.strip()]
    chunks: list[str] = []
    buffer = ""
    for paragraph in paragraphs:
        if len(paragraph) > size:
            # 超长段落不要单独冲掉前面的短标题，否则会出现"只有标题"的碎片
            if buffer:
                paragraph = f"{buffer}\n\n{paragraph}"
                buffer = ""
            start = 0
            while start < len(paragraph):
                chunks.append(paragraph[start : start + size])
                start += max(1, size - overlap)
            continue
        if len(buffer) + len(paragraph) + 2 <= size:
            buffer = f"{buffer}\n\n{paragraph}" if buffer else paragraph
        else:
            chunks.append(buffer)
            buffer = paragraph
    if buffer:
        chunks.append(buffer)
    return [c.strip() for c in chunks if c.strip()]


def chunk_text(text: str, size: int | None = None, overlap: int | None = None) -> list[str]:
    size = size or int(nacos.kb_param("chunk-size", settings.chunk_size))
    overlap = overlap or int(nacos.kb_param("chunk-overlap", settings.chunk_overlap))
    text = text.replace("\r\n", "\n").strip()
    if not text:
        return []

    chunks: list[str] = []
    for section in _split_sections(text):
        if len(section) <= size:
            chunks.append(section)
        else:
            chunks.extend(_pack(section, size, overlap))
    return chunks


def save_upload(doc_id: str, data: bytes) -> Path:
    directory = Path(settings.upload_dir)
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{doc_id}.bin"
    path.write_bytes(data)
    return path


def _set_status(doc_id: str, status: str, *, error: str = "", chunk_count: int | None = None) -> None:
    try:
        with session_scope() as db:
            doc = db.get(AgentKbDoc, doc_id)
            if doc is None:
                return
            doc.status = status
            doc.error = error[:500]
            if chunk_count is not None:
                doc.chunk_count = chunk_count
    except Exception as exc:  # noqa: BLE001
        logger.warning("更新文档状态失败 %s: %s", doc_id, exc)


def ingest_document_sync(doc_id: str) -> int:
    """同步入库（在后台线程里跑）。返回 chunk 数。

    只有管理员审核通过（review_status=approved）的文档才允许进索引；
    未审核/已驳回的文档即使被误触发也保持 waiting/rejected，不污染检索结果。
    """
    with session_scope() as db:
        doc = db.get(AgentKbDoc, doc_id)
        if doc is None:
            raise ValueError(f"文档不存在：{doc_id}")
        kb_id, title, fmt = doc.kb_id, doc.title, doc.format
        content = doc.content or ""
        if doc.review_status != "approved":
            _set_status(doc_id, "waiting" if doc.review_status == "pending" else "rejected")
            logger.info("文档未通过审核，跳过入库 %s（review_status=%s）", doc_id, doc.review_status)
            return 0

    _set_status(doc_id, "parsing")
    if content.strip():
        # 优先用库里的正文（在线编辑后的版本），不再回头解析原始文件
        text = content
    else:
        path = Path(settings.upload_dir) / f"{doc_id}.bin"
        if not path.exists():
            _set_status(doc_id, "failed", error="原始文件丢失，请重新上传")
            raise FileNotFoundError(str(path))
        text = extract_text(path.read_bytes(), fmt)
        # 首次解析的正文落库，之后编辑/重建都走 content
        with session_scope() as db:
            row = db.get(AgentKbDoc, doc_id)
            if row is not None and not row.content:
                row.content = text

    _set_status(doc_id, "chunking")
    pieces = chunk_text(text)
    if not pieces:
        _set_status(doc_id, "failed", error="解析后没有可索引的文本")
        return 0

    # 先清旧数据，保证重建幂等
    store.delete_doc(doc_id)
    with session_scope() as db:
        db.query(AgentKbChunk).filter(AgentKbChunk.doc_id == doc_id).delete()

    rows = [
        {
            "id": uuid.uuid4().hex[:32],
            "doc_id": doc_id,
            "kb_id": kb_id,
            "seq": idx,
            "text": piece,
            "tokens": max(1, len(piece) // 2),
        }
        for idx, piece in enumerate(pieces)
    ]
    with session_scope() as db:
        db.bulk_insert_mappings(AgentKbChunk, rows)

    _set_status(doc_id, "embedding")
    vectors = embedder.embed_sync([r["text"] for r in rows])
    store.upsert_chunks(kb_id, doc_id, title, rows, vectors)

    _set_status(doc_id, "ready", chunk_count=len(rows))
    logger.info("文档入库完成 %s（%s 个 chunk）", doc_id, len(rows))
    return len(rows)


async def ingest_document(doc_id: str) -> None:
    try:
        await anyio.to_thread.run_sync(ingest_document_sync, doc_id)
    except Exception as exc:  # noqa: BLE001
        logger.exception("文档入库失败 %s", doc_id)
        _set_status(doc_id, "failed", error=str(exc))


def purge_index_sync(doc_id: str) -> None:
    """只删向量和切块，保留文档记录（驳回/取消发布时用）。"""
    store.delete_doc(doc_id)
    with session_scope() as db:
        db.query(AgentKbChunk).filter(AgentKbChunk.doc_id == doc_id).delete()
        doc = db.get(AgentKbDoc, doc_id)
        if doc is not None:
            doc.chunk_count = 0


async def purge_index(doc_id: str) -> None:
    await anyio.to_thread.run_sync(purge_index_sync, doc_id)


async def delete_document(doc_id: str) -> None:
    def _work() -> None:
        store.delete_doc(doc_id)
        with session_scope() as db:
            db.query(AgentKbChunk).filter(AgentKbChunk.doc_id == doc_id).delete()
            doc = db.get(AgentKbDoc, doc_id)
            if doc is not None:
                db.delete(doc)

    await anyio.to_thread.run_sync(_work)
    path = Path(settings.upload_dir) / f"{doc_id}.bin"
    if path.exists():
        path.unlink(missing_ok=True)
