"""无人值守调度：定时采集 TradingView 策略 + 回测全品种，结果入库。

一次循环（run_cycle）做两件事：
  1) 增量采集：把语料库里还没下过的开源脚本补齐（已存在的不重复请求）
  2) 扫描回测：对 universe.json 里的 (品种 × 周期 × 策略) 逐一回测并写入 SQLite

两个关键设计：
- **增量**：某个组合在 max_age_days 内已有成功记录就跳过，避免每轮全量重跑；
- **轮转**：按「上次成功时间」从旧到新排序，一轮跑不完（--limit）也不会饿死后面的组合。

防重入：用文件锁（flock）。systemd timer 若上一轮没跑完，新一轮会直接退出。

用法：
    python -m alpharadar.scheduler --once            # 跑一轮
    python -m alpharadar.scheduler --once --force    # 忽略新鲜度，全部重跑
    python -m alpharadar.scheduler --once --limit 20 # 本轮最多 20 个组合
    python -m alpharadar.scheduler --loop --interval 21600   # 常驻模式（默认用 timer）
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import traceback
from datetime import datetime, timedelta
from pathlib import Path

from . import config, store
from .judge import result_extras
from .metrics import summarize
from .pipeline import run_one, save_result
from .tushare_client import TushareClient

UNIVERSE = config.ROOT / "config" / "universe.json"
LOCK = config.DATA_DIR / "scheduler.lock"


def _flock_nb(fh) -> bool:
    """非阻塞文件锁；Windows 用 msvcrt，POSIX 用 fcntl。"""
    try:
        import fcntl
        fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return True
    except ImportError:
        import msvcrt
        try:
            msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
            return True
        except OSError:
            return False
    except OSError:
        return False


def load_universe(path: Path | None = None) -> dict:
    f = Path(path or UNIVERSE)
    cfg = json.loads(f.read_text(encoding="utf-8"))
    cfg.setdefault("start", "20220101")
    cfg.setdefault("max_age_days", 7)
    cfg.setdefault("harvest_max_fetch", 300)
    return cfg


def build_cells(cfg: dict) -> list[dict]:
    """展开成待跑清单：每个元素是 (品种, 市场, 周期, 策略)。

    跳过「策略不支持的周期」—— 日内形态（开盘区间突破、冰点反转）配日线必然
    0 笔，跑它只会浪费配额、把无效行混进榜单。
    """
    from .strategies import get as get_strategy
    cells = []
    for market in ("futures", "stocks"):
        block = cfg.get(market) or {}
        for sym in block.get("symbols", []):
            for freq in block.get("freqs", ["1d"]):
                for strat in cfg.get("strategies", []):
                    try:
                        allow = get_strategy(strat).freqs
                    except KeyError:
                        allow = ()
                    if allow and freq not in allow:
                        continue
                    cells.append({"symbol": sym, "market": market,
                                  "freq": freq, "strategy": strat})
    return cells


def _stale(cells: list[dict], max_age_days: int, force: bool,
           max_fails: int = 5) -> list[dict]:
    """排序待跑清单。

    - 新鲜（max_age_days 内成功过）的放最后，不参与本轮；
    - 失败记录**不算新鲜**：时间戳再新也要重跑（否则一次网络抖动会让某个组合
      永远不再被尝试 —— 这是踩过的坑）；
    - 连续失败超过 max_fails 的组合跳过，避免坏品种每轮空跑消耗配额。
    """
    now = datetime.now()
    fresh, stale, dead = [], [], []
    for c in cells:
        last = store.last_ok(c["symbol"], c["strategy"], c["freq"])
        age = None
        if last and last.get("ts"):
            try:
                age = (now - datetime.fromisoformat(last["ts"])).days
            except ValueError:
                age = None
        ok = bool(last and last.get("status") == "ok")
        # 没跑过或上次失败 -> 视为最旧，优先跑
        c["_age"] = (10 ** 6) if (age is None or not ok) else age
        c["_last"] = (last or {}).get("ts", "—")
        c["_fails"] = store.consecutive_failures(c["symbol"], c["strategy"], c["freq"])
        if not force and c["_fails"] >= max_fails:
            c["_why"] = f"连续失败 {c['_fails']} 次"
            dead.append(c)
        elif not force and ok and age is not None and age < max_age_days:
            fresh.append(c)
        else:
            stale.append(c)
    stale.sort(key=lambda x: -x["_age"])            # 越旧越先跑
    if dead:
        out = ", ".join(f"{d['symbol']}/{d['strategy']}/{d['freq']}" for d in dead[:5])
        print(f"[skip] 连续失败已停跑 {len(dead)} 个组合：{out}"
              f"{' …' if len(dead) > 5 else ''}（--force 可强制重跑）")
    return stale + fresh


def do_harvest(cfg: dict, run_id: int, out=print) -> dict:
    """增量采集 TradingView 开源脚本并入语料库。"""
    from .harvest import harvest
    stats: dict = {}
    scripts = harvest(max_fetch=int(cfg.get("harvest_max_fetch", 300)),
                      feed_pages=int(cfg.get("harvest_feed_pages", 50)),
                      forum_pages=int(cfg.get("harvest_forum_pages", 50)),
                      progress=lambda *a: None,      # 静默，避免日志刷屏
                      stats=stats)
    total, new = store.upsert_scripts(scripts)
    st = store.script_stats()
    store.set_state("harvest", {"total": total, "new": new,
                                "open": int(st.get("open") or 0),
                                "strategy": int(st.get("strat") or 0),
                                "channels": {k: stats.get(k, 0)
                                             for k in ("search", "feed", "forum",
                                                       "forum_snippets", "downloaded")}})
    out(f"[harvest] 语料库 {total} 个（开源 {st.get('open')}），本次新增 {new}；"
        f"通道产出 搜索{stats.get('search', 0)} / 脚本流{stats.get('feed', 0)} / "
        f"论坛{stats.get('forum', 0)}（代码块 {stats.get('forum_snippets', 0)}），"
        f"本轮新下载 {stats.get('downloaded', 0)}")
    return {"total": total, "new": new, **stats}


def run_cycle(cfg: dict | None = None, force: bool = False, limit: int = 0,
              only_symbol: str = "", only_strategy: str = "",
              client: TushareClient | None = None, out=print,
              with_harvest: bool = True) -> dict:
    """跑一轮：采集 + 回测。返回统计。"""
    cfg = cfg or load_universe()
    LOCK.parent.mkdir(parents=True, exist_ok=True)
    with open(LOCK, "w") as lk:
        if not _flock_nb(lk):
            out("[skip] 已有一轮在跑，本轮退出")
            return {"skipped": True}
        return _cycle_inner(cfg, force, limit, only_symbol, only_strategy,
                            client, out, with_harvest)


def harvest_only(cfg: dict | None = None, out=print,
                 sync_instruments_too: bool = False) -> dict:
    """只补采语料库（与定时任务共用同一把锁）。

    sync_instruments_too=True 时顺带刷新全市场品种表与任务队列 —— 定时器用
    这个组合做「采集 + 品种同步」，回测交给常驻 worker，两边职责不重叠。
    """
    cfg = cfg or load_universe()
    LOCK.parent.mkdir(parents=True, exist_ok=True)
    with open(LOCK, "w") as lk:
        if not _flock_nb(lk):
            out("[skip] 已有一轮在跑，本次采集退出")
            return {"skipped": True}
        run_id = store.start_run("harvest", "manual")
        try:
            res = do_harvest(cfg, run_id, out)
            if sync_instruments_too:
                from .instruments import sync as sync_inst
                out("[sync] 刷新全市场品种与任务队列…")
                res.update(sync_inst(cfg=cfg, verbose=out))
            store.finish_run(run_id, "ok", n_ok=1, n_err=0)
            return res
        except Exception as exc:
            store.finish_run(run_id, "error", 0, 1,
                             note=f"{type(exc).__name__}: {exc}"[:300])
            raise


def _cycle_inner(cfg, force, limit, only_symbol, only_strategy, client, out,
                 with_harvest: bool = True) -> dict:
    t0 = time.time()
    run_id = store.start_run("cycle", "scheduler")
    n_ok = n_err = n_skip = 0
    try:
        if with_harvest:
            do_harvest(cfg, run_id, out)
        else:
            out("[harvest] 本轮跳过采集")
        cells = build_cells(cfg)
        if only_symbol:
            cells = [c for c in cells if c["symbol"] == only_symbol]
        if only_strategy:
            cells = [c for c in cells if c["strategy"] == only_strategy]
        ordered = _stale(cells, int(cfg.get("max_age_days", 7)), force,
                         int(cfg.get("max_fails", 5)))
        todo = [c for c in ordered
                if force or (c["_age"] >= int(cfg.get("max_age_days", 7))
                             and c["_fails"] < int(cfg.get("max_fails", 5)))]
        if limit:
            todo = todo[:limit]
        out(f"[scan] 组合 {len(cells)} 个，其中需要重跑 {len(todo)} 个"
            f"（新鲜跳过 {len(cells) - len(todo)}）")
        cli = client or TushareClient()
        for i, c in enumerate(todo, 1):
            sym, strat, freq = c["symbol"], c["strategy"], c["freq"]
            row = {"run_id": run_id, "ts": datetime.now().isoformat(timespec="seconds"),
                   "symbol": sym, "market": c["market"], "strategy": strat,
                   "freq": freq, "start": cfg["start"],
                   "end": datetime.now().strftime("%Y%m%d"), "status": "ok",
                   "name": "", "trades": 0, "win_rate": None, "pf": None,
                   "avg_points": None, "total_pnl": 0.0, "max_dd": 0.0,
                   "ret_dd": None, "pos_years": 0, "years": 0,
                   "error": "", "report": ""}
            try:
                res = run_one(sym, strat, freq, cfg["start"], client=cli,
                              verbose=False)
                s = summarize(res)
                _, html = save_result(res, tag="sched")
                row.update(name=s["name"], trades=s["笔数"],
                           win_rate=None if s["笔数"] == 0 else round(s["胜率"], 4),
                           pf=None if s["笔数"] == 0 else round(float(s["PF"]), 3),
                           avg_points=None if s["笔数"] == 0 else round(s["均点"], 3),
                           total_pnl=round(s["合计元"], 0),
                           max_dd=round(s["最大回撤"], 0),
                           ret_dd=None if s["笔数"] == 0 else round(s["收益回撤比"], 2),
                           pos_years=s["正年数"], years=s["年数"],
                           report=html.name)
                row.update(result_extras(res))
                n_ok += 1
                out(f"  [{i}/{len(todo)}] {sym} {strat} {freq} → "
                    f"{s['笔数']} 笔 PF {s['PF']:.2f} 均点 {s['均点']:+.2f}")
            except Exception as exc:
                row.update(status="error", error=f"{type(exc).__name__}: {exc}"[:300])
                n_err += 1
                out(f"  [{i}/{len(todo)}] {sym} {strat} {freq} → 失败：{exc}")
            store.add_result(row)
        store.set_state("last_cycle", {
            "finished": datetime.now().isoformat(timespec="seconds"),
            "ok": n_ok, "err": n_err, "todo": len(todo), "cells": len(cells),
            "seconds": round(time.time() - t0)})
        store.finish_run(run_id, "ok" if n_err == 0 else "partial",
                         n_ok, n_err, len(cells) - len(todo))
        out(f"[done] 成功 {n_ok} 失败 {n_err}，用时 {time.time() - t0:.0f}s")
        return {"ok": n_ok, "err": n_err, "todo": len(todo)}
    except Exception as exc:
        store.finish_run(run_id, "error", n_ok, n_err, 0,
                         note=f"{type(exc).__name__}: {exc}"[:300])
        out(f"[error] {exc}\n{traceback.format_exc()[-600:]}")
        raise


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="alpha-radar 定时扫描")
    ap.add_argument("--once", action="store_true", help="跑一轮后退出（供 systemd timer）")
    ap.add_argument("--loop", action="store_true", help="常驻循环")
    ap.add_argument("--interval", type=int, default=21600, help="常驻模式间隔秒数")
    ap.add_argument("--force", action="store_true", help="忽略新鲜度，全部重跑")
    ap.add_argument("--limit", type=int, default=0, help="本轮最多跑多少个组合")
    ap.add_argument("--symbol", default="")
    ap.add_argument("--strategy", default="")
    ap.add_argument("--universe", default=str(UNIVERSE))
    ap.add_argument("--no-harvest", action="store_true", help="只回测，不采集")
    ap.add_argument("--harvest-only", action="store_true",
                    help="只采集语料库，不回测（运维/手动补采）")
    ap.add_argument("--sync-instruments", action="store_true",
                    help="采集后刷新全市场品种表与任务队列")
    args = ap.parse_args(argv)

    store.init()
    cfg = load_universe(Path(args.universe))
    if args.harvest_only:
        harvest_only(cfg, sync_instruments_too=args.sync_instruments)
        return 0
    if args.loop:
        while True:
            try:
                run_cycle(cfg, args.force, args.limit, args.symbol, args.strategy,
                          with_harvest=not args.no_harvest)
            except Exception:
                pass
            time.sleep(args.interval)
    run_cycle(cfg, args.force, args.limit, args.symbol, args.strategy,
              with_harvest=not args.no_harvest)
    return 0


if __name__ == "__main__":
    sys.exit(main())
