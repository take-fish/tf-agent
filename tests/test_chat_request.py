"""聊天请求体：单图与多图字段合并、数量与总量上限。"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.api.chat import ChatImage, ChatRequest


def _image(mime: str = "image/png", size: int = 16) -> ChatImage:
    return ChatImage(mime=mime, data="a" * size)


def test_single_image_is_merged_into_images():
    req = ChatRequest(
        message="这个报错是什么原因",
        image=_image("image/png"),
        images=[_image("image/jpeg")],
    )

    assert [item.mime for item in req.images] == ["image/png", "image/jpeg"]


def test_images_only_request_is_accepted():
    req = ChatRequest(message="看图", images=[_image(), _image("image/webp")])
    assert len(req.images) == 2
    assert req.image is None


def test_rejects_more_than_six_images():
    with pytest.raises(ValidationError):
        ChatRequest(message="看图", images=[_image() for _ in range(7)])

    with pytest.raises(ValidationError):
        ChatRequest(message="看图", image=_image(), images=[_image() for _ in range(6)])


def test_rejects_oversized_total_payload():
    with pytest.raises(ValidationError):
        ChatRequest(
            message="看图",
            images=[_image(size=4_100_000), _image(size=4_100_000)],
        )
