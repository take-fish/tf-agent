"""query_pipeline —— 查 GitHub Actions 流水线状态。"""

from __future__ import annotations

import logging

import httpx

from app.config import settings
from app.tools.base import ToolResult, ToolSpec, obj_schema

logger = logging.getLogger(__name__)

STATUS_ZH = {
    "success": "成功",
    "failure": "失败",
    "cancelled": "已取消",
    "skipped": "跳过",
    "in_progress": "进行中",
    "queued": "排队中",
    "waiting": "等待审批",
    "completed": "已完成",
}


def _headers() -> dict[str, str]:
    return {
        "Authorization": f"Bearer {settings.github_token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }


async def _runs_for(client: httpx.AsyncClient, repo: str, branch: str, per_page: int) -> list[dict]:
    params = {"per_page": per_page}
    if branch:
        params["branch"] = branch
    resp = await client.get(
        f"{settings.github_api.rstrip('/')}/repos/{settings.github_org}/{repo}/actions/runs",
        params=params,
        headers=_headers(),
    )
    resp.raise_for_status()
    return resp.json().get("workflow_runs") or []


async def query_pipeline(repo: str = "", branch: str = "", limit: int = 5) -> ToolResult:
    if not settings.github_token:
        return ToolResult.error("未配置 GITHUB_TOKEN（复用 tf-console 的 token）")
    limit = max(1, min(int(limit or 5), 20))
    repos = [repo.strip()] if (repo or "").strip() else list(settings.deploy_workflows.keys())
    lines: list[str] = []
    total = 0

    async with httpx.AsyncClient(timeout=15.0) as client:
        for name in repos[:6]:
            try:
                runs = await _runs_for(client, name, branch, limit)
            except httpx.HTTPStatusError as exc:
                lines.append(f"{name}: 查询失败 HTTP {exc.response.status_code}")
                continue
            except Exception as exc:  # noqa: BLE001
                lines.append(f"{name}: 查询失败 {exc}")
                continue
            if not runs:
                lines.append(f"{name}: 无流水线记录")
                continue
            for run in runs[:limit]:
                total += 1
                status = run.get("status")
                conclusion = run.get("conclusion")
                state = STATUS_ZH.get(conclusion or status or "", conclusion or status or "?")
                lines.append(
                    f"{name} | {run.get('name') or run.get('workflow_id')} | 分支 {run.get('head_branch')} "
                    f"| {state} | {str(run.get('created_at'))[:16]} | {run.get('html_url')}"
                )

    if not lines:
        return ToolResult(text="没有取到流水线状态", rows=0)
    return ToolResult(text="\n".join(lines), rows=total, data={"repos": repos})


spec = ToolSpec(
    name="query_pipeline",
    description="查询 GitHub Actions 流水线最近的运行状态（分支、结论、失败步骤链接）。不传 repo 则查全部常用仓库。",
    parameters=obj_schema(
        {
            "repo": {"type": "string", "description": "仓库名，例如 tf-game；留空查全部"},
            "branch": {"type": "string", "description": "只看某个分支，例如 master / test"},
            "limit": {"type": "integer", "description": "每个仓库返回条数，默认 5", "minimum": 1, "maximum": 20},
        }
    ),
    handler=query_pipeline,
    read_only=True,
    timeout=20.0,
)
