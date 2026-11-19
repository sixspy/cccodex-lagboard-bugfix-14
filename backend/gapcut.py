"""空窗误放切断（甲路径，封档见 docs/空窗切断卡.md）。

甲路径：指针（摄入水位）推进与到达落行必须同库捆提交；遇空则整段撤销、
记空窗审计，正式水位零推进。全仓不引入乙路径的影子卷机制。

双席（调度席/对账席）抢同一源指针时，以源为粒度占空窗锁：
后交者不推进，只在 gap_audits 记“空窗争用”，由调用面返回 409。
"""
from __future__ import annotations

import threading
from datetime import datetime

# 以源为粒度的空窗锁。源名录初始化后所有源都在此登记，
# 未知源访问时按需补登记（源名录本身仍是唯一来源）。
source_locks: dict[str, threading.RLock] = {}
_locks_guard = threading.Lock()


def lock_for(source: str) -> threading.RLock:
    lock = source_locks.get(source)
    if lock is None:
        with _locks_guard:
            lock = source_locks.get(source)
            if lock is None:
                lock = threading.RLock()
                source_locks[source] = lock
    return lock


def now_stamp() -> str:
    return datetime.now().isoformat(timespec="seconds")


def ensure_gap_schema(conn) -> None:
    """水位表与空窗审计表；列均 NOT NULL，禁止空窗列。"""
    conn.execute("""CREATE TABLE IF NOT EXISTS watermarks(
        source TEXT PRIMARY KEY,
        watermark_clock TEXT NOT NULL,
        updated_at TEXT NOT NULL)""")
    conn.execute("""CREATE TABLE IF NOT EXISTS gap_audits(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        source TEXT NOT NULL,
        kind TEXT NOT NULL,
        detail TEXT NOT NULL,
        created_at TEXT NOT NULL)""")
    for row in conn.execute("SELECT name FROM sources ORDER BY id").fetchall():
        source = row["name"]
        exists = conn.execute(
            "SELECT 1 FROM watermarks WHERE source=?", (source,)
        ).fetchone()
        if exists is None:
            # 新源立名录即立水位基线；没有任何到达时给显式基线而非 null
            conn.execute(
                "INSERT INTO watermarks(source, watermark_clock, updated_at) VALUES (?,?,?)",
                (source, "", now_stamp()),
            )
        lock_for(source)


def record_audit(conn, source: str, kind: str, detail: str) -> None:
    """空窗留痕。kind ∈ 空窗审计 / 空窗争用 / 空窗偏了。"""
    conn.execute(
        "INSERT INTO gap_audits(source, kind, detail, created_at) VALUES (?,?,?,?)",
        (source, kind, detail, now_stamp()),
    )


def current_watermark(conn, source: str) -> str:
    row = conn.execute(
        "SELECT watermark_clock FROM watermarks WHERE source=?", (source,)
    ).fetchone()
    return "" if row is None else row["watermark_clock"]


def latest_arrival_clock(conn, source: str) -> str:
    row = conn.execute(
        "SELECT MAX(ingest_clock) m FROM arrivals WHERE source=?", (source,)
    ).fetchone()
    return "" if row is None or row["m"] is None else row["m"]


def has_gap(conn, source: str) -> bool:
    """空窗：指针已抬过（非空基线），却没有对齐到该水位的到达落行业。"""
    wm = current_watermark(conn, source)
    if wm == "":
        return False
    return latest_arrival_clock(conn, source) != wm


def holds_source_lock(source: str) -> bool:
    """当前线程是否持有该源的空窗锁。

    RLock 用原生 _is_owned() 判定；不能用 acquire/release 探测——
    那会平衡掉外层已持有的重入计数，把合法持锁误判成未持锁。
    """
    lock = lock_for(source)
    is_owned = getattr(lock, "_is_owned", None)
    if is_owned is not None:
        return bool(is_owned())
    # 兜底：普通 Lock 无可重入计数，acquire 成功即未持有
    if lock.acquire(blocking=False):
        lock.release()
        return False
    return True


def advance_watermark(conn, source: str, ingest_clock: str) -> None:
    """仅允许在已持有源空窗锁、且与到达落行同一事务内调用。

    偏离空窗切断卡（未占锁即推进指针）由调用面拦下并记“空窗偏了”，
    本函数只做硬断言，确保任何旁路都无法只抬指针数字。
    """
    if not holds_source_lock(source):
        raise RuntimeError("空窗偏了：水位推进必须在源空窗锁内与到达落行同库提交")
    conn.execute(
        "UPDATE watermarks SET watermark_clock=?, updated_at=? WHERE source=?",
        (ingest_clock, now_stamp(), source),
    )
