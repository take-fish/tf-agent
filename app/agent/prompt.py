"""系统提示词与场景定义。"""

from __future__ import annotations

from dataclasses import dataclass

SYSTEM_PROMPT = """你是 FishMatrix 内网智能运维助手，服务对象是研发和运维同学。

你可以做的事（通过工具）：
- 查日志：按 trace_id、服务名或关键词检索 Loki
- 查服务健康：Nacos 实例 + /healthz 探测
- 查数据库：只读白名单视图（不能写 SQL）
- 查流水线：GitHub Actions 运行状态
- 查知识库：团队文档（手册、规范、故障复盘）
- 触发发布 / 重启服务：写操作，必须人工确认

必须遵守：
1. 不确定的事就去查，不要编造 trace_id、服务名、日志内容、文档内容。
2. 回答里给出依据：日志时间、视图名、文档标题与段落、流水线链接。
3. 触发发布或重启前，先用一句话说明"将要做什么、影响哪个环境"，然后调用工具——
   系统会自动弹确认框，不要自己假装已经执行。
4. 你没有任何写代码、改文件、执行 shell 的能力，用户提出这类要求时直接说明。
5. 用中文回答，先给结论，再给依据；不要输出与问题无关的客套话。
6. 查不到就说查不到，并说明查了什么条件、建议下一步查什么。
7. 不要叙述"我将调用某个工具"或"调用 search_kb 函数"这类过程话术——
   需要工具时直接调用，拿到结果后直接给结论。
"""


@dataclass(frozen=True)
class Scene:
    name: str
    title: str
    tools: list[str]
    temperature: float = 0.2
    max_tokens: int = 800
    system_extra: str = ""


SCENES: dict[str, Scene] = {
    "ops": Scene(
        name="ops",
        title="运维排障",
        tools=["search_logs", "check_health", "query_view", "query_pipeline", "search_kb", "restart_service"],
        temperature=0.2,
    ),
    "release": Scene(
        name="release",
        title="发布操作",
        tools=["check_health", "query_pipeline", "query_view", "search_kb", "trigger_release", "restart_service"],
        temperature=0.1,
        max_tokens=600,
        system_extra="当前场景是发布：优先确认目标仓库、分支、环境三项信息齐全再动手。",
    ),
    "kb": Scene(
        name="kb",
        title="知识问答",
        tools=["search_kb"],
        temperature=0.3,
        system_extra="当前场景只查知识库：回答必须标注出自哪篇文档的第几块，不要调用其他工具。",
    ),
}

DEFAULT_SCENE = "ops"


def get_scene(name: str | None) -> Scene:
    return SCENES.get((name or DEFAULT_SCENE).strip(), SCENES[DEFAULT_SCENE])


def build_system_prompt(scene: Scene, *, env: str, user_name: str) -> str:
    parts = [SYSTEM_PROMPT]
    if scene.system_extra:
        parts.append(f"\n【场景】{scene.title}：{scene.system_extra}")
    parts.append(f"\n【当前环境】默认 env={env}（另一环境是 {'test' if env == 'prod' else 'prod'}）")
    parts.append(f"【当前用户】{user_name}")
    return "".join(parts)
