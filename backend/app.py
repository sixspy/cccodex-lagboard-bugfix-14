
import hashlib, secrets, sqlite3
from pathlib import Path
from fastapi import Header, HTTPException
from pydantic import BaseModel

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data"
DB_PATH = DATA / "app.db"
FRONTEND = ROOT / "frontend"
SESSIONS: dict[str, str] = {}

def connect():
    DATA.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
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

@app.post("/api/arrivals")
def add_arrival(body: ArrivalIn, authorization: str | None = Header(default=None)):
    require_user(authorization)
    with connect() as c:
        src = c.execute("SELECT * FROM sources WHERE name=?", (body.source,)).fetchone()
        if not src: raise HTTPException(404, "未知源")
        # lag by minute rough parse
        def parse(t):
            return datetime.fromisoformat(t)
        lag = int((parse(body.ingest_clock) - parse(body.source_clock)).total_seconds() // 60)
        if lag < 0: lag = 0
        lv = level_of(lag, src["sla_minutes"])
        mode = mode_of(lv)
        c.execute("INSERT INTO arrivals(source,source_clock,ingest_clock,lag_minutes,level) VALUES (?,?,?,?,?)",
                  (body.source, body.source_clock, body.ingest_clock, lag, lv))
        c.execute("UPDATE sources SET output_mode=? WHERE name=?", (mode, body.source))
        return {"lag_minutes": lag, "level": lv, "output_mode": mode}

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
        c.execute("UPDATE sources SET output_mode='可写' WHERE name=?", (source,))
        c.execute("INSERT INTO catchups(source,status,restored_mode,note,created_at) VALUES (?,?,?,?,?)",
                  (source, "成功", "可写", "补齐后恢复可写", now))
        return {"ok": True, "output_mode": "可写"}

@app.get("/api/catchups")
def catchups(authorization: str | None = Header(default=None)):
    require_user(authorization)
    with connect() as c:
        return {"items":[dict(r) for r in c.execute("SELECT * FROM catchups ORDER BY id DESC").fetchall()]}

@app.get("/")
def index():
    return FileResponse(FRONTEND / "index.html")
app.mount("/static", StaticFiles(directory=str(FRONTEND)), name="static")
