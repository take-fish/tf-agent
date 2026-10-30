"""Nacos 配置解析与降级的测试（不联网）。"""

from __future__ import annotations

import pytest

from app import nacos
from app.agent import prompt

CONFIG = """
version: 1
agent:
  scenes:
    ops:
      title: 运维排障（远程）
      temperature: 0.1
      max-tokens: 500
      tools: [search_logs, search_kb]
    kb:
      title: 知识问答
      tools: [search_kb]
  limits:
    max-tool-rounds: 2
    history-limit: 4
    max-input-chars: 2000
  concurrency:
    max-concurrency: 1
  knowledge-base:
    top-k: 5
    chunk-size: 400
"""


@pytest.fixture(autouse=True)
def _reset_runtime():
    nacos.set_runtime(None)
    yield
    nacos.set_runtime(None)


def test_parse_config():
    config = nacos.RuntimeConfig.from_yaml(CONFIG)
    assert set(config.scenes) == {"ops", "kb"}
    assert config.limits["max-tool-rounds"] == 2
    assert config.concurrency["max-concurrency"] == 1
    assert config.knowledge_base["top-k"] == 5


def test_parse_empty_config_is_safe():
    for text in ("", "version: 1", "agent: {}"):
        config = nacos.RuntimeConfig.from_yaml(text)
        assert config.scenes == {}


def test_limits_fall_back_to_default():
    assert nacos.limit("max-tool-rounds", 4) == 4
    nacos.set_runtime(nacos.RuntimeConfig.from_yaml(CONFIG))
    assert nacos.limit("max-tool-rounds", 4) == 2
    assert nacos.limit("not-configured", 7) == 7


def test_scenes_use_remote_overrides_and_keep_defaults():
    defaults = prompt.scenes()
    assert defaults["ops"].max_tokens == 800

    nacos.set_runtime(nacos.RuntimeConfig.from_yaml(CONFIG))
    remote = prompt.scenes()
    assert remote["ops"].title == "运维排障（远程）"
    assert remote["ops"].max_tokens == 500
    assert remote["ops"].tools == ["search_logs", "search_kb"]
    # release 没在远程配置里出现，就从表里消失（远程是权威）
    assert "release" not in remote


def test_get_scene_falls_back_when_unknown():
    nacos.set_runtime(nacos.RuntimeConfig.from_yaml(CONFIG))
    scene = prompt.get_scene("not-exist")
    assert scene.name in prompt.scenes()


def test_kb_param_prefers_remote():
    assert nacos.kb_param("chunk-size", 500) == 500
    nacos.set_runtime(nacos.RuntimeConfig.from_yaml(CONFIG))
    assert nacos.kb_param("chunk-size", 500) == 400
