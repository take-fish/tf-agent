# tf-agent

FishMatrix（摸鱼矩阵）Agent 智能助手服务 —— Python 3.13 + FastAPI。

- 角色：对接大模型 API，提供代码式问答、风险评估等能力
- 端口：`8201`
- 健康检查：`GET /healthz`

## 本地开发（uv 管理依赖）

```bash
uv sync                                            # 安装依赖（含 dev 组）
uv run uvicorn app.main:app --port 8201 --reload   # 启动开发服务
uv run pytest -q                                   # 运行测试
uv run ruff check .                                # 静态检查
```

## Docker 构建

```bash
docker build -t tf-agent .
```

## 分支模型

- `master`：保护分支，只接受 PR（CI 必须通过），合并后自动发版
- `test`：联调分支，可直接推送
- 开发流：`feature/*` → PR 到 `test` → 联调 → PR 到 `master` → 自动发版
