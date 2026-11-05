"""集中配置：全部来自环境变量。

设计原则：
1. 所有外部依赖都可缺省——本机试点时 Loki/Nacos/GitHub 连不上，agent 仍要能对话，
   由工具自己返回结构化错误，而不是启动就崩。
2. 缺省值面向"笔记本 + 集中机"拓扑（见部署方案 §4）。
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from functools import lru_cache


def _env(name: str, default: str = "") -> str:
    return os.getenv(name, default).strip()


def _load_dotenv() -> None:
    """极简 .env 加载（不引入额外依赖）：只认 KEY=VALUE，不覆盖已有环境变量。"""
    from pathlib import Path

    for filename in (".env", ".env.local"):
        path = Path(__file__).resolve().parent.parent / filename
        if not path.exists():
            continue
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except OSError:
            continue
        for line in lines:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            key = key.strip()
            value = value.strip().strip('"').strip("'")
            if key and key not in os.environ:
                os.environ[key] = value


_load_dotenv()


def _int(name: str, default: int) -> int:
    raw = _env(name)
    try:
        return int(raw) if raw else default
    except ValueError:
        return default


def _float(name: str, default: float) -> float:
    raw = _env(name)
    try:
        return float(raw) if raw else default
    except ValueError:
        return default


def _bool(name: str, default: bool = False) -> bool:
    raw = _env(name).lower()
    if not raw:
        return default
    return raw in {"1", "true", "yes", "on"}


def _json_dict(name: str) -> dict:
    raw = _env(name)
    if not raw:
        return {}
    try:
        value = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    return value if isinstance(value, dict) else {}


@dataclass
class Settings:
    # 服务
    app_version: str = field(default_factory=lambda: _env("APP_VERSION", "dev"))
    # 开发模式：跳过网关签名校验（仅本机调试，生产必须关闭）
    dev_mode: bool = field(default_factory=lambda: _bool("FM_AGENT_DEV_MODE"))

    # 模型
    llm_base_url: str = field(default_factory=lambda: _env("FM_LLM_BASE_URL", "http://127.0.0.1:8000/v1"))
    llm_api_key: str = field(default_factory=lambda: _env("FM_LLM_API_KEY"))
    llm_model: str = field(default_factory=lambda: _env("FM_LLM_MODEL", "qwen2.5-7b-instruct"))
    llm_connect_timeout: float = field(default_factory=lambda: _float("FM_LLM_CONNECT_TIMEOUT", 5.0))
    llm_read_timeout: float = field(default_factory=lambda: _float("FM_LLM_READ_TIMEOUT", 180.0))
    # vLLM --max-model-len，8192 是"输入 + 输出"的总预算，不是只限输入
    llm_context_tokens: int = field(default_factory=lambda: _int("FM_LLM_CONTEXT_TOKENS", 8192))
    # 单轮最多重试次数（只在还没吐出任何 delta 时重试，避免前端收到重复内容）
    llm_retries: int = field(default_factory=lambda: _int("FM_LLM_RETRIES", 1))

    # 编排
    max_concurrency: int = field(default_factory=lambda: _int("FM_AGENT_MAX_CONCURRENCY", 2))
    queue_limit: int = field(default_factory=lambda: _int("FM_AGENT_QUEUE_LIMIT", 4))
    request_timeout: float = field(default_factory=lambda: _float("FM_AGENT_TIMEOUT_SECONDS", 120.0))
    tool_timeout: float = field(default_factory=lambda: _float("FM_AGENT_TOOL_TIMEOUT_SECONDS", 20.0))
    max_tool_rounds: int = field(default_factory=lambda: _int("FM_AGENT_MAX_TOOL_ROUNDS", 4))
    max_tokens: int = field(default_factory=lambda: _int("FM_AGENT_MAX_TOKENS", 800))
    max_input_chars: int = field(default_factory=lambda: _int("FM_AGENT_MAX_INPUT_CHARS", 6000))
    history_limit: int = field(default_factory=lambda: _int("FM_AGENT_HISTORY_LIMIT", 10))
    rate_limit_per_minute: int = field(default_factory=lambda: _int("FM_AGENT_RPM", 10))
    # 同一轮的多个只读工具并发执行上限（Loki / MySQL / ES 各一个连接，别打爆）
    tool_parallelism: int = field(default_factory=lambda: _int("FM_AGENT_TOOL_PARALLELISM", 3))
    # SSE 心跳间隔（秒）：长工具调用期间防止网关/浏览器空闲断连
    sse_heartbeat_seconds: float = field(default_factory=lambda: _float("FM_AGENT_SSE_HEARTBEAT", 10.0))

    # 审计库
    mysql_dsn: str = field(
        default_factory=lambda: _env(
            "FM_AGENT_MYSQL_DSN",
            "mysql+pymysql://root:fm123456@127.0.0.1:3307/fm_agent?charset=utf8mb4",
        )
    )

    # 数据源（集中机）
    loki_url: str = field(default_factory=lambda: _env("LOKI_URL", "http://10.28.13.112:3100"))
    loki_timeout: float = field(default_factory=lambda: _float("LOKI_TIMEOUT_SECONDS", 10.0))
    # 留空表示 service 是 JSON 字段（Java/Go 日志均带 service），用 `| json | service="..."` 过滤；
    # 只有显式配置成真实流标签（如 compose_service / container）时才拼进 `{}` 选择器。
    loki_service_label: str = field(default_factory=lambda: _env("LOKI_SERVICE_LABEL"))
    nacos_url: str = field(default_factory=lambda: _env("NACOS_URL", "http://10.28.13.112:8848"))
    nacos_namespace: str = field(default_factory=lambda: _env("NACOS_NAMESPACE", "fm-prod"))
    # 配置中心：拉 tf-agent.yaml 覆盖场景/限流/知识库参数
    nacos_enabled: bool = field(default_factory=lambda: _bool("NACOS_ENABLED", True))
    nacos_config_data_id: str = field(default_factory=lambda: _env("NACOS_CONFIG_DATA_ID", "tf-agent.yaml"))
    nacos_config_group: str = field(default_factory=lambda: _env("NACOS_CONFIG_GROUP", "FM"))
    nacos_refresh_seconds: int = field(default_factory=lambda: _int("NACOS_REFRESH_SECONDS", 60))
    # 服务注册：让网关 service=tf-agent 的发现能命中本实例
    nacos_register: bool = field(default_factory=lambda: _bool("NACOS_REGISTER", True))
    nacos_service_name: str = field(default_factory=lambda: _env("NACOS_SERVICE_NAME", "tf-agent"))
    # 上报给 Nacos 的 IP/端口；容器里必须显式给宿主机可达 IP
    nacos_instance_ip: str = field(default_factory=lambda: _env("NACOS_INSTANCE_IP"))
    nacos_port: int = field(default_factory=lambda: _int("FM_AGENT_PORT", 8201))
    # Nacos 2.x 开启鉴权时需要；未开启留空
    nacos_username: str = field(default_factory=lambda: _env("NACOS_USERNAME"))
    nacos_password: str = field(default_factory=lambda: _env("NACOS_PASSWORD"))
    # 静态服务表：Nacos 不可用时的兜底，{"tf-game": "http://10.28.13.112:8082"}
    services_map: dict = field(default_factory=lambda: _json_dict("FM_AGENT_SERVICES"))

    # GitHub（复用 tf-console 的 token）
    github_token: str = field(default_factory=lambda: _env("GITHUB_TOKEN"))
    github_api: str = field(default_factory=lambda: _env("GITHUB_API", "https://api.github.com"))
    github_org: str = field(default_factory=lambda: _env("GITHUB_ORG", "take-fish"))
    # repo -> 发布 workflow 文件名；各业务仓统一为 .github/workflows/release.yml
    deploy_workflows: dict = field(
        default_factory=lambda: (
            _json_dict("FM_AGENT_DEPLOY_WORKFLOWS")
            or {
                "tf-web": "release.yml",
                "tf-console": "release.yml",
                "tf-gateway": "release.yml",
                "tf-user": "release.yml",
                "tf-chat": "release.yml",
                "tf-reader": "release.yml",
                "tf-game": "release.yml",
                "tf-music": "release.yml",
            }
        )
    )

    # 只读数据库视图白名单：view -> 说明
    db_views: dict = field(default_factory=lambda: _json_dict("FM_AGENT_DB_VIEWS"))
    db_dsn: str = field(default_factory=lambda: _env("FM_AGENT_DB_DSN"))
    # 重启服务用的 workflow。业务仓目前没有 restart.yml，留空表示未配置；
    # 需要重启能力时在目标仓新增工作流，再设 FM_AGENT_RESTART_WORKFLOW
    # 或用 FM_AGENT_DEPLOY_WORKFLOWS 配 "<repo>:restart" 覆盖。
    restart_workflow: str = field(default_factory=lambda: _env("FM_AGENT_RESTART_WORKFLOW"))

    # 向量库 / 嵌入
    # 向量库：Elasticsearch（BM25 + kNN 混合检索；Qdrant 已下线）
    kb_backend: str = field(default_factory=lambda: _env("FM_KB_BACKEND", "es"))
    kb_hybrid: bool = field(default_factory=lambda: _bool("FM_KB_HYBRID", True))
    es_url: str = field(default_factory=lambda: _env("FM_KB_ES_URL", "http://127.0.0.1:9200"))
    es_index: str = field(default_factory=lambda: _env("FM_KB_ES_INDEX", "fm_kb"))
    es_username: str = field(default_factory=lambda: _env("FM_KB_ES_USERNAME"))
    es_password: str = field(default_factory=lambda: _env("FM_KB_ES_PASSWORD"))
    es_timeout: float = field(default_factory=lambda: _float("FM_KB_ES_TIMEOUT", 10.0))
    embed_model: str = field(default_factory=lambda: _env("FM_EMBED_MODEL", "BAAI/bge-base-zh-v1.5"))
    embed_model_path: str = field(default_factory=lambda: _env("FM_EMBED_MODEL_PATH"))
    embed_dim: int = field(default_factory=lambda: _int("FM_EMBED_DIM", 768))
    chunk_size: int = field(default_factory=lambda: _int("FM_KB_CHUNK_SIZE", 800))
    chunk_overlap: int = field(default_factory=lambda: _int("FM_KB_CHUNK_OVERLAP", 80))
    kb_top_k: int = field(default_factory=lambda: _int("FM_KB_TOP_K", 8))
    kb_max_chunks_in_context: int = field(default_factory=lambda: _int("FM_KB_MAX_CHUNKS", 5))
    kb_chunk_chars: int = field(default_factory=lambda: _int("FM_KB_CHUNK_CHARS", 300))
    upload_dir: str = field(default_factory=lambda: _env("FM_AGENT_UPLOAD_DIR", "data/uploads"))

    # 网关身份
    identity_secret: str = field(default_factory=lambda: _env("FM_IDENTITY_SECRET"))
    identity_ttl: int = field(default_factory=lambda: _int("FM_IDENTITY_TTL_SECONDS", 120))
    # 确认令牌签名密钥：不配就复用网关密钥，再没有就用开发默认值（仅本地）
    confirm_secret: str = field(default_factory=lambda: _env("FM_CONFIRM_SECRET"))

    # 默认环境（prod/test 共用一个实例，工具参数带 env）
    default_env: str = field(default_factory=lambda: _env("FM_AGENT_DEFAULT_ENV", "prod"))


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
