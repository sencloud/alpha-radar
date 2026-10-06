"""结果库（SQLite）：调度状态、语料库、回测结果。

为什么用 SQLite 而不是 CSV/JSON：
- 无人值守跑几个月会产生上万行结果，需要按品种/策略/周期筛选与排序；
- 同机已有 lastdays 用 SQLite 的先例，备份/排查方式一致；
- 单文件，`workbench download` 就能拉下来做离线分析。

写入用 WAL + 短事务：调度器（systemd timer）与 Web 控制台可能同时访问。
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path

from .config import DB_PATH

SCHEMA = """
CREATE TABLE IF NOT EXISTS runs(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  kind TEXT NOT NULL,          -- harvest | matrix | trigger
  started_at TEXT, finished_at TEXT, status TEXT,
  note TEXT, n_ok INTEGER DEFAULT 0, n_err INTEGER DEFAULT 0, n_skip INTEGER DEFAULT 0
);
CREATE TABLE IF NOT EXISTS scripts(
  sid TEXT PRIMARY KEY, slug TEXT, title TEXT, author TEXT, agree INTEGER,
  kind TEXT, access TEXT, lines INTEGER, file TEXT,
  first_seen TEXT, last_seen TEXT
);
CREATE TABLE IF NOT EXISTS results(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  run_id INTEGER, ts TEXT, symbol TEXT, name TEXT, market TEXT,
  strategy TEXT, freq TEXT, start TEXT, "end" TEXT,
  trades INTEGER, win_rate REAL, pf REAL, avg_points REAL, total_pnl REAL,
  max_dd REAL, ret_dd REAL, pos_years INTEGER, years INTEGER,
  status TEXT, error TEXT, report TEXT
);
CREATE INDEX IF NOT EXISTS idx_res_sym ON results(symbol, strategy, freq, ts);
CREATE TABLE IF NOT EXISTS state(k TEXT PRIMARY KEY, v TEXT, ts TEXT);

-- 全市场任务队列：品种 × 策略 × 周期 一个格子，按 next_due 轮转
CREATE TABLE IF NOT EXISTS instruments(
  symbol TEXT PRIMARY KEY, name TEXT, market TEXT,
  freqs TEXT,                 -- 逗号分隔的可用周期
  active INTEGER DEFAULT 1, updated_at TEXT
);
CREATE TABLE IF NOT EXISTS tasks(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  symbol TEXT NOT NULL, strategy TEXT NOT NULL, freq TEXT NOT NULL,
  state TEXT DEFAULT 'pending',      -- pending | running | done | error
  next_due TEXT, attempts INTEGER DEFAULT 0,
  last_status TEXT, last_ts TEXT, last_error TEXT,
  UNIQUE(symbol, strategy, freq)
);
CREATE INDEX IF NOT EXISTS idx_tasks_due ON tasks(next_due);
CREATE INDEX IF NOT EXISTS idx_tasks_state ON tasks(state, next_due);
"""

RESULT_COLS = ("run_id", "ts", "symbol", "name", "market", "strategy", "freq",
               "start", "end", "trades", "win_rate", "pf", "avg_points",
               "total_pnl", "max_dd", "ret_dd", "pos_years", "years",
               "status", "error", "report")


@contextmanager
def connect(path: Path | None = None):
    con = sqlite3.connect(str(path or DB_PATH), timeout=20)
    con.row_factory = sqlite3.Row
    try:
        con.execute("PRAGMA journal_mode=WAL")
        con.execute("PRAGMA busy_timeout=20000")
        yield con
        con.commit()
    finally:
        con.close()


def init(path: Path | None = None) -> None:
    with connect(path) as con:
        con.executescript(SCHEMA)


# ---------- runs ----------
def start_run(kind: str, note: str = "", path: Path | None = None) -> int:
    from datetime import datetime
    with connect(path) as con:
        cur = con.execute(
            "INSERT INTO runs(kind, started_at, status, note) VALUES(?,?,?,?)",
            (kind, datetime.now().isoformat(timespec="seconds"), "running", note))
        return int(cur.lastrowid)


def finish_run(run_id: int, status: str, n_ok: int = 0, n_err: int = 0,
               n_skip: int = 0, note: str = "", path: Path | None = None) -> None:
    from datetime import datetime
    with connect(path) as con:
        con.execute(
            "UPDATE runs SET finished_at=?, status=?, n_ok=?, n_err=?, n_skip=?, "
            "note=CASE WHEN ?='' THEN note ELSE ? END WHERE id=?",
            (datetime.now().isoformat(timespec="seconds"), status, n_ok, n_err,
             n_skip, note, note, run_id))


def recent_runs(limit: int = 30, path: Path | None = None) -> list[dict]:
    with connect(path) as con:
        rows = con.execute(
            "SELECT * FROM runs ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
    return [dict(r) for r in rows]


# ---------- scripts ----------
def upsert_scripts(scripts, path: Path | None = None) -> tuple[int, int]:
    """写入语料库，返回 (总数, 本次新增)。"""
    from datetime import datetime
    now = datetime.now().isoformat(timespec="seconds")
    new = 0
    with connect(path) as con:
        for s in scripts:
            cur = con.execute("SELECT 1 FROM scripts WHERE sid=?", (s.id,)).fetchone()
            if cur is None:
                new += 1
            con.execute(
                "INSERT INTO scripts(sid,slug,title,author,agree,kind,access,lines,"
                "file,first_seen,last_seen) VALUES(?,?,?,?,?,?,?,?,?,?,?) "
                "ON CONFLICT(sid) DO UPDATE SET agree=excluded.agree, lines=excluded.lines,"
                "file=excluded.file, access=excluded.access, last_seen=excluded.last_seen",
                (s.id, s.slug, s.title, s.author, s.agree, s.kind, s.access,
                 s.lines, s.file, now, now))
        total = con.execute("SELECT COUNT(*) c FROM scripts").fetchone()["c"]
    return int(total), new


def script_stats(path: Path | None = None) -> dict:
    with connect(path) as con:
        r = con.execute(
            "SELECT COUNT(*) total, SUM(access LIKE 'open%') open, "
            "SUM(kind='strategy') strat, SUM(kind='study') study, "
            "MAX(last_seen) seen FROM scripts").fetchone()
    return dict(r) if r else {}


def top_scripts(limit: int = 20, path: Path | None = None) -> list[dict]:
    with connect(path) as con:
        rows = con.execute(
            "SELECT title, author, agree, kind, lines, file FROM scripts "
            "WHERE access LIKE 'open%' ORDER BY agree DESC LIMIT ?",
            (limit,)).fetchall()
    return [dict(r) for r in rows]


_SCRIPT_SORTS = {
    "agree": "agree DESC, lines DESC",
    "lines": "lines DESC, agree DESC",
    "new": "first_seen DESC, agree DESC",
    "seen": "last_seen DESC, agree DESC",
    "title": "title COLLATE NOCASE ASC",
}


def list_scripts(kind: str = "", q: str = "", sort: str = "agree",
                 limit: int = 100, offset: int = 0,
                 path: Path | None = None) -> tuple[list[dict], int]:
    """采集到的脚本清单（分页）；返回 (本页数据, 总数)。"""
    where, args = ["access LIKE 'open%'"], []
    if kind:
        where.append("kind = ?")
        args.append(kind)
    if q:
        where.append("(title LIKE ? OR author LIKE ? OR sid LIKE ?)")
        args += [f"%{q}%"] * 3
    clause = " AND ".join(where)
    order = _SCRIPT_SORTS.get(sort, _SCRIPT_SORTS["agree"])
    with connect(path) as con:
        total = con.execute(f"SELECT COUNT(*) c FROM scripts WHERE {clause}",
                            args).fetchone()["c"]
        rows = con.execute(
            f"SELECT sid, slug, title, author, agree, kind, lines, file, "
            f"first_seen, last_seen FROM scripts WHERE {clause} "
            f"ORDER BY {order} LIMIT ? OFFSET ?", [*args, limit, offset]).fetchall()
    return [dict(r) for r in rows], int(total)


def get_script(sid: str, path: Path | None = None) -> dict | None:
    """按完整 sid（PUB;xxx）或仅哈希部分查询。"""
    with connect(path) as con:
        r = con.execute("SELECT * FROM scripts WHERE sid=? OR sid LIKE ?",
                        (sid, f"%;{sid}")).fetchone()
    return dict(r) if r else None


def script_kinds(path: Path | None = None) -> list[str]:
    with connect(path) as con:
        rows = con.execute(
            "SELECT kind, COUNT(*) n FROM scripts WHERE access LIKE 'open%' "
            "AND kind <> '' GROUP BY kind ORDER BY n DESC").fetchall()
    return [r["kind"] for r in rows]


def script_authors(limit: int = 20, path: Path | None = None) -> list[dict]:
    with connect(path) as con:
        rows = con.execute(
            "SELECT author, COUNT(*) n, SUM(agree) likes FROM scripts "
            "WHERE access LIKE 'open%' AND author <> '' GROUP BY author "
            "ORDER BY n DESC LIMIT ?", (limit,)).fetchall()
    return [dict(r) for r in rows]


# ---------- results ----------
def add_result(row: dict, path: Path | None = None) -> None:
    vals = [row.get(c) for c in RESULT_COLS]
    cols = ",".join(f'"{c}"' for c in RESULT_COLS)
    ph = ",".join("?" * len(RESULT_COLS))
    with connect(path) as con:
        con.execute(f"INSERT INTO results({cols}) VALUES({ph})", vals)


def latest_results(limit: int = 400, symbol: str = "", strategy: str = "",
                   freq: str = "", market: str = "", path: Path | None = None
                   ) -> list[dict]:
    """每个 (品种,策略,周期) 只取最新一条。"""
    where, args = ["1=1"], []
    for col, val in (("symbol", symbol), ("strategy", strategy),
                     ("freq", freq), ("market", market)):
        if val:
            where.append(f"{col}=?")
            args.append(val)
    sql = f"""
      SELECT r.* FROM results r
      JOIN (SELECT symbol, strategy, freq, MAX(id) mid FROM results
            WHERE {' AND '.join(where)} GROUP BY symbol, strategy, freq) t
        ON r.id = t.mid
      ORDER BY r.pf DESC LIMIT ?"""
    with connect(path) as con:
        rows = con.execute(sql, [*args, limit]).fetchall()
    return [dict(r) for r in rows]


def symbols_in_results(path: Path | None = None) -> list[dict]:
    with connect(path) as con:
        rows = con.execute(
            "SELECT symbol, name, market, COUNT(*) n, MAX(ts) last_ts FROM results "
            "GROUP BY symbol ORDER BY market, symbol").fetchall()
    return [dict(r) for r in rows]


def history(symbol: str, strategy: str, freq: str, limit: int = 60,
            path: Path | None = None) -> list[dict]:
    with connect(path) as con:
        rows = con.execute(
            "SELECT ts, trades, pf, avg_points, total_pnl, max_dd, pos_years, years "
            "FROM results WHERE symbol=? AND strategy=? AND freq=? "
            "ORDER BY id DESC LIMIT ?", (symbol, strategy, freq, limit)).fetchall()
    return [dict(r) for r in rows]


def last_ok(symbol: str, strategy: str, freq: str,
            path: Path | None = None) -> dict | None:
    with connect(path) as con:
        r = con.execute(
            "SELECT ts, status FROM results WHERE symbol=? AND strategy=? AND freq=? "
            "ORDER BY id DESC LIMIT 1", (symbol, strategy, freq)).fetchone()
    return dict(r) if r else None


def consecutive_failures(symbol: str, strategy: str, freq: str,
                         path: Path | None = None) -> int:
    """最近连续失败次数（用于给坏组合设重试上限）。"""
    with connect(path) as con:
        rows = con.execute(
            "SELECT status FROM results WHERE symbol=? AND strategy=? AND freq=? "
            "ORDER BY id DESC LIMIT 10", (symbol, strategy, freq)).fetchall()
    n = 0
    for r in rows:
        if r["status"] == "error":
            n += 1
        else:
            break
    return n


# ---------- 调度状态 ----------
def set_state(key: str, value, path: Path | None = None) -> None:
    from datetime import datetime
    with connect(path) as con:
        con.execute("INSERT INTO state(k,v,ts) VALUES(?,?,?) ON CONFLICT(k) DO "
                    "UPDATE SET v=excluded.v, ts=excluded.ts",
                    (key, json.dumps(value, ensure_ascii=False),
                     datetime.now().isoformat(timespec="seconds")))


def get_state(key: str, default=None, path: Path | None = None):
    with connect(path) as con:
        r = con.execute("SELECT v, ts FROM state WHERE k=?", (key,)).fetchone()
    if not r:
        return default
    try:
        return {"value": json.loads(r["v"]), "ts": r["ts"]}
    except Exception:
        return default


# ==================== 全市场任务队列 ====================
def sync_instruments(rows: list[dict], path: Path | None = None) -> int:
    """写入/更新品种表。rows: [{symbol,name,market,freqs}]"""
    from datetime import datetime
    now = datetime.now().isoformat(timespec="seconds")
    with connect(path) as con:
        for r in rows:
            con.execute(
                "INSERT INTO instruments(symbol,name,market,freqs,active,updated_at) "
                "VALUES(?,?,?,?,1,?) ON CONFLICT(symbol) DO UPDATE SET "
                "name=excluded.name, market=excluded.market, freqs=excluded.freqs, "
                "active=1, updated_at=excluded.updated_at",
                (r["symbol"], r.get("name", ""), r.get("market", ""),
                 ",".join(r.get("freqs", [])), now))
        n = con.execute("SELECT COUNT(*) c FROM instruments WHERE active=1").fetchone()["c"]
    return int(n)


def list_instruments(market: str = "", path: Path | None = None) -> list[dict]:
    with connect(path) as con:
        if market:
            rows = con.execute("SELECT * FROM instruments WHERE active=1 AND market=? "
                               "ORDER BY symbol", (market,)).fetchall()
        else:
            rows = con.execute("SELECT * FROM instruments WHERE active=1 "
                               "ORDER BY market, symbol").fetchall()
    return [dict(r) for r in rows]


def sync_tasks(strategy_freqs: dict, path: Path | None = None) -> tuple[int, int]:
    """按 品种 × 策略 × 可用周期 补齐任务队列，返回 (新增, 总数)。

    strategy_freqs: {策略key: 允许的周期元组}，空元组表示不限。
    日内形态（orb/vreversal）配日线必然 0 笔，不能进队列浪费算力。
    """
    with connect(path) as con:
        ins = con.execute("SELECT symbol, freqs FROM instruments WHERE active=1").fetchall()
        pairs = []
        for r in ins:
            for fq in (r["freqs"] or "").split(","):
                if not fq:
                    continue
                for st, allow in strategy_freqs.items():
                    if allow and fq not in allow:
                        continue
                    pairs.append((r["symbol"], st, fq))
        before = con.execute("SELECT COUNT(*) c FROM tasks").fetchone()["c"]
        con.executemany(
            "INSERT OR IGNORE INTO tasks(symbol,strategy,freq,state,next_due) "
            "VALUES(?,?,?,'pending',datetime('now'))", pairs)
        after = con.execute("SELECT COUNT(*) c FROM tasks").fetchone()["c"]
    return int(after - before), int(after)


def claim_tasks(limit: int = 1, symbol: str = "",
                path: Path | None = None) -> list[dict]:
    """取一批到期的任务（串行 worker 用 limit=1 即可），带上品种信息。

    symbol 非空时只取该品种 —— worker 按品种成批处理，这样行情数据
    只下载一次、用完即删，不会因为任务交错而反复下载同一份数据。
    """
    with connect(path) as con:
        sql = ("SELECT t.*, i.name name, i.market market FROM tasks t "
               "LEFT JOIN instruments i ON i.symbol=t.symbol "
               "WHERE (t.next_due IS NULL OR t.next_due <= datetime('now')) ")
        args: list = []
        if symbol:
            sql += "AND t.symbol=? "
            args.append(symbol)
        sql += "ORDER BY COALESCE(t.next_due,'') , t.id LIMIT ?"
        rows = con.execute(sql, [*args, limit]).fetchall()
    return [dict(r) for r in rows]


def next_due_symbols(limit: int = 20, path: Path | None = None) -> list[str]:
    """有到期任务的品种列表，按最早到期时间排序（worker 按此顺序逐品种处理）。"""
    with connect(path) as con:
        rows = con.execute(
            "SELECT symbol, MIN(COALESCE(next_due,'')) d FROM tasks "
            "WHERE next_due IS NULL OR next_due <= datetime('now') "
            "GROUP BY symbol ORDER BY d, symbol LIMIT ?", (limit,)).fetchall()
    return [r["symbol"] for r in rows]


def finish_task(task_id: int, status: str, requeue_days: int = 7,
                fail_backoff_hours: int = 6, err: str = "",
                path: Path | None = None) -> None:
    """标记任务结果并按结果安排下次执行时间。"""
    if status == "ok":
        due = f"datetime('now','+{int(requeue_days)} days')"
    else:
        due = f"datetime('now','+{int(fail_backoff_hours)} hours')"
    with connect(path) as con:
        con.execute(
            f"UPDATE tasks SET state=?, next_due={due}, "
            "attempts=attempts+1, last_status=?, last_ts=datetime('now'), "
            "last_error=? WHERE id=?",
            (status, status, (err or "")[:300], task_id))


def task_stats(path: Path | None = None) -> dict:
    with connect(path) as con:
        r = con.execute(
            "SELECT COUNT(*) total, "
            "SUM(state='ok') ok, SUM(state='error') err, "
            "SUM(next_due IS NULL OR next_due<=datetime('now')) due "
            "FROM tasks").fetchone()
        by_market = con.execute(
            "SELECT i.market market, COUNT(*) n FROM tasks t "
            "JOIN instruments i ON i.symbol=t.symbol GROUP BY i.market").fetchall()
        by_freq = con.execute(
            "SELECT freq, COUNT(*) n FROM tasks GROUP BY freq ORDER BY n DESC").fetchall()
    return {"total": int(r["total"] or 0), "ok": int(r["ok"] or 0),
            "err": int(r["err"] or 0), "due": int(r["due"] or 0),
            "by_market": {x["market"]: x["n"] for x in by_market},
            "by_freq": {x["freq"]: x["n"] for x in by_freq}}
