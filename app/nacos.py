"""Nacos 接入：配置拉取 + 服务注册。

两件事，都**允许失败**——连不上 Nacos 时用代码里的默认值继续跑，
只是拿到不到集中下发的场景/限流参数，也不会出现在网关的服务发现里
（网关那时走 `TF_AGENT_URL` 静态兜底）。

1. 配置：拉 `tf-agent.yaml`（group=FM，namespace=fm-prod|fm-test），
   解析出场景工具白名单、上下文预算、并发与知识库参数，按 refresh_seconds 轮询热更新。
2. 注册：把本实例（ip:8201）注册成临时实例并持续心跳，
   让网关的 `service: tf-agent` 路由能通过服务发现命中；退出时注销。
"""

from __future__ import annotations

import asyncio
import logging
import socket
from dataclasses import dataclass, field
from typing import Any

import httpx
import yaml

from app.config import settings

logger = logging.getLogger(__name__)


@dataclass
class RuntimeConfig:
    """从 Nacos 拉到的运行参数；字段缺失时用空值，由调用方回落默认。"""

    scenes: dict[str, dict[str, Any]] = field(default_factory=dict)
    limits: dict[str, int] = field(default_factory=dict)
    concurrency: dict[str, int] = field(default_factory=dict)
    knowledge_base: dict[str, Any] = field(default_factory=dict)
    write_operations: dict[str, Any] = field(default_factory=dict)
    version: str = ""

    @classmethod
    def from_yaml(cls, text: str) -> RuntimeConfig:
        raw = yaml.safe_load(text) or {}
        section = raw.get("agent") if isinstance(raw, dict) else None
        section = section or {}
        return cls(
            scenes=dict(section.get("scenes") or {}),
            limits=dict(section.get("limits") or {}),
            concurrency=dict(section.get("concurrency") or {}),
            knowledge_base=dict(section.get("knowledge-base") or {}),
            write_operations=dict(section.get("write-operations") or {}),
            version=str(raw.get("version", "")) if isinstance(raw, dict) else "",
        )


_runtime: RuntimeConfig | None = None


def runtime() -> RuntimeConfig:
    """当前生效的远程配置；没拉到过就返回空配置。"""
    return _runtime or RuntimeConfig()


def set_runtime(config: RuntimeConfig | None) -> None:
    global _runtime
    _runtime = config


def limit(name: str, default: int) -> int:
    value = runtime().limits.get(name)
    try:
        return int(value) if value is not None else default
    except (TypeError, ValueError):
        return default


def kb_param(name: str, default: Any) -> Any:
    value = runtime().knowledge_base.get(name)
    return default if value is None else value


def concurrency(name: str, default: int) -> int:
    """并发 / 队列类参数（agent.concurrency.*）。"""
    value = runtime().concurrency.get(name)
    try:
        return int(value) if value is not None else default
    except (TypeError, ValueError):
        return default


def write_operation(name: str, default: Any) -> Any:
    """写操作开关（agent.write-operations.*），例如 require-confirm / confirm-ttl-seconds。"""
    value = runtime().write_operations.get(name)
    return default if value is None else value


class NacosClient:
    """极简客户端：只用 HTTP 开放接口，不引入 Nacos SDK。"""

    def __init__(
        self,
        base_url: str | None = None,
        namespace: str | None = None,
        username: str = "",
        password: str = "",
        timeout: float = 5.0,
    ) -> None:
        self.base_url = (base_url or settings.nacos_url).rstrip("/")
        self.namespace = namespace if namespace is not None else settings.nacos_namespace
        self.username = username
        self.password = password
        self._client = httpx.AsyncClient(timeout=timeout)
        self._token: str | None = None
        self._token_ok = False

    async def close(self) -> None:
        await self._client.aclose()

    async def _access_token(self) -> str | None:
        if self._token_ok:
            return self._token
        self._token_ok = True
        if not self.username:
            return None
        try:
            response = await self._client.post(
                f"{self.base_url}/nacos/v1/auth/login",
                data={"username": self.username, "password": self.password},
            )
            if response.status_code == 200:
                self._token = response.json().get("accessToken")
                return self._token
            logger.warning("Nacos 登录失败：HTTP %s", response.status_code)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Nacos 登录异常：%s", exc)
        return None

    async def _params(self) -> dict[str, str]:
        params: dict[str, str] = {"tenant": self.namespace} if self.namespace else {}
        token = await self._access_token()
        if token:
            params["accessToken"] = token
        return params

    async def get_config(self, data_id: str, group: str) -> str | None:
        """拉配置；不存在返回 None。

        Nacos 对"没有这条配置"的表示不统一：有的版本返回 404，
        有的返回 200 + 正文 `config data not exist`，两种都要当成 None，
        否则会把这句话当成 YAML 解析失败。
        """
        params = await self._params()
        params.update({"dataId": data_id, "group": group})
        response = await self._client.get(f"{self.base_url}/nacos/v1/cs/configs", params=params)
        if response.status_code == 404:
            return None
        response.raise_for_status()
        text = response.text
        if text.strip() == "config data not exist":
            return None
        return text

    async def register_instance(self, service: str, ip: str, port: int) -> bool:
        params = await self._params()
        if self.namespace:
            params["namespaceId"] = self.namespace
        params.update(
            {
                "serviceName": service,
                "ip": ip,
                "port": str(port),
                "ephemeral": "true",
                "weight": "1",
                "enabled": "true",
            }
        )
        response = await self._client.post(f"{self.base_url}/nacos/v1/ns/instance", params=params)
        return response.status_code == 200 and response.text.strip() == "ok"

    async def deregister_instance(self, service: str, ip: str, port: int) -> None:
        params = await self._params()
        if self.namespace:
            params["namespaceId"] = self.namespace
        params.update({"serviceName": service, "ip": ip, "port": str(port), "ephemeral": "true"})
        await self._client.delete(f"{self.base_url}/nacos/v1/ns/instance", params=params)

    async def beat(self, service: str, ip: str, port: int) -> None:
        """临时实例必须持续心跳，否则 Nacos 15 秒后标记不健康、30 秒后摘除。"""
        import json

        beat = {"serviceName": service, "ip": ip, "port": port, "weight": 1, "cluster": "DEFAULT"}
        params = await self._params()
        if self.namespace:
            params["namespaceId"] = self.namespace
        params.update({"serviceName": service, "beat": json.dumps(beat)})
        await self._client.put(f"{self.base_url}/nacos/v1/ns/instance/beat", params=params)


@dataclass
class NacosStatus:
    config_ok: bool = False
    config_source: str = "defaults"
    registered: bool = False
    last_error: str = ""


_status = NacosStatus()


def status() -> NacosStatus:
    return _status


def instance_ip() -> str:
    """本机上报给 Nacos 的 IP；容器里从环境变量给，宿主机用主机名解析兜底。"""
    configured = settings.nacos_instance_ip
    if configured:
        return configured
    try:
        return socket.gethostbyname(socket.gethostname())
    except OSError:
        return "127.0.0.1"


class NacosRuntime:
    """把配置轮询、注册与心跳串起来；主应用只调 start/stop。"""

    def __init__(self) -> None:
        self.client: NacosClient | None = None
        self._tasks: list[asyncio.Task] = []
        self.ip = instance_ip()

    async def _refresh_config(self) -> None:
        assert self.client is not None
        try:
            text = await self.client.get_config(settings.nacos_config_data_id, settings.nacos_config_group)
        except Exception as exc:  # noqa: BLE001
            _status.config_ok = False
            _status.last_error = f"配置拉取失败：{exc}"
            logger.warning("Nacos 配置拉取失败，继续使用%s：%s", _status.config_source, exc)
            return
        if text is None:
            _status.config_ok = False
            _status.last_error = f"未找到 {settings.nacos_config_data_id}"
            logger.warning("Nacos 上没有 %s，继续使用默认参数", settings.nacos_config_data_id)
            return
        try:
            set_runtime(RuntimeConfig.from_yaml(text))
        except Exception as exc:  # noqa: BLE001
            _status.config_ok = False
            _status.last_error = f"配置解析失败：{exc}"
            logger.warning("Nacos 配置解析失败：%s", exc)
            return
        _status.config_ok = True
        _status.config_source = f"nacos:{settings.nacos_config_data_id}"
        _status.last_error = ""
        logger.info(
            "Nacos 配置已加载：场景 %s，限流 %s",
            ",".join(runtime().scenes) or "-",
            runtime().concurrency or "-",
        )

    async def _config_loop(self) -> None:
        while True:
            await self._refresh_config()
            await asyncio.sleep(max(10, settings.nacos_refresh_seconds))

    async def _register(self) -> None:
        assert self.client is not None
        try:
            ok = await self.client.register_instance(settings.nacos_service_name, self.ip, settings.nacos_port)
            _status.registered = ok
            if ok:
                logger.info("已注册到 Nacos：%s %s:%s", settings.nacos_service_name, self.ip, settings.nacos_port)
            else:
                logger.warning("Nacos 注册未成功，网关将走 TF_AGENT_URL 静态兜底")
        except Exception as exc:  # noqa: BLE001
            _status.registered = False
            logger.warning("Nacos 注册失败（走静态兜底）：%s", exc)

    async def _beat_loop(self) -> None:
        while True:
            await asyncio.sleep(5)
            if not _status.registered or self.client is None:
                continue
            try:
                await self.client.beat(settings.nacos_service_name, self.ip, settings.nacos_port)
            except Exception as exc:  # noqa: BLE001
                _status.registered = False
                logger.warning("Nacos 心跳失败：%s", exc)

    async def start(self) -> None:
        if not settings.nacos_enabled:
            logger.info("NACOS_ENABLED=0，跳过 Nacos 配置与注册")
            return
        self.client = NacosClient(
            username=settings.nacos_username,
            password=settings.nacos_password,
        )
        await self._refresh_config()
        if settings.nacos_register:
            await self._register()
        self._tasks.append(asyncio.create_task(self._config_loop()))
        if settings.nacos_register:
            self._tasks.append(asyncio.create_task(self._beat_loop()))

    async def stop(self) -> None:
        for task in self._tasks:
            task.cancel()
        self._tasks.clear()
        if self.client is None:
            return
        if _status.registered:
            try:
                await self.client.deregister_instance(settings.nacos_service_name, self.ip, settings.nacos_port)
            except Exception as exc:  # noqa: BLE001
                logger.warning("Nacos 注销失败：%s", exc)
            _status.registered = False
        await self.client.close()
        self.client = None


_runtime_manager: NacosRuntime | None = None


def manager() -> NacosRuntime:
    global _runtime_manager
    if _runtime_manager is None:
        _runtime_manager = NacosRuntime()
    return _runtime_manager
