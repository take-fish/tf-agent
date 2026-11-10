"""把单块命中拼成连续上下文。

ES 负责"找得到"，这里负责"给得全"：

- neighbor：命中块前后各补 1 块，解决清单/表格被切在两块里的问题；
- section：命中块所在章节整节回捞（需要 V11 的 section_seq 元数据）；
- off：保持原样，只返回命中块。

扩展只发生在结果打包层，``data["chunks"]`` 仍是原始命中，
召回率/精确率等指标口径不变。数据库不可用或老索引没有章节字段时，
自动退回原始块，检索本身不受影响。
"""

from __future__ import annotations

import logging
from typing import Any

from app import nacos
from app.config import settings
from app.db import db_available, session_scope
from app.models import AgentKbChunk

logger = logging.getLogger(__name__)

_MODES = {"off", "neighbor", "section"}


def _param(name: str, default: Any) -> Any:
    return nacos.kb_param(name, default)


def _mode() -> str:
    raw = str(_param("expand-mode", settings.kb_expand_mode) or "").strip().lower()
    return raw if raw in _MODES else "off"


def _int_param(name: str, default: int, *, minimum: int = 1) -> int:
    try:
        return max(minimum, int(_param(name, default)))
    except (TypeError, ValueError):
        return max(minimum, default)


def _trim(text: str, limit: int) -> str:
    """按行截断，避免把一句话/一行表格从中间切掉。"""
    text = (text or "").strip()
    if len(text) <= limit:
        return text
    cut = text[:limit]
    newline = cut.rfind("\n")
    if newline >= limit * 0.6:
        cut = cut[:newline]
    return cut.rstrip()


def _join(previous: str, following: str, max_overlap: int = 300) -> str:
    """拼接相邻块，去掉切块时的重叠部分。"""
    if not previous:
        return following
    if not following:
        return previous
    limit = min(len(previous), len(following), max_overlap)
    for size in range(limit, 0, -1):
        if previous.endswith(following[:size]):
            return previous + following[size:]
    return f"{previous}\n{following}"


def _row_dict(row: AgentKbChunk) -> dict[str, Any]:
    return {
        "seq": int(row.seq or 0),
        "text": row.text or "",
        "section": row.section or "",
        "section_seq": int(row.section_seq or 0),
    }


def _load_neighbors(hit: dict[str, Any], radius: int) -> list[dict[str, Any]]:
    seq = int(hit.get("seq") or 0)
    start = max(0, seq - radius)
    end = seq + radius
    with session_scope() as db:
        rows = (
            db.query(AgentKbChunk)
            .filter(
                AgentKbChunk.doc_id == str(hit.get("doc_id") or ""),
                AgentKbChunk.kb_id == str(hit.get("kb_id") or ""),
                AgentKbChunk.seq >= start,
                AgentKbChunk.seq <= end,
            )
            .order_by(AgentKbChunk.seq)
            .all()
        )
        return [_row_dict(row) for row in rows]


def _load_section(hit: dict[str, Any], max_chunks: int) -> list[dict[str, Any]]:
    section_seq = hit.get("section_seq")
    if section_seq is None:
        return []
    with session_scope() as db:
        rows = (
            db.query(AgentKbChunk)
            .filter(
                AgentKbChunk.doc_id == str(hit.get("doc_id") or ""),
                AgentKbChunk.kb_id == str(hit.get("kb_id") or ""),
                AgentKbChunk.section_seq == int(section_seq),
            )
            .order_by(AgentKbChunk.seq)
            .limit(max_chunks)
            .all()
        )
        return [_row_dict(row) for row in rows]


def raw_blocks(hits: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [_raw_block(hit) for hit in (hits or []) if hit]


def _raw_block(hit: dict[str, Any]) -> dict[str, Any]:
    seq = int(hit.get("seq") or 0)
    return {
        "title": str(hit.get("title") or ""),
        "doc_id": str(hit.get("doc_id") or ""),
        "kb_id": str(hit.get("kb_id") or ""),
        "seq_start": seq,
        "seq_end": seq,
        "text": str(hit.get("text") or ""),
        "section": str(hit.get("section") or ""),
        "section_seq": hit.get("section_seq"),
        "expanded": False,
        "score": float(hit.get("score") or 0.0),
    }


def _expanded_block(
    hit: dict[str, Any],
    rows: list[dict[str, Any]],
    *,
    limit: int,
    section_seq: int | None = None,
) -> dict[str, Any] | None:
    if not rows:
        return None
    ordered = sorted(rows, key=lambda row: int(row.get("seq") or 0))
    text = ""
    for row in ordered:
        text = _join(text, str(row.get("text") or ""))
    text = _trim(text, limit)
    if not text:
        return None
    section = next((str(row.get("section") or "") for row in ordered if row.get("section")), "")
    if not section:
        section = str(hit.get("section") or "")
    return {
        "title": str(hit.get("title") or ""),
        "doc_id": str(hit.get("doc_id") or ""),
        "kb_id": str(hit.get("kb_id") or ""),
        "seq_start": int(ordered[0].get("seq") or 0),
        "seq_end": int(ordered[-1].get("seq") or 0),
        "text": text,
        "section": section,
        "section_seq": section_seq if section_seq is not None else ordered[0].get("section_seq"),
        "expanded": True,
        "score": float(hit.get("score") or 0.0),
    }


def _covered(seq: int, ranges: list[tuple[int, int]]) -> bool:
    return any(start <= seq <= end for start, end in ranges)


def expand_hits(hits: list[dict[str, Any]], *, mode: str | None = None) -> list[dict[str, Any]]:
    """把命中块扩成上下文块；任何异常都退回原始块。"""
    selected = [hit for hit in (hits or []) if hit]
    if not selected:
        return []

    active = mode or _mode()
    if active == "off":
        return [_raw_block(hit) for hit in selected]

    top = _int_param("expand-top", settings.kb_expand_top)
    radius = min(3, _int_param("expand-radius", settings.kb_expand_radius))
    if active == "section":
        block_chars = _int_param("section-chars", settings.kb_section_block_chars)
        total_chars = _int_param("section-total-chars", settings.kb_section_total_chars)
        max_chunks = _int_param("section-max-chunks", settings.kb_section_max_chunks)
    else:
        block_chars = _int_param("expand-block-chars", settings.kb_expand_block_chars)
        total_chars = _int_param("expand-total-chars", settings.kb_expand_total_chars)
        max_chunks = radius * 2 + 1

    blocks: list[dict[str, Any]] = []
    covered: list[tuple[int, int]] = []
    used = 0

    for index, hit in enumerate(selected):
        seq = int(hit.get("seq") or 0)
        if index < top and not _covered(seq, covered):
            rows: list[dict[str, Any]] = []
            section_seq: int | None = None
            try:
                if not db_available():
                    rows = []
                elif active == "section" and hit.get("section_seq") is not None:
                    section_seq = int(hit["section_seq"])
                    rows = _load_section(hit, max_chunks)
                else:
                    rows = _load_neighbors(hit, radius)
            except Exception as exc:  # noqa: BLE001 - 扩展失败不影响检索
                logger.warning("知识库上下文扩展失败 %s:%s：%s", hit.get("doc_id"), seq, exc)
                rows = []

            remaining = max(0, total_chars - used)
            limit = min(block_chars, remaining)
            block = _expanded_block(hit, rows, limit=limit, section_seq=section_seq) if limit >= 200 else None
            if block is not None and len(rows) > 1:
                blocks.append(block)
                covered.append((int(block["seq_start"]), int(block["seq_end"])))
                used += len(block["text"])
                continue

        if _covered(seq, covered):
            continue
        blocks.append(_raw_block(hit))

    return blocks
