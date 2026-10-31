"""Agent 展示配置：默认值、校验与写库前的归一化。"""

from __future__ import annotations

import pytest

from app.agent import profile


def test_defaults_are_complete_and_independent():
    first = profile.defaults()
    second = profile.defaults()
    assert first["name"] and first["greeting"] and first["suggestions"]
    assert first["avatar"]["style"] in profile.AVATAR_STYLES
    # 返回的是深拷贝，外部改不会污染默认值
    first["avatar"]["size"] = 1
    first["suggestions"].append("x")
    assert second["avatar"]["size"] == profile.DEFAULT_PROFILE["avatar"]["size"]
    assert len(second["suggestions"]) == len(profile.DEFAULT_PROFILE["suggestions"])


def test_normalize_merges_defaults_and_lowercases_colors():
    config = profile.normalize({"name": "  小助手 ", "avatar": {"style": "cube", "primary": "#ABCDEF"}})
    assert config["name"] == "小助手"
    assert config["avatar"]["style"] == "cube"
    assert config["avatar"]["primary"] == "#abcdef"
    assert config["greeting"] == profile.DEFAULT_PROFILE["greeting"]


@pytest.mark.parametrize(
    ("payload", "message"),
    [
        ({"name": ""}, "名称不能为空"),
        ({"name": "x" * 41}, "名称最多 40"),
        ({"avatar": {"style": "sphere"}}, "形象样式"),
        ({"avatar": {"primary": "red"}}, "形象主色"),
        ({"avatar": {"size": 300}}, "形象尺寸"),
        ({"suggestions": ["a"] * 7}, "最多 6 条"),
        ({"suggestions": ["x" * 61]}, "最多 60"),
        ({"theme": "#fff"}, "主题色"),
        ({"greeting": ""}, "问候语不能为空"),
    ],
)
def test_normalize_rejects_bad_values(payload, message):
    with pytest.raises(profile.ProfileError) as exc:
        profile.normalize(payload)
    assert message in str(exc.value)


def test_normalize_drops_blank_suggestions():
    config = profile.normalize({"suggestions": ["  发布流程？ ", "", "  "]})
    assert config["suggestions"] == ["发布流程？"]
