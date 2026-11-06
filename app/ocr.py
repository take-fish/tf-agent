"""图片文字识别（OCR）：把聊天里上传的截图转成文本证据，交给纯文本模型。

设计要点：

1. 不换视觉模型、不占 GPU。RapidOCR + onnxruntime 全在 CPU 上跑，单进程串行，
   避免和 bge 嵌入抢核心。
2. 图片是**不可信输入**：严格校验 MIME / magic bytes / Base64 / 大小 / 像素，
   只取 GIF 第一帧，不落盘，不把原始字节或 OCR 正文写进日志/审计/会话历史。
3. OCR 是回答前的确定性预处理，不注册成模型工具；失败或超时只降级为“读不到文字”，
   绝不阻断对话。
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import hashlib
import io
import logging
import re
import threading
import time
from dataclasses import dataclass
from typing import Any

from app.config import settings

logger = logging.getLogger(__name__)

# 只接受聊天端实际会产生的格式；SVG 等矢量/可执行内容一律拒绝。
ALLOWED_MIME = {
    "image/png": "PNG",
    "image/jpeg": "JPEG",
    "image/webp": "WEBP",
    "image/gif": "GIF",
}

# 浏览器粘贴的 data URL 可能带空白/换行，解码前先剔除 Base64 之外的字符？
# 不接受：严格模式拒绝任何非 Base64 字符，避免把别的协议夹带进来。
_BASE64_RE = re.compile(r"^[A-Za-z0-9+/]*={0,2}$")

_engine: Any = None
_engine_lock = threading.Lock()
_thread_sem = threading.Semaphore(max(1, settings.ocr_max_concurrency))


class OcrError(Exception):
    """可安全展示给用户的 OCR 失败（不含图片内容）。"""


@dataclass(frozen=True)
class ImagePayload:
    mime: str
    data: str


@dataclass(frozen=True)
class PreparedImage:
    image: Any  # PIL.Image.Image；只在工作线程内使用
    mime: str
    bytes: int
    sha256: str


@dataclass(frozen=True)
class OcrResult:
    ok: bool
    mime: str = ""
    bytes: int = 0
    sha256: str = ""
    text: str = ""
    line_count: int = 0
    ms: int = 0
    error: str = ""


def _check_magic(raw: bytes, mime: str) -> None:
    if mime == "image/png":
        ok = raw.startswith(b"\x89PNG\r\n\x1a\n")
    elif mime == "image/jpeg":
        ok = raw.startswith(b"\xff\xd8\xff")
    elif mime == "image/webp":
        ok = len(raw) >= 12 and raw[:4] == b"RIFF" and raw[8:12] == b"WEBP"
    elif mime == "image/gif":
        ok = raw.startswith((b"GIF87a", b"GIF89a"))
    else:
        ok = False
    if not ok:
        raise OcrError("图片格式与声明不一致")


def prepare_image(payload: ImagePayload) -> PreparedImage:
    """校验并解码图片，返回可直接喂给 OCR 的 RGB 图像。"""
    mime = (payload.mime or "").strip().lower()
    expected_format = ALLOWED_MIME.get(mime)
    if expected_format is None:
        raise OcrError("图片格式不支持")

    data = (payload.data or "").strip()
    if not data:
        raise OcrError("图片为空")
    if not _BASE64_RE.match(data):
        raise OcrError("图片编码无效")
    try:
        raw = base64.b64decode(data, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise OcrError("图片编码无效") from exc
    if not raw:
        raise OcrError("图片为空")
    if len(raw) > settings.ocr_max_bytes:
        raise OcrError("图片过大")

    _check_magic(raw, mime)

    # Pillow 延迟导入：OCR 关闭或未收到图片时不必加载图像库。
    from PIL import Image, ImageOps, UnidentifiedImageError

    Image.MAX_IMAGE_PIXELS = max(1, settings.ocr_max_pixels)
    try:
        with Image.open(io.BytesIO(raw)) as probe:
            probe.verify()
        with Image.open(io.BytesIO(raw)) as source:
            if source.format != expected_format:
                raise OcrError("图片格式与声明不一致")
            width, height = source.size
            if width <= 0 or height <= 0:
                raise OcrError("图片内容无法解析")
            if width * height > settings.ocr_max_pixels:
                raise OcrError("图片像素过多")
            # GIF 只看第一帧；动图后续帧不参与识别。
            if getattr(source, "n_frames", 1) > 1:
                source.seek(0)
            image = ImageOps.exif_transpose(source)
            # 长边过大时等比缩小，既控制 OCR 耗时，又避免直接拒绝 4K 截图。
            if max(image.size) > settings.ocr_max_side:
                image.thumbnail(
                    (settings.ocr_max_side, settings.ocr_max_side),
                    Image.Resampling.LANCZOS,
                )
            image = image.convert("RGB").copy()
    except OcrError:
        raise
    except (UnidentifiedImageError, OSError, ValueError, Image.DecompressionBombError) as exc:
        raise OcrError("图片内容无法解析") from exc

    return PreparedImage(
        image=image,
        mime=mime,
        bytes=len(raw),
        sha256=hashlib.sha256(raw).hexdigest(),
    )


def _get_engine() -> Any:
    global _engine
    if _engine is None:
        with _engine_lock:
            if _engine is None:
                from rapidocr import RapidOCR

                _engine = RapidOCR()
    return _engine


def _extract_lines(result: Any, min_confidence: float) -> list[str]:
    """兼容 RapidOCR 3.x 的 RapidOCROutput 与旧版 tuple 返回值。"""
    txts: Any = getattr(result, "txts", None)
    scores: Any = getattr(result, "scores", None)
    if txts is None and isinstance(result, (tuple, list)):
        # 旧版：([boxes], [txts], [scores])
        if len(result) >= 2:
            txts = result[1]
            scores = result[2] if len(result) >= 3 else None
    if not txts:
        return []

    if scores is None:
        scores = [1.0] * len(txts)
    lines: list[str] = []
    for idx, value in enumerate(txts):
        text = str(value or "").strip()
        if not text:
            continue
        try:
            score = float(scores[idx])
        except (IndexError, TypeError, ValueError):
            score = 1.0
        if score < min_confidence:
            continue
        lines.append(text)
    return lines


def _run_engine(prepared: PreparedImage) -> tuple[str, int]:
    with _thread_sem:
        engine = _get_engine()
        result = engine(prepared.image)
        lines = _extract_lines(result, settings.ocr_min_confidence)
    return "\n".join(lines), len(lines)


def _failure(mime: str, size: int, sha: str, started: float, error: str) -> OcrResult:
    return OcrResult(
        ok=False,
        mime=mime,
        bytes=size,
        sha256=sha,
        ms=int((time.perf_counter() - started) * 1000),
        error=error,
    )


async def recognize(payload: ImagePayload) -> OcrResult:
    """异步识别一张图片；异常收敛成 OcrResult，不向请求层抛。"""
    started = time.perf_counter()
    mime = (payload.mime or "").strip().lower()
    if not settings.ocr_enabled:
        return _failure(mime, 0, "", started, "图片识别未启用")

    try:
        prepared = await asyncio.to_thread(prepare_image, payload)
    except OcrError as exc:
        return _failure(mime, 0, "", started, str(exc))
    except Exception as exc:  # noqa: BLE001 - 图片解析异常不能中断对话
        logger.warning("图片预处理失败：%s", type(exc).__name__)
        return _failure(mime, 0, "", started, "图片内容无法解析")

    try:
        text, line_count = await asyncio.wait_for(
            asyncio.to_thread(_run_engine, prepared),
            timeout=max(0.1, settings.ocr_timeout_seconds),
        )
    except TimeoutError:
        logger.warning("图片识别超时 sha=%s", prepared.sha256[:12])
        return _failure(prepared.mime, prepared.bytes, prepared.sha256, started, "图片识别超时")
    except Exception as exc:  # noqa: BLE001
        logger.warning("图片识别失败 sha=%s error=%s", prepared.sha256[:12], type(exc).__name__)
        return _failure(prepared.mime, prepared.bytes, prepared.sha256, started, "图片识别失败")

    return OcrResult(
        ok=True,
        mime=prepared.mime,
        bytes=prepared.bytes,
        sha256=prepared.sha256,
        text=text,
        line_count=line_count,
        ms=int((time.perf_counter() - started) * 1000),
    )
