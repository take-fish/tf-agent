FROM ghcr.io/astral-sh/uv:0.13.1 AS uv

FROM python:3.13-slim
COPY --from=uv /uv /bin/uv
WORKDIR /app
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev
COPY app ./app
# 迁移 SQL 必须进镜像：建表由 `python -m app.migrate` 以 fm_agent_migrate 身份执行，
# 业务容器本身用 fm_agent_app（仅 DML），启动时只校验表存在。
COPY migrations ./migrations
EXPOSE 8201
CMD ["uv", "run", "--no-dev", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8201"]
