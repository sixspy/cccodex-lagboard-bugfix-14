"""空窗切断红测/验收测。

口令沿用本仓：源名录 sources、到达 arrivals、水位 watermarks、
空窗审计 gap_audits、对账附页 reconcile。
择定甲路径（见 docs/空窗切断卡.md）：指针推进与到达落行同库捆提交，
遇空整段撤销并记空窗审计，正式水位零推进。
"""
import sqlite3
import threading

import pytest
from fastapi.testclient import TestClient

from app import app, init_db, DB_PATH, connect


SRC = "订单源"


@pytest.fixture(autouse=True)
def _db(tmp_path, monkeypatch):
    monkeypatch.setattr("app.DB_PATH", tmp_path / "t.db")
    init_db()
    yield


@pytest.fixture()
def client():
    c = TestClient(app)
    token = c.post("/api/login", json={"username": "admin", "password": "123456"}).json()["token"]
    c.headers.update({"Authorization": "Bearer " + token})
    return c


def _watermark(source=SRC):
    with connect() as c:
        row = c.execute("SELECT watermark_clock FROM watermarks WHERE source=?", (source,)).fetchone()
    return None if row is None else row["watermark_clock"]


def _audit_kinds():
    with connect() as c:
        return [r["kind"] for r in c.execute("SELECT kind FROM gap_audits ORDER BY id").fetchall()]


def _ensure_scene_tables():
    """红测现场表：复现夹具需要一张可被单独抬指针的水位表。

    用 IF NOT EXISTS，实现落库后同名列结构不冲突。
    """
    with connect() as c:
        c.execute("""CREATE TABLE IF NOT EXISTS watermarks(
            source TEXT PRIMARY KEY,
            watermark_clock TEXT NOT NULL,
            updated_at TEXT NOT NULL)""")
        c.execute("""CREATE TABLE IF NOT EXISTS gap_audits(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            source TEXT NOT NULL,
            kind TEXT NOT NULL,
            detail TEXT NOT NULL,
            created_at TEXT NOT NULL)""")


def _poison_gap_fixture(client, source=SRC):
    """空窗夹具：把指针（摄入水位）抬过，但不写任何到达落行业。

    模拟“指针读数抬过却无到达行”的现场。只允许测试夹具直接写库，
    正式写路径不得提供这种只抬指针的入口。
    """
    _ensure_scene_tables()
    # 先有一次真实到达，把基线水位立起来
    r = client.post("/api/arrivals", json={
        "source": source,
        "source_clock": "2026-11-16T09:00:00",
        "ingest_clock": "2026-11-16T09:10:00",
    })
    assert r.status_code == 200
    with connect() as c:
        c.execute(
            "INSERT OR REPLACE INTO watermarks(source, watermark_clock, updated_at) VALUES (?,?,?)",
            (source, "2026-11-16T09:10:00", "2026-11-16T09:10:00"),
        )
    # 源处于冻结待补齐态：只有误放绿灯才会把它翻成可写
    with connect() as c:
        c.execute("UPDATE sources SET output_mode='冻结' WHERE name=?", (source,))
    # 只抬指针数字，不落到达行 —— 制造空窗
    with connect() as c:
        c.execute(
            "UPDATE watermarks SET watermark_clock=?, updated_at=? WHERE source=?",
            ("2026-11-16T10:00:00", "2026-11-16T10:00:00", source),
        )
    assert _watermark(source) == "2026-11-16T10:00:00"
    with connect() as c:
        latest = c.execute(
            "SELECT MAX(ingest_clock) m FROM arrivals WHERE source=?", (source,)
        ).fetchone()["m"]
    assert latest == "2026-11-16T09:10:00"  # 指针 10:00 已越过最新到达 09:10


# ---------------------------------------------------------------------------
# 红测一：空窗误放绿灯必须拦下
# ---------------------------------------------------------------------------

def test_gap_must_not_light_green(client):
    _poison_gap_fixture(client)
    # 空窗期间补齐对账不得恢复“可写”绿灯
    r = client.post(f"/api/catchup/{SRC}")
    assert r.status_code == 409, "空窗未切断：无到达行却允许补齐亮绿灯"
    with connect() as c:
        mode = c.execute("SELECT output_mode FROM sources WHERE name=?", (SRC,)).fetchone()["output_mode"]
    assert mode != "可写", "空窗误报绿灯：输出态被改成可写"
    kinds = _audit_kinds()
    assert "空窗审计" in kinds, "空窗拦下后缺少空窗审计留痕"


def test_claim_without_arrival_is_rolled_back(client):
    """同库捆提交：缺到达落行/对不齐时整段撤销，正式水位零推进并记审计。"""
    _ensure_scene_tables()
    before = _watermark(SRC)
    r = client.post("/api/arrivals", json={
        "source": SRC,
        "source_clock": "2026-11-16T11:00:00",
        "ingest_clock": "2026-11-16T10:30:00",  # 早于当前水位，对不齐
    })
    assert r.status_code in (409, 422)
    assert _watermark(SRC) == before, "整段撤销失败：水位被推进"
    with connect() as c:
        n = c.execute("SELECT COUNT(*) c FROM arrivals WHERE source=? AND ingest_clock=?",
                      (SRC, "2026-11-16T10:30:00")).fetchone()["c"]
    assert n == 0, "整段撤销失败：到达行残留"
    assert "空窗审计" in _audit_kinds()


# ---------------------------------------------------------------------------
# 红测二：双席抢同一源指针，后交只记水位空窗争用
# ---------------------------------------------------------------------------

def test_dual_seat_contention_leaves_gap_lock_trace(client):
    import app as appmod

    _ensure_scene_tables()
    assert hasattr(appmod, "source_locks"), "缺空窗锁：调度席与对账席无法占锁抢针"
    held = appmod.source_locks[SRC]
    barrier = threading.Barrier(2)

    def seat():
        with held:
            barrier.wait()           # 等两席都到这里，持锁席不放
            barrier.wait()

    t = threading.Thread(target=seat)
    t.start()
    barrier.wait()                    # 另一席（后台线程）已持锁
    try:
        r = client.post("/api/arrivals", json={
            "source": SRC,
            "source_clock": "2026-11-16T12:00:00",
            "ingest_clock": "2026-11-16T12:05:00",
        })
        assert r.status_code == 409, "后交席未被空窗锁拦下"
    finally:
        barrier.wait()
        t.join()
    kinds = _audit_kinds()
    assert "空窗争用" in kinds, "缺空窗争用留痕"
    # 争用失败方不得推进水位
    assert _watermark(SRC) != "2026-11-16T12:05:00"


def test_concurrent_claims_one_wins_one_contention(tmp_path, monkeypatch):
    """真实并发：两席同时抢，恰一席成功，另一席留争用痕且零推进。

    TestClient 带 portal 锁会把跨线程请求串行化，无法验证真实占锁，
    因此这里起一个真实 uvicorn 服务，用 httpx 跨线程打 HTTP。
    """
    import uvicorn
    import httpx
    import app as appmod
    import gapcut

    db_file = tmp_path / "t.db"
    monkeypatch.setattr("app.DB_PATH", db_file)
    init_db()
    appmod.source_locks[SRC] = type(appmod.source_locks[SRC])()  # 干净锁

    entered = threading.Event()
    proceed = threading.Event()
    real_advance = gapcut.advance_watermark

    def gated_advance(conn, source, ingest_clock):
        entered.set()
        proceed.wait(5)
        return real_advance(conn, source, ingest_clock)

    appmod.gapcut.advance_watermark = gated_advance
    config = uvicorn.Config(appmod.app, host="127.0.0.1", port=10899, log_level="warning")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    results = []
    try:
        thread.start()
        for _ in range(50):
            try:
                if httpx.get("http://127.0.0.1:10899/api/health", timeout=1).status_code == 200:
                    break
            except Exception:
                pass
            threading.Event().wait(0.1)
        with httpx.Client(base_url="http://127.0.0.1:10899", timeout=10) as http:
            token = http.post("/api/login", json={"username": "admin", "password": "123456"}).json()["token"]
            headers = {"Authorization": "Bearer " + token}

            def seat(ingest):
                r = http.post("/api/arrivals", headers=headers, json={
                    "source": SRC,
                    "source_clock": "2026-11-16T13:00:00",
                    "ingest_clock": ingest,
                })
                results.append((ingest, r.status_code))

            t1 = threading.Thread(target=seat, args=("2026-11-16T13:05:00",))
            t2 = threading.Thread(target=seat, args=("2026-11-16T13:06:00",))
            t1.start()
            assert entered.wait(5), "先交席未进入持锁临界区"
            t2.start()                        # 后交席在持锁期间抢同一源指针
            t2.join(8)
            assert not t2.is_alive(), "后交席应立即被非阻塞空窗锁拒绝"
            proceed.set()
            t1.join(8)
    finally:
        appmod.gapcut.advance_watermark = real_advance
        server.should_exit = True
        thread.join(5)
    statuses = sorted(s for _, s in results)
    assert statuses[0] == 200 and statuses[1] == 409, results
    with sqlite3.connect(db_file) as raw:
        kinds = [r[0] for r in raw.execute("SELECT kind FROM gap_audits ORDER BY id").fetchall()]
        wm = raw.execute("SELECT watermark_clock FROM watermarks WHERE source=?", (SRC,)).fetchone()[0]
    assert "空窗争用" in kinds, "缺空窗争用留痕"
    assert wm == "2026-11-16T13:05:00", "后交席不得推进水位"


# ---------------------------------------------------------------------------
# 验收：绿后落库，读数/详情/名录/任务/对账附页禁止空值
# ---------------------------------------------------------------------------

def test_arrival_and_watermark_commit_together(client):
    r = client.post("/api/arrivals", json={
        "source": SRC,
        "source_clock": "2026-11-16T14:00:00",
        "ingest_clock": "2026-11-16T14:08:00",
    })
    assert r.status_code == 200
    body = r.json()
    assert body["watermark_clock"] == "2026-11-16T14:08:00"
    assert _watermark(SRC) == "2026-11-16T14:08:00"
    with connect() as c:
        n = c.execute("SELECT COUNT(*) c FROM arrivals WHERE source=? AND ingest_clock=?",
                      (SRC, "2026-11-16T14:08:00")).fetchone()["c"]
    assert n == 1


def test_reconcile_page_has_no_nulls(client):
    client.post("/api/arrivals", json={
        "source": SRC,
        "source_clock": "2026-11-16T15:00:00",
        "ingest_clock": "2026-11-16T15:07:00",
    })
    r = client.get(f"/api/reconcile/{SRC}")
    assert r.status_code == 200
    page = r.json()
    # 对账附页所有字段非空；列表为空时也必须是显式空集合而非 null
    for key in ("source", "watermark_clock", "latest_arrival_clock", "has_gap", "arrivals", "audits"):
        assert key in page and page[key] is not None, f"对账附页出现空窗字段: {key}"
    assert isinstance(page["arrivals"], list) and isinstance(page["audits"], list)
    assert page["has_gap"] is False
    assert page["watermark_clock"] == page["latest_arrival_clock"] == "2026-11-16T15:07:00"
    for a in page["arrivals"]:
        assert all(v is not None and v != "" for v in a.values())


def test_sources_and_task_lists_have_no_nulls(client):
    r = client.get("/api/sources")
    assert r.status_code == 200
    for s in r.json()["items"]:
        assert all(v is not None and v != "" for v in s.values())
    r = client.get("/api/arrivals")
    assert r.status_code == 200
    assert isinstance(r.json()["items"], list)
    for a in r.json()["items"]:
        assert all(v is not None and v != "" for v in a.values())
    r = client.get("/api/catchups")
    assert r.status_code == 200
    assert isinstance(r.json()["items"], list)


def test_unlocked_advance_is_deviation(client):
    """偏离空窗切断卡（未占锁抬指针）须拦下并记“空窗偏了”，正式水位零推进。"""
    import gapcut

    _ensure_scene_tables()
    before = _watermark(SRC)
    with connect() as c:
        with pytest.raises(RuntimeError):
            gapcut.advance_watermark(c, SRC, "2026-11-16T20:00:00")  # 未占锁
    assert _watermark(SRC) == before, "偏离路径不得推进水位"


def test_catchup_allowed_after_gap_closed(client):
    _poison_gap_fixture(client)
    # 空窗被真实到达补齐：到达摄入时钟追平水位
    r = client.post("/api/arrivals", json={
        "source": SRC,
        "source_clock": "2026-11-16T09:50:00",
        "ingest_clock": "2026-11-16T10:00:00",
    })
    assert r.status_code == 200
    r = client.post(f"/api/catchup/{SRC}")
    assert r.status_code == 200
    assert r.json()["output_mode"] == "可写"
