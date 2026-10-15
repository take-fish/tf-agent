import os

from fastapi import FastAPI

app = FastAPI(
    title="tf-agent",
    docs_url="/api/v1/agent/docs",
    openapi_url="/api/v1/agent/openapi.json",
)


@app.get("/healthz")
def healthz() -> dict:
    return {"status": "ok", "service": "tf-agent", "version": os.getenv("APP_VERSION", "dev")}


# 伪装路径示例：Agent 探活（运维工具风格）
@app.get("/api/v1/agent/ping")
def ping() -> dict:
    return {
        "code": 0,
        "msg": "success",
        "data": {"service": "tf-agent", "version": os.getenv("APP_VERSION", "dev")},
    }
