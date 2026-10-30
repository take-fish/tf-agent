"""check_health —— 查服务健康：Nacos 实例列表 + /healthz 探测。"""

from __future__ import annotations

import asyncio
import json
import logging
import time

import httpx

from app.config import settings
from app.tools.base import ToolResult, ToolSpec, obj_schema

logger = logging.getLogger(__name__)

PROBE_PATHS = ("/healthz", "/actuator/health")
PROBE_TIMEOUT = 3.0


def _static_services() -> dict[str, str]:
    raw = getattr(settings, "services_map", None) or {}
    return raw if isinstance(raw, dict) else {}


async def _nacos_services(client: httpx.AsyncClient) -> dict[str, list[dict]]:
    base = settings.nacos_url.rstrip("/")
    ns = settings.nacos_namespace
    resp = await client.get(
        f"{base}/nacos/v1/ns/service/list",
        params={"pageNo": 1, "pageSize": 200, "namespaceId": ns},
    )
    resp.raise_for_status()
    names = resp.json().get("doms") or []
    result: dict[str, list[dict]] = {}
    for name in names:
        try:
            inst = await client.get(
                f"{base}/nacos/v1/ns/instance/list",
                params={"serviceName": name, "namespaceId": ns},
            )
            inst.raise_for_status()
            result[name] = inst.json().get("hosts") or []
        except Exception:  # noqa: BLE001 - 单个服务查不到不影响其他
            result[name] = []
    return result


async def _probe(client: httpx.AsyncClient, url: str) -> tuple[bool, int, str]:
    start = time.perf_counter()
    for path in PROBE_PATHS:
        try:
            resp = await client.get(f"{url.rstrip('/')}{path}", timeout=PROBE_TIMEOUT)
            ms = int((time.perf_counter() - start) * 1000)
            if resp.status_code < 400:
                return True, ms, resp.text[:120]
            return False, ms, f"HTTP {resp.status_code}"
        except Exception:  # noqa: BLE001
            continue
    return False, int((time.perf_counter() - start) * 1000), "不可达"


async def check_health(service: str = "") -> ToolResult:
    service = (service or "").strip()
    targets: list[tuple[str, str]] = []  # (name, base_url)

    async with httpx.AsyncClient(timeout=5.0) as client:
        try:
            services = await _nacos_services(client)
        except Exception as exc:  # noqa: BLE001 - Nacos 不可达时退回静态表
            services = {}
            logger.warning("Nacos 查询失败：%s", exc)

    if services:
        for name, hosts in services.items():
            if service and service not in name:
                continue
            for host in hosts:
                if not host.get("enabled", True):
                    continue
                targets.append((name, f"http://{host.get('ip')}:{host.get('port')}"))
    if not targets:
        for name, url in _static_services().items():
            if service and service not in name:
                continue
            targets.append((name, url))

    if not targets:
        hint = "（未配置静态服务表，且 Nacos 未返回实例）"
        return ToolResult(text=f"没有找到服务 {service or '（任意）'} 的实例 {hint}", rows=0)

    targets = targets[:12]
    sem = asyncio.Semaphore(8)

    async def one(name: str, url: str) -> tuple[str, str, bool, int]:
        async with sem:
            async with httpx.AsyncClient(timeout=PROBE_TIMEOUT + 2) as client:
                ok, ms, detail = await _probe(client, url)
        return name, url, ok, ms

    results = await asyncio.gather(*(one(n, u) for n, u in targets))
    lines = []
    down = 0
    for name, url, ok, ms in results:
        if ok:
            lines.append(f"{name}: OK ({url}, {ms}ms)")
        else:
            down += 1
            lines.append(f"{name}: DOWN ({url})")
    summary = f"共 {len(results)} 个实例，异常 {down} 个"
    return ToolResult(
        text=summary + "\n" + "\n".join(lines),
        rows=len(results),
        data={"down": down, "instances": [{"service": n, "url": u, "ok": o, "ms": m} for n, u, o, m in results]},
    )


spec = ToolSpec(
    name="check_health",
    description="查询微服务健康状态：从 Nacos 取实例列表并探测 /healthz。不传服务名则查全部。",
    parameters=obj_schema(
        {"service": {"type": "string", "description": "服务名（支持部分匹配），例如 tf-game；留空查全部"}}
    ),
    handler=check_health,
    read_only=True,
    timeout=20.0,
)


__all__ = ["check_health", "spec", "_static_services"]
_ = json  # 保留导入占位，静态表由配置注入
