"""search_kb —— 知识库语义检索（RAG 的取数入口）。"""

from __future__ import annotations

import logging

import anyio.to_thread

from app.config import settings
from app.kb import embedder, store
from app.tools.base import ToolResult, ToolSpec, obj_schema

logger = logging.getLogger(__name__)


async def search_kb(query: str, kb_ids: list[str] | None = None, top_k: int = 8) -> ToolResult:
    query = (query or "").strip()
    if not query:
        return ToolResult.error("检索词不能为空")
    top_k = max(1, min(int(top_k or settings.kb_top_k), 20))

    try:
        vector = (await embedder.embed([query]))[0]
    except Exception as exc:  # noqa: BLE001
        return ToolResult.error(f"向量化失败：{exc}")

    try:
        hits = await anyio.to_thread.run_sync(lambda: store.search(vector, kb_ids, top_k))
    except Exception as exc:  # noqa: BLE001
        return ToolResult.error(f"向量库不可用（{settings.qdrant_url}）：{exc}")

    if not hits:
        return ToolResult(text=f"知识库里没有找到与「{query}」相关的内容", rows=0)

    selected = hits[: settings.kb_max_chunks_in_context]
    lines = []
    for idx, hit in enumerate(selected, start=1):
        text = hit["text"].strip()[: settings.kb_chunk_chars]
        lines.append(f"[{idx}] 《{hit['title']}》第{int(hit['seq']) + 1}块（相似度 {hit['score']:.2f}）\n{text}")

    return ToolResult(
        text="知识库检索结果：\n" + "\n\n".join(lines),
        rows=len(selected),
        data={"chunks": selected, "query": query},
    )


spec = ToolSpec(
    name="search_kb",
    description="在团队知识库里做语义检索，返回最相关的文档片段（带出处）。"
    "涉及运维手册、流程规范、历史故障处理经验时先查这里。",
    parameters=obj_schema(
        {
            "query": {"type": "string", "description": "检索问题或关键词，用自然语言即可"},
            "kb_ids": {
                "type": "array",
                "description": "限定在指定知识库内检索（可选）",
                "items": {"type": "string"},
            },
            "top_k": {"type": "integer", "description": "召回条数，默认 8", "minimum": 1, "maximum": 20},
        },
        ["query"],
    ),
    handler=search_kb,
    read_only=True,
    timeout=30.0,
)
