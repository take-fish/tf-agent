"""场景：默认 auto 不限定工具，由模型按用户描述自己选。"""

from __future__ import annotations

from app.agent import prompt


def test_auto_scene_is_default_and_has_no_tool_whitelist():
    assert prompt.get_scene("").name == "auto"
    assert prompt.get_scene(None).name == "auto"
    assert prompt.get_scene("auto").tools == []
    assert prompt.get_scene("AUTO").name == "auto"


def test_named_scenes_still_work_and_are_case_insensitive():
    assert prompt.get_scene("kb").name == "kb"
    assert prompt.get_scene("KB").name == "kb"
    assert prompt.get_scene("ops").name == "ops"


def test_auto_scene_is_always_listed():
    assert "auto" in prompt.scenes()


def test_unknown_scene_falls_back_to_auto():
    assert prompt.get_scene("not-exist").name == "auto"
