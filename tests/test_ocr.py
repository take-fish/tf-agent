"""图片 OCR：输入校验、结果解析与降级。"""

from __future__ import annotations

import base64
import io
import time

import pytest
from PIL import Image

from app import ocr


def _png_bytes(width: int = 2, height: int = 2) -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", (width, height), "white").save(buffer, format="PNG")
    return buffer.getvalue()


def _payload(raw: bytes | None = None, mime: str = "image/png") -> ocr.ImagePayload:
    data = base64.b64encode(raw if raw is not None else _png_bytes()).decode()
    return ocr.ImagePayload(mime=mime, data=data)


def test_prepare_image_accepts_png():
    prepared = ocr.prepare_image(_payload())
    assert prepared.mime == "image/png"
    assert prepared.bytes > 0
    assert len(prepared.sha256) == 64
    assert prepared.image.mode == "RGB"


def test_prepare_image_rejects_unsupported_mime():
    with pytest.raises(ocr.OcrError, match="格式不支持"):
        ocr.prepare_image(ocr.ImagePayload(mime="image/svg+xml", data="PHN2Zz48L3N2Zz4="))


def test_prepare_image_rejects_invalid_base64():
    with pytest.raises(ocr.OcrError, match="编码无效"):
        ocr.prepare_image(ocr.ImagePayload(mime="image/png", data="not base64 !!!"))


def test_prepare_image_rejects_mime_magic_mismatch():
    with pytest.raises(ocr.OcrError, match="声明不一致"):
        ocr.prepare_image(_payload(mime="image/jpeg"))


def test_prepare_image_rejects_oversized_bytes(monkeypatch):
    monkeypatch.setattr(ocr.settings, "ocr_max_bytes", 10)
    with pytest.raises(ocr.OcrError, match="图片过大"):
        ocr.prepare_image(_payload())


def test_prepare_image_rejects_too_many_pixels(monkeypatch):
    monkeypatch.setattr(ocr.settings, "ocr_max_pixels", 8)
    with pytest.raises(ocr.OcrError, match="像素过多"):
        ocr.prepare_image(_payload(_png_bytes(4, 4)))


def test_prepare_image_downscales_long_side(monkeypatch):
    monkeypatch.setattr(ocr.settings, "ocr_max_side", 3)
    prepared = ocr.prepare_image(_payload(_png_bytes(10, 4)))
    assert max(prepared.image.size) == 3


def test_extract_lines_filters_low_confidence_and_supports_tuple():
    class Output:
        txts = ("第一行", "", "低置信")
        scores = (0.9, 0.8, 0.2)

    assert ocr._extract_lines(Output(), 0.5) == ["第一行"]
    assert ocr._extract_lines(([], ["a", "b"], [0.9, 0.1]), 0.5) == ["a"]
    assert ocr._extract_lines(None, 0.5) == []


async def test_recognize_returns_text(monkeypatch):
    monkeypatch.setattr(ocr, "_run_engine", lambda prepared: ("识别到的文字", 1))
    result = await ocr.recognize(_payload())
    assert result.ok is True
    assert result.text == "识别到的文字"
    assert result.line_count == 1
    assert result.bytes > 0


async def test_recognize_degrades_on_engine_error(monkeypatch):
    def boom(prepared):
        raise RuntimeError("onnx boom")

    monkeypatch.setattr(ocr, "_run_engine", boom)
    result = await ocr.recognize(_payload())
    assert result.ok is False
    assert result.error == "图片识别失败"
    assert result.sha256


async def test_recognize_degrades_on_timeout(monkeypatch):
    def slow(prepared):
        time.sleep(0.2)
        return "迟到的文字", 1

    monkeypatch.setattr(ocr, "_run_engine", slow)
    monkeypatch.setattr(ocr.settings, "ocr_timeout_seconds", 0.01)
    result = await ocr.recognize(_payload())
    assert result.ok is False
    assert result.error == "图片识别超时"


async def test_recognize_respects_disabled(monkeypatch):
    monkeypatch.setattr(ocr.settings, "ocr_enabled", False)
    result = await ocr.recognize(_payload())
    assert result.ok is False
    assert result.error == "图片识别未启用"
