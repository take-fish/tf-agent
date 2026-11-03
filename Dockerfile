FROM ghcr.io/astral-sh/uv:0.13.1 AS uv

FROM python:3.13-slim
COPY --from=uv /uv /bin/uv
WORKDIR /app
# 嵌入模型（bge）只跑 CPU，但 uv.lock 里钉的是默认 torch 轮子（Linux 上带一整套
# nvidia 运行时，镜像约 5 GB）。想瘦身要单独生成 CPU 版 lock（uv lock --torch-backend cpu），
# 属于独立改动，这里保持与本地/CI 一致的锁文件。
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev
COPY app ./app
# 迁移 SQL 必须进镜像：建表由 `python -m app.migrate` 以 fm_agent_migrate 身份执行，
# 业务容器本身用 fm_agent_app（仅 DML），启动时只校验表存在。
COPY migrations ./migrations
EXPOSE 8201
CMD ["uv", "run", "--no-dev", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8201"]
