
import hashlib, secrets, sqlite3
from pathlib import Path
from fastapi import Header, HTTPException
from pydantic import BaseModel

import gapcut
from gapcut import source_locks  # 以源为粒度的空窗锁（空窗切断卡择定甲路径）

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data"
DB_PATH = DATA / "app.db"
FRONTEND = ROOT / "frontend"
SESSIONS: dict[str, str] = {}

def connect():
    DATA.mkdir(parents=True, exist_ok=True)
    # timeout 即 busy_timeout：双席抢针时后交席的争用留痕需等待持锁席提交，
    # 避免 SQLite 写锁互斥直接抛 database is locked（空窗争用留痕不可丢）
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=30000")
    # WAL 让读写/写写并发更稳；受限环境（只读挂载等）切不了模式时回退默认日志，
    # busy_timeout 仍保证争用留痕会等待而非立刻报错。
    try:
        conn.execute("PRAGMA journal_mode=WAL")
    except sqlite3.OperationalError:
        pass
    return conn

def sha(p: str) -> str:
    return hashlib.sha256(p.encode("utf-8")).hexdigest()

def ensure_users(conn):
    conn.execute("""CREATE TABLE IF NOT EXISTS users(
        username TEXT PRIMARY KEY, password_sha TEXT NOT NULL, role TEXT NOT NULL)""")
    if conn.execute("SELECT COUNT(*) c FROM users").fetchone()["c"] == 0:
        conn.execute("INSERT INTO users VALUES (?,?,?)", ("admin", sha("123456"), "管理员"))
        conn.execute("INSERT INTO users VALUES (?,?,?)", ("viewer", sha("view123"), "只读"))

class LoginIn(BaseModel):
    username: str
    password: str

def require_user(authorization: str | None) -> str:
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(401, "未登录")
    u = SESSIONS.get(authorization.removeprefix("Bearer ").strip())
    if not u:
        raise HTTPException(401, "登录已失效")
    return u

from datetime import datetime
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

PRODUCT = "源到达SLA"
SKELETON = "多源到达时钟对照 SLA 分级，超限则冻结或降级输出，补齐后恢复并对账"

app = FastAPI(title=PRODUCT)
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_credentials=True, allow_methods=["*"], allow_headers=["*"])

def init_db():
    with connect() as conn:
        ensure_users(conn)
        conn.executescript("""
        CREATE TABLE IF NOT EXISTS sources(
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          name TEXT UNIQUE NOT NULL,
          sla_minutes INTEGER NOT NULL,
          output_mode TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS arrivals(
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          source TEXT NOT NULL,
          source_clock TEXT NOT NULL,
          ingest_clock TEXT NOT NULL,
          lag_minutes INTEGER NOT NULL,
          level TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS catchups(
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          source TEXT NOT NULL,
          status TEXT NOT NULL,
          restored_mode TEXT NOT NULL,
          note TEXT NOT NULL,
          created_at TEXT NOT NULL
        );
        """)
        if conn.execute("SELECT COUNT(*) c FROM sources").fetchone()["c"] == 0:
            conn.executemany("INSERT INTO sources(name,sla_minutes,output_mode) VALUES (?,?,?)", [
                ("订单源", 15, "可写"), ("支付源", 20, "降级只读"), ("物流源", 30, "冻结"),
            ])
            conn.execute("INSERT INTO arrivals(source,source_clock,ingest_clock,lag_minutes,level) VALUES (?,?,?,?,?)",
                         ("支付源","2026-11-16T08:00:00","2026-11-16T08:35:00",35,"告警"))
            conn.execute("INSERT INTO arrivals(source,source_clock,ingest_clock,lag_minutes,level) VALUES (?,?,?,?,?)",
                         ("物流源","2026-11-16T07:00:00","2026-11-16T09:10:00",130,"违约"))
        # 空窗切断：源名录立名录即立水位基线，并登记每源空窗锁
        gapcut.ensure_gap_schema(conn)

def level_of(lag, sla):
    if lag <= sla: return "正常"
    if lag <= sla * 2: return "告警"
    return "违约"

def mode_of(level):
    return {"正常":"可写", "告警":"降级只读", "违约":"冻结"}[level]

@app.on_event("startup")
def _s(): init_db()

@app.get("/api/health")
def health():
    with connect() as c:
        n = c.execute("SELECT COUNT(*) c FROM sources").fetchone()["c"]
    return {"ok": True, "product": PRODUCT, "skeleton": SKELETON, "sources": n, "db": str(DB_PATH)}

@app.post("/api/login")
def login(body: LoginIn):
    with connect() as c:
        row = c.execute("SELECT username,role FROM users WHERE username=? AND password_sha=?", (body.username, sha(body.password))).fetchone()
    if not row: raise HTTPException(401, "账号或密码错误")
    tok = secrets.token_hex(16); SESSIONS[tok]=row["username"]
    return {"token": tok, "username": row["username"], "role": row["role"]}

@app.get("/api/sources")
def sources(authorization: str | None = Header(default=None)):
    require_user(authorization)
    with connect() as c:
        return {"items":[dict(r) for r in c.execute("SELECT * FROM sources ORDER BY id").fetchall()]}

class ArrivalIn(BaseModel):
    source: str
    source_clock: str
    ingest_clock: str

class _GapReject(Exception):
    """遇空：已在独立事务记审计，正式水位零推进，对调用面回 409。"""
    def __init__(self, detail: str):
        super().__init__(detail)
        self.detail = detail


@app.post("/api/arrivals")
def add_arrival(body: ArrivalIn, authorization: str | None = Header(default=None)):
    require_user(authorization)
    conn = connect()
    try:
        src = conn.execute("SELECT * FROM sources WHERE name=?", (body.source,)).fetchone()
        if not src:
            raise HTTPException(404, "未知源")
        # 甲路径：双席抢同一源指针须占空窗锁，后交只记水位空窗争用
        lock = gapcut.lock_for(body.source)
        if not lock.acquire(blocking=False):
            conn.rollback(); conn.close()
            with connect() as audit_conn:
                gapcut.record_audit(audit_conn, body.source, "空窗争用",
                                    "调度席与对账席同时抢同一源指针，后交零推进")
            raise HTTPException(409, "空窗争用：该源指针正被占用，本次水位零推进")
        try:
            # 校验时钟与水位对齐；对不齐即遇空，整段撤销
            try:
                source_dt = datetime.fromisoformat(body.source_clock)
                ingest_dt = datetime.fromisoformat(body.ingest_clock)
            except ValueError:
                raise HTTPException(422, "时钟格式无法解析")
            if ingest_dt < source_dt:
                _reject_gap(conn, body.source, "摄入时钟早于源时钟，指针与到达对不齐")
            lag = int((ingest_dt - source_dt).total_seconds() // 60)
            if lag < 0:
                lag = 0
            current = gapcut.current_watermark(conn, body.source)
            if current and ingest_dt < datetime.fromisoformat(current):
                _reject_gap(conn, body.source, "新指针早于当前水位，禁止回抬造成空窗")
            lv = level_of(lag, src["sla_minutes"])
            mode = mode_of(lv)
            # 指针推进与到达落行同库捆提交
            conn.execute("INSERT INTO arrivals(source,source_clock,ingest_clock,lag_minutes,level) VALUES (?,?,?,?,?)",
                         (body.source, body.source_clock, body.ingest_clock, lag, lv))
            try:
                gapcut.advance_watermark(conn, body.source, body.ingest_clock)
            except RuntimeError:
                # 偏离空窗切断卡：未占锁即抬指针，整段撤销并记“空窗偏了”
                conn.rollback(); conn.close()
                with connect() as audit_conn:
                    gapcut.record_audit(audit_conn, body.source, "空窗偏了",
                                        "未占源空窗锁即推进水位，已拦下，正式水位零推进")
                raise HTTPException(409, "空窗偏了：未占锁抬指针已拦下，水位零推进")
            conn.execute("UPDATE sources SET output_mode=? WHERE name=?", (mode, body.source))
            conn.commit()
            return {"lag_minutes": lag, "level": lv, "output_mode": mode,
                    "watermark_clock": body.ingest_clock}
        finally:
            lock.release()
    except _GapReject as exc:
        raise HTTPException(409, exc.detail)
    finally:
        try:
            conn.close()
        except sqlite3.ProgrammingError:
            pass


def _reject_gap(conn, source: str, detail: str):
    """遇空：整段撤销（正式水位零推进），审计在独立事务留痕后抛出。"""
    conn.rollback()
    conn.close()
    with connect() as audit_conn:
        gapcut.record_audit(audit_conn, source, "空窗审计", detail)
    raise _GapReject("空窗审计：" + detail + "，已整段撤销")

@app.get("/api/arrivals")
def arrivals(authorization: str | None = Header(default=None)):
    require_user(authorization)
    with connect() as c:
        return {"items":[dict(r) for r in c.execute("SELECT * FROM arrivals ORDER BY id DESC").fetchall()]}

@app.post("/api/catchup/{source}")
def catchup(source: str, authorization: str | None = Header(default=None)):
    require_user(authorization)
    now = datetime.now().isoformat(timespec="seconds")
    with connect() as c:
        src = c.execute("SELECT * FROM sources WHERE name=?", (source,)).fetchone()
        if not src: raise HTTPException(404, "未知源")
        # 空窗切断：指针抬过却无对齐到达行时，禁止误报绿灯
        if gapcut.has_gap(c, source):
            detail = "存在未闭合空窗（水位已越过而无对应到达行），禁止补齐亮绿灯"
            gapcut.record_audit(c, source, "空窗审计", detail)
            c.commit()
            raise HTTPException(409, detail)
        c.execute("UPDATE sources SET output_mode='可写' WHERE name=?", (source,))
        c.execute("INSERT INTO catchups(source,status,restored_mode,note,created_at) VALUES (?,?,?,?,?)",
                  (source, "成功", "可写", "补齐后恢复可写", now))
        c.commit()
        return {"ok": True, "output_mode": "可写"}

@app.get("/api/reconcile/{source}")
def reconcile(source: str, authorization: str | None = Header(default=None)):
    """对账附页：水位读数与到达详情对齐结论，所有字段非空（禁止空窗）。"""
    require_user(authorization)
    with connect() as c:
        src = c.execute("SELECT * FROM sources WHERE name=?", (source,)).fetchone()
        if not src: raise HTTPException(404, "未知源")
        watermark = gapcut.current_watermark(c, source)
        latest = gapcut.latest_arrival_clock(c, source)
        gap = gapcut.has_gap(c, source)
        arrivals = [dict(r) for r in c.execute(
            "SELECT id,source,source_clock,ingest_clock,lag_minutes,level "
            "FROM arrivals WHERE source=? ORDER BY id DESC", (source,)).fetchall()]
        audits = [dict(r) for r in c.execute(
            "SELECT id,source,kind,detail,created_at FROM gap_audits WHERE source=? ORDER BY id DESC",
            (source,)).fetchall()]
    return {
        "source": source,
        "watermark_clock": watermark,          # 无数据时为显式 ""，非 null
        "latest_arrival_clock": latest,
        "has_gap": gap,
        "output_mode": src["output_mode"],
        "arrivals": arrivals,                  # 显式空集合，非 null
        "audits": audits,
    }

@app.get("/api/catchups")
def catchups(authorization: str | None = Header(default=None)):
    require_user(authorization)
    with connect() as c:
        return {"items":[dict(r) for r in c.execute("SELECT * FROM catchups ORDER BY id DESC").fetchall()]}

@app.get("/")
def index():
    return FileResponse(FRONTEND / "index.html")
app.mount("/static", StaticFiles(directory=str(FRONTEND)), name="static")
