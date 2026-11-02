# tf-agent

FishMatrix（摸鱼矩阵）Agent 智能助手服务 —— Python 3.13 + FastAPI。

在控制台里用一句话排查问题：查日志、查服务健康、查数据库白名单视图、查流水线、查团队知识库，
以及**经人工确认后**触发发布 / 重启服务。它没有任何写代码、改文件、执行 shell 的能力。

- 端口：`8201`（仅 GPU 笔记本 `10.28.13.214` 对外，防火墙限源 IP `10.28.13.112`）
- 健康检查：`GET /healthz`
- 依赖：vLLM（`qwen2.5-7b-instruct`，OpenAI 兼容）+ MySQL `fm_agent`（审计）+ Qdrant（向量）

## 接口一览

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/api/v1/agent/chat` | SSE 流式对话（`start` / `delta` / `tool` / `confirm_required` / `done` / `error`） |
| POST | `/api/v1/agent/confirm` | 确认写操作（发布 / 重启），60 秒内有效，仅本人可确认 |
| GET/POST/PUT/DELETE | `/api/v1/agent/kb/**` | 知识库、文档上传、重建、分块预览、检索 |
| GET | `/api/v1/agent/admin/tool-calls` | 工具调用流水 |
| GET/PUT | `/api/v1/agent/admin/tools` | 工具开关、超时、允许角色 |
| GET | `/api/v1/agent/admin/stats` | 调用统计 |
| GET | `/api/v1/agent/status` | 模型 / 审计库 / 向量库 / 工具数自检 |

身份来自网关注入的 `X-FM-*` 签名头（HMAC-SHA256），本地调试可设 `FM_AGENT_DEV_MODE=1` 绕过。

## 工具

| 工具 | 类型 | 作用 |
| --- | --- | --- |
| `search_logs` | 只读 | 按 trace_id / 服务 / 关键词查 Loki |
| `check_health` | 只读 | Nacos 实例列表 + `/healthz` 探测 |
| `query_view` | 只读 | 只读白名单视图，参数化查询，禁止拼 SQL |
| `query_pipeline` | 只读 | GitHub Actions 运行状态 |
| `search_kb` | 只读 | 知识库语义检索（bge-base-zh-v1.5，768 维） |
| `trigger_release` | 写 | 触发发布流水线（workflow_dispatch），需确认 |
| `restart_service` | 写 | 重启服务（service / 环境），需确认 |

写工具标了 `requires_config`：管理台没配过（`agent_tool_config` 无记录或 `enabled=0`）时不会出现在模型面前，
避免"调了才报错"白烧一轮。新库迁移后需要管理员在管理台启用发布 / 重启工具。

## 本地开发（uv 管理依赖）

```bash
uv sync                                            # 安装依赖（含 dev 组）
cp .env.example .env                               # 按需修改，开发态默认 FM_AGENT_DEV_MODE=1
uv run uvicorn app.main:app --port 8201            # 单 worker！并发闸门在进程内
uv run pytest -q                                   # 运行测试
uv run ruff check .                                # 静态检查
```

本地依赖（Qdrant）可用部署仓库的编排起：

```bash
docker compose -f ../tf-deploy/docker-compose.llm.yml up -d qdrant
```

嵌入模型放在 `models/bge-base-zh-v1.5`（CPU 推理，不占显存）：

```bash
HF_ENDPOINT=https://hf-mirror.com HF_HUB_DISABLE_XET=1 \
  uv run python -c "from huggingface_hub import hf_hub_download as d; d('BAAI/bge-base-zh-v1.5','pytorch_model.bin',local_dir='../models/bge-base-zh-v1.5')"
```

## Docker 构建

```bash
docker build -t tf-agent .
```

镜像里不含模型权重，运行时把 `models/` 挂到 `/models:ro`，用 `FM_EMBED_MODEL_PATH` 指定。

## 关键约束

- **必须单 worker**：入口并发闸门在进程内，多 worker 会让 `max_concurrency` 失效。
- **8192 是"输入 + 输出"的总预算**：按「历史条数 → 整块丢弃旧轮次 → 工具结果按行截断」三层降级，
  不会把 prompt 顶到 vLLM 报 400；最后一轮不传工具，强制模型给出总结。
- **写操作二次确认**：模型只能发起；确认凭证是无状态 HMAC 令牌（重启不丢、防篡改、防重放），
  执行要管理台点头，审计记录确认人。
- **知识库"隐藏"= 不在列表 / 详情里展示**，文档仍参与检索；它不是权限，不要当访问控制用。
- **降级不阻断**：MySQL / Loki / Nacos 任一不可用都只让对应工具报错，服务照常对话。

## 可观测

- `GET /api/v1/agent/metrics`（Prometheus）：对话数、工具耗时 / 成败、模型每轮耗时、prompt / completion token。
- `agent_tool_call`：每次工具调用的参数摘要 / 行数 / 耗时；
  `agent_llm_call`：每轮模型调用的 token 与耗时（用于算成本、找慢轮次）。
- SSE 长任务期间每 10 秒发一行 `: ping` 注释保活，客户端可忽略。

## 升级注意

- 新增表结构后必须先跑迁移再重启业务容器：
  `uv run python -m app.migrate`（V8 起有 `agent_llm_call`）。
  没跑迁移时审计库会整体降级（对话仍可用，会话与审计不落库）。

## 分支模型

- `master`：保护分支，只接受 PR（CI 必须通过，squash 合并），合并后自动发版
- `test`：联调分支，开发分支直接合入（不走 PR），可直接推送
- 开发流：`feature/*` 开发 → 直接合入 `test` 联调 → 验证 OK 后从开发分支开 PR 合 `master` → 自动发版
