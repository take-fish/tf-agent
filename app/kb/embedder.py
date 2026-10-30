"""嵌入模型：bge-base-zh-v1.5，CPU 推理（8G 显存被 vLLM 占满，没有 GPU 余量）。"""

from __future__ import annotations

import logging
import threading

import anyio.to_thread

from app.config import settings

logger = logging.getLogger(__name__)

_model = None
_model_lock = threading.Lock()


def load_model():
    global _model
    with _model_lock:
        if _model is None:
            from sentence_transformers import SentenceTransformer

            source = settings.embed_model_path or settings.embed_model
            logger.info("加载嵌入模型：%s（CPU）", source)
            _model = SentenceTransformer(source, device="cpu")
        return _model


def embed_sync(texts: list[str]) -> list[list[float]]:
    if not texts:
        return []
    model = load_model()
    vectors = model.encode(
        texts,
        normalize_embeddings=True,
        batch_size=8,
        show_progress_bar=False,
        convert_to_numpy=True,
    )
    return [vector.tolist() for vector in vectors]


async def embed(texts: list[str]) -> list[list[float]]:
    return await anyio.to_thread.run_sync(embed_sync, texts)


def warmup() -> None:
    """启动时预热，避免首个查询等 10 秒。"""
    try:
        embed_sync(["预热"])
        logger.info("嵌入模型预热完成")
    except Exception as exc:  # noqa: BLE001 - 预热失败不阻塞启动
        logger.warning("嵌入模型预热失败：%s", exc)
