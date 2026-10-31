"""trigger_release / restart_service —— 写操作，必须经过人工二次确认。

只调 GitHub Actions workflow_dispatch，不碰 SSH、不碰生产目录。
"""

from __future__ import annotations

import logging

import httpx

from app.config import settings
from app.tools.base import ToolResult, ToolSpec, obj_schema

logger = logging.getLogger(__name__)


def _headers() -> dict[str, str]:
    return {
        "Authorization": f"Bearer {settings.github_token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }


async def _dispatch(repo: str, workflow: str, ref: str, inputs: dict[str, str]) -> tuple[bool, str]:
    url = (
        f"{settings.github_api.rstrip('/')}/repos/{settings.github_org}/{repo}/actions/workflows/{workflow}/dispatches"
    )
    async with httpx.AsyncClient(timeout=20.0) as client:
        resp = await client.post(url, json={"ref": ref, "inputs": inputs}, headers=_headers())
        if resp.status_code == 422 and inputs:
            # workflow 没声明 inputs 时会 422，退化为只带 ref 再试一次
            resp = await client.post(url, json={"ref": ref}, headers=_headers())
        if resp.status_code in (204, 200, 201):
            return True, "已触发"
        return False, f"HTTP {resp.status_code}: {resp.text[:300]}"


async def trigger_release(repo: str, ref: str = "master", env: str = "prod") -> ToolResult:
    if not settings.github_token:
        return ToolResult.error("未配置 GITHUB_TOKEN，无法触发发布")
    repo = (repo or "").strip()
    workflow = settings.deploy_workflows.get(repo)
    if not workflow:
        return ToolResult.error(f"仓库 {repo} 未配置发布 workflow（FM_AGENT_DEPLOY_WORKFLOWS）")
    ref = (ref or "master").strip()
    env = (env or settings.default_env).strip()
    try:
        ok, message = await _dispatch(repo, workflow, ref, {"environment": env})
    except Exception as exc:  # noqa: BLE001
        return ToolResult.error(f"调用 GitHub 失败：{exc}")
    if not ok:
        return ToolResult.error(f"触发 {repo} 发布失败：{message}")
    return ToolResult(
        text=f"已触发 {repo} 的发布（workflow={workflow}, ref={ref}, env={env}）",
        rows=1,
        data={"repo": repo, "ref": ref, "env": env, "workflow": workflow},
    )


async def restart_service(service: str, env: str = "prod") -> ToolResult:
    if not settings.github_token:
        return ToolResult.error("未配置 GITHUB_TOKEN，无法重启服务")
    service = (service or "").strip()
    if not service:
        return ToolResult.error("必须指定 service")
    if service not in settings.deploy_workflows:
        return ToolResult.error(f"服务 {service} 不在已知仓库列表中（FM_AGENT_DEPLOY_WORKFLOWS）")
    workflow = settings.deploy_workflows.get(f"{service}:restart") or settings.restart_workflow
    env = (env or settings.default_env).strip()
    try:
        ok, message = await _dispatch(service, str(workflow), "master", {"service": service, "environment": env})
    except Exception as exc:  # noqa: BLE001
        return ToolResult.error(f"调用 GitHub 失败：{exc}")
    if not ok:
        return ToolResult.error(f"重启 {service} 失败：{message}")
    return ToolResult(
        text=f"已触发 {service} 重启（workflow={workflow}, env={env}）", rows=1, data={"service": service, "env": env}
    )


spec_trigger = ToolSpec(
    name="trigger_release",
    description="触发某个仓库的发布流水线（GitHub Actions workflow_dispatch）。"
    "这是写操作，调用前必须向用户说明将要发布什么，等待用户确认。",
    parameters=obj_schema(
        {
            "repo": {"type": "string", "description": "仓库名，例如 tf-game"},
            "ref": {"type": "string", "description": "分支或标签，默认 master"},
            "env": {"type": "string", "description": "环境：prod 或 test", "enum": ["prod", "test"]},
        },
        ["repo"],
    ),
    handler=trigger_release,
    read_only=False,
    timeout=30.0,
    requires_config=True,
)

spec_restart = ToolSpec(
    name="restart_service",
    description="重启某个微服务（GitHub Actions workflow_dispatch）。"
    "这是写操作，调用前必须向用户说明影响范围并等待确认。",
    parameters=obj_schema(
        {
            "service": {"type": "string", "description": "服务名，例如 tf-game"},
            "env": {"type": "string", "description": "环境：prod 或 test", "enum": ["prod", "test"]},
        },
        ["service"],
    ),
    handler=restart_service,
    read_only=False,
    timeout=60.0,
    requires_config=True,
)
