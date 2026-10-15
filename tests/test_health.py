from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)


def test_healthz():
    resp = client.get("/healthz")
    assert resp.status_code == 200
    assert resp.json()["status"] == "ok"


def test_ping():
    resp = client.get("/api/v1/agent/ping")
    assert resp.status_code == 200
    assert resp.json()["code"] == 0
