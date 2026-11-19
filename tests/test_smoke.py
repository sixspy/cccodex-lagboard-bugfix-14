
import pytest
from fastapi.testclient import TestClient
from app import app, init_db

@pytest.fixture(autouse=True)
def _db(tmp_path, monkeypatch):
    monkeypatch.setattr("app.DB_PATH", tmp_path/"t.db"); init_db(); yield

def test_lag_freeze_and_catchup():
    c = TestClient(app)
    assert c.get("/api/health").json()["product"] == "源到达SLA"
    h = {"Authorization": "Bearer " + c.post("/api/login", json={"username":"admin","password":"123456"}).json()["token"]}
    r = c.post("/api/arrivals", headers=h, json={"source":"订单源","source_clock":"2026-11-16T10:00:00","ingest_clock":"2026-11-16T11:00:00"})
    assert r.status_code == 200 and r.json()["output_mode"] == "冻结"
    assert c.post("/api/catchup/订单源", headers=h).json()["output_mode"] == "可写"
