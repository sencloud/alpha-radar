"""常驻 worker：按任务队列串行回测全市场。

为什么不用 systemd timer：全市场是 20 万量级的回测单元，一轮跑不完，
需要"接着上次继续"的持久队列，而不是每次从头扫一遍清单。
worker 是 Type=simple 常驻服务，循环：取到期任务 -> 回测 -> 写结果 -> 排下次时间。

调度策略：
- 成功：next_due = 现在 + requeue_days（默认 7 天）
- 失败：next_due = 现在 + fail_backoff_hours（默认 6 小时），下次自动重试
- 任务表为空或全部未到期时睡眠轮询（默认 60 秒）
- 磁盘低于阈值时暂停，避免把机器写满（同机还有两个邻居服务）
"""

from __future__ import annotations

import argparse
import shutil
import sys
import time
from datetime import datetime

from . import config
from . import store
from .instruments import sync as sync_instruments
from .metrics import summarize
from .pipeline import run_one, save_result
from .tushare_client import TushareClient


def _free_gb(path: str = "/") -> float:
    try:
        return shutil.disk_usage(path).free / 1e9
    except OSError:
        return 999.0


def _start_for(cfg: dict, market: str, freq: str) -> str:
    blk = (cfg.get("auto") or {}).get("stocks" if market == "stocks" else "futures") or {}
    if freq == "1d":
        return blk.get("start_daily", cfg.get("start", "20220101"))
    return blk.get("start_minute", cfg.get("start", "20220101"))


def run_worker(cfg: dict, minutes: float = 60.0, limit: int = 0,
               poll_seconds: int = 60, min_free_gb: float = 2.0,
               requeue_days: int = 7, client: TushareClient | None = None,
               out=print, log_every: int = 5, cache_max_gb: float = 0.0,
               keep_cache: bool = False) -> dict:
    """串行跑到期任务。minutes=0 表示不限时（常驻）。limit>0 表示本轮最多跑几个。

    磁盘策略：**按品种成批处理，处理完立即删掉该品种的行情缓存**（保留很小的
    主力映射表）。同一个品种有 6~11 个任务共用一份行情，成批处理只下载一次。
    cache_max_gb>0 时再加一道全局兜底：缓存超限就按 LRU 删到限额以下。
    """
    store.init()
    cli = client or TushareClient()
    t0 = time.time()
    n_ok = n_err = 0
    cur_symbol = None
    evicted_files = 0
    evicted_mb = 0.0
    run_id = store.start_run("worker", f"budget={minutes}min")
    cfg_cache = (cfg.get("cache") or {}).get("max_gb", 0)
    cache_max_gb = cache_max_gb or float(cfg_cache or 0)
    out(f"[worker] 启动：时间预算 {minutes or '∞'} 分钟，串行执行；"
        f"缓存策略 {'保留' if keep_cache else '按品种回收'}，"
        f"全局上限 {cache_max_gb or '不限'}GB")
    try:
        while True:
            if minutes and (time.time() - t0) / 60.0 >= minutes:
                out("[worker] 时间预算用完，本轮结束")
                break
            if limit and (n_ok + n_err) >= limit:
                out(f"[worker] 达到本轮上限 {limit}，结束")
                break
            if _free_gb() < min_free_gb:
                out(f"[worker] 磁盘可用 {_free_gb():.1f}G < {min_free_gb}G，暂停 10 分钟")
                time.sleep(600)
                continue

            # 先把手上的品种做完，再换下一个（保证一份行情只下一次）
            if cur_symbol:
                tasks = store.claim_tasks(1, symbol=cur_symbol)
                if tasks:
                    if not keep_cache and cache_max_gb and cli.cache_gb() > cache_max_gb:
                        n, mb = cli.evict_lru(cache_max_gb * 0.8)
                        evicted_files += n
                        evicted_mb += mb
                        if n:
                            out(f"  [cache] 缓存超 {cache_max_gb}GB，"
                                f"LRU 回收 {n} 个文件 {mb:.1f}MB")
                else:
                    if not keep_cache:
                        n, mb = cli.evict_written()
                        evicted_files += n
                        evicted_mb += mb
                        if n:
                            out(f"  [cache] {cur_symbol} 完成，回收 {n} 个文件 "
                                f"{mb:.1f}MB")
                    cur_symbol = None
                    continue
            else:
                syms = store.next_due_symbols(5)
                if not syms:
                    st = store.task_stats()
                    out(f"[worker] 暂无到期任务（共 {st['total']} 个，"
                        f"已完成 {st['ok']}）；等待 {poll_seconds}s")
                    time.sleep(poll_seconds)
                    continue
                cur_symbol = syms[0]
                tasks = store.claim_tasks(1, symbol=cur_symbol)
            if not tasks:
                st = store.task_stats()
                out(f"[worker] 暂无到期任务（共 {st['total']} 个，已完成 {st['ok']}）；"
                    f"等待 {poll_seconds}s")
                time.sleep(poll_seconds)
                continue

            t = tasks[0]
            sym, strat, freq = t["symbol"], t["strategy"], t["freq"]
            market = t.get("market") or "futures"
            start = _start_for(cfg, market, freq)
            row = {"run_id": run_id, "ts": datetime.now().isoformat(timespec="seconds"),
                   "symbol": sym, "market": market, "strategy": strat, "freq": freq,
                   "start": start, "end": datetime.now().strftime("%Y%m%d"),
                   "status": "ok", "name": t.get("name") or "", "trades": 0,
                   "win_rate": None, "pf": None, "avg_points": None,
                   "total_pnl": 0.0, "max_dd": 0.0, "ret_dd": None,
                   "pos_years": 0, "years": 0, "error": "", "report": ""}
            try:
                res = run_one(sym, strat, freq, start, client=cli, verbose=False)
                s = summarize(res)
                n = s["笔数"]
                _, html = save_result(res, tag="worker")
                row.update(trades=n,
                           win_rate=None if n == 0 else round(s["胜率"], 4),
                           pf=None if n == 0 else round(float(s["PF"]), 3),
                           avg_points=None if n == 0 else round(s["均点"], 3),
                           total_pnl=round(s["合计元"], 0),
                           max_dd=round(s["最大回撤"], 0),
                           ret_dd=None if n == 0 else round(s["收益回撤比"], 2),
                           pos_years=s["正年数"], years=s["年数"], report=html.name)
                store.add_result(row)
                store.finish_task(t["id"], "ok", requeue_days=requeue_days)
                n_ok += 1
                if n_ok % 25 == 0:            # 定期落状态，看板才能显示实时进度与 ETA
                    st_now = store.task_stats()
                    secs = max(1.0, time.time() - t0)
                    store.set_state("worker", {
                        "running": True,
                        "updated": datetime.now().isoformat(timespec="seconds"),
                        "ok": n_ok, "err": n_err, "seconds": round(secs),
                        "rate_per_min": round((n_ok + n_err) / secs * 60, 1), **st_now})
                if n_ok % log_every == 0 or n_ok <= 3:
                    out(f"  [{n_ok}ok/{n_err}err] {sym} {strat} {freq} -> "
                        f"{n} 笔 PF {0 if not n else (s['PF']):.2f} 均点 "
                        f"{0 if not n else (s['均点']):+.2f}")
            except Exception as exc:
                err = f"{type(exc).__name__}: {exc}"[:300]
                row.update(status="error", error=err)
                store.add_result(row)
                store.finish_task(t["id"], "error", err=err,
                                  fail_backoff_hours=6)
                n_err += 1
                out(f"  [{n_ok}ok/{n_err}err] {sym} {strat} {freq} -> 失败：{err[:90]}")

        st = store.task_stats()
        store.set_state("worker", {"finished": datetime.now().isoformat(timespec="seconds"),
                                   "running": False, "ok": n_ok, "err": n_err,
                                   "seconds": round(time.time() - t0),
                                   "cache_gb": round(cli.cache_gb(), 2),
                                   "evicted_files": evicted_files,
                                   "evicted_mb": round(evicted_mb, 1), **st})
        store.finish_run(run_id, "ok" if n_err == 0 else "partial", n_ok, n_err)
        out(f"[worker] 结束：成功 {n_ok} 失败 {n_err}，"
            f"队列 {st['total']}（完成 {st['ok']}，待跑 {st['due']}）")
        return {"ok": n_ok, "err": n_err, **st}
    except Exception as exc:
        store.finish_run(run_id, "error", n_ok, n_err,
                         note=f"{type(exc).__name__}: {exc}"[:300])
        raise


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="alpha-radar 全市场串行 worker")
    ap.add_argument("--minutes", type=float, default=60,
                    help="本轮时间预算（分钟）；0 = 不限时")
    ap.add_argument("--limit", type=int, default=0, help="本轮最多跑几个任务")
    ap.add_argument("--poll", type=int, default=60, help="无到期任务时的轮询间隔（秒）")
    ap.add_argument("--min-free-gb", type=float, default=2.0, help="磁盘下限，低于则暂停")
    ap.add_argument("--requeue-days", type=int, default=7, help="成功后多久重跑")
    ap.add_argument("--cache-max-gb", type=float, default=0.0,
                    help="缓存全局上限（GB），超过按 LRU 回收；0=用配置值")
    ap.add_argument("--keep-cache", action="store_true",
                    help="不做按品种回收（磁盘充足时用，可省掉重复下载）")
    ap.add_argument("--sync", action="store_true", help="先同步全市场品种与任务队列")
    ap.add_argument("--universe", default=str(config.ROOT / "config" / "universe.json"))
    args = ap.parse_args(argv)

    import json
    from pathlib import Path
    cfg = json.loads(Path(args.universe).read_text(encoding="utf-8"))
    cli = TushareClient()
    store.init()
    if args.sync:
        print("[sync] 同步全市场品种…")
        sync_instruments(cli, cfg, verbose=print)
    run_worker(cfg, minutes=args.minutes, limit=args.limit,
               poll_seconds=args.poll, min_free_gb=args.min_free_gb,
               requeue_days=args.requeue_days, client=cli,
               cache_max_gb=args.cache_max_gb, keep_cache=args.keep_cache)
    return 0


if __name__ == "__main__":
    sys.exit(main())
