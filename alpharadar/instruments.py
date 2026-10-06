"""全市场品种同步：从 Tushare 拉取 A 股与期货品种，写入 instruments 表。

规模实测（2026-10-06）：
  A 股   5572 只（主板 3197 / 创业板 1409 / 科创板 618 / 北交所 348）
  期货   103 个活跃品种（六个交易所，按未退市合约推导品种代码）

期货品种推导：fut_basic 给的是具体合约（RB2601.SHF），把月份数字去掉得到
品种连续代码（RB.SHF）；只保留 delist_date 未到的合约，避免收录已退市品种。
"""

from __future__ import annotations

import re

import pandas as pd

from . import store
from .tushare_client import TushareClient

EXCHANGES = ("SHFE", "DCE", "CZCE", "CFFEX", "INE", "GFEX")
MIN_FREQS = ("1min", "5min", "15min", "30min", "60min")
ALL_FREQS = MIN_FREQS + ("1d",)


def _product(code: str) -> str:
    """RB2601.SHF -> RB.SHF"""
    head, _, suffix = code.partition(".")
    return f"{re.sub(r'[0-9]+$', '', head)}.{suffix}"


def sync_stocks(client: TushareClient, freqs: tuple[str, ...] = ("1d",),
                exclude_markets: tuple[str, ...] = (), limit: int = 0,
                verbose=print) -> list[dict]:
    """A 股全市场。limit>0 时按代码顺序截断（用于小规模试跑）。"""
    df = client.call("stock_basic", exchange="", list_status="L",
                     fields="ts_code,name,market,list_date")
    if df is None or df.empty:
        raise RuntimeError("stock_basic 返回空，检查权限")
    if exclude_markets:
        df = df[~df["market"].isin(exclude_markets)]
    df = df.sort_values("ts_code")
    if limit:
        df = df.head(limit)
    rows = [{"symbol": r.ts_code, "name": r.name, "market": "stocks",
             "freqs": list(freqs)} for r in df.itertuples(index=False)]
    verbose(f"  A股 {len(rows)} 只（周期 {','.join(freqs)}）")
    return rows


def sync_futures(client: TushareClient,
                 freqs: tuple[str, ...] = ALL_FREQS,
                 exchanges: tuple[str, ...] = EXCHANGES,
                 verbose=print) -> list[dict]:
    """期货活跃品种：按未退市合约推导品种代码。"""
    today = pd.Timestamp.now().strftime("%Y%m%d")
    rows: dict[str, dict] = {}
    for ex in exchanges:
        try:
            f = client.call("fut_basic", exchange=ex, fut_type="1",
                            fields="ts_code,name,delist_date")
        except Exception as exc:
            verbose(f"  {ex} 取数失败：{type(exc).__name__}")
            continue
        if f is None or f.empty:
            continue
        act = f[f["delist_date"].astype(str) >= today]
        for r in act.itertuples(index=False):
            p = _product(r.ts_code)
            # 品种中文名去月份，例如「螺纹钢2610」-> 螺纹钢
            nm = re.sub(r"[0-9]+$", "", str(getattr(r, "name", "")) or "") or p
            rows.setdefault(p, {"symbol": p, "name": nm, "market": "futures",
                                "freqs": list(freqs)})
    out = [rows[k] for k in sorted(rows)]
    verbose(f"  期货 {len(out)} 个活跃品种（周期 {','.join(freqs)}）")
    return out


def rank_stocks_by_liquidity(client: TushareClient, top_n: int = 300,
                             lookback_days: int = 20,
                             verbose=print) -> list[str]:
    """按成交额取流动性最好的 N 只 A 股。

    为什么需要它：全市场 1 分钟数据约 78GB，而这台机只剩 21GB —— 全A股分钟线
    在磁盘与 API 配额上都不成立。分钟级回测按流动性取子集，日线才跑全市场。
    实现：用 daily(trade_date=...) 批量取最近若干交易日，按成交额中位数排名。
    """
    import pandas as pd
    cal = client.call("trade_cal", exchange="SSE", is_open="1",
                      start_date=(pd.Timestamp.now() - pd.Timedelta(days=60)).strftime("%Y%m%d"),
                      end_date=pd.Timestamp.now().strftime("%Y%m%d"))
    if cal is None or cal.empty:
        raise RuntimeError("取交易日历失败")
    days = sorted(cal["cal_date"].astype(str))[-lookback_days:]
    acc: dict[str, list[float]] = {}
    for d in days:
        try:
            df = client.call("daily", trade_date=d, fields="ts_code,amount")
        except Exception:
            continue
        if df is None or df.empty:
            continue
        for r in df.itertuples(index=False):
            acc.setdefault(r.ts_code, []).append(float(r.amount or 0))
    if not acc:
        raise RuntimeError("未能取到成交额，无法排序")
    med = {k: sorted(v)[len(v) // 2] for k, v in acc.items()}
    top = sorted(med, key=lambda k: -med[k])[:top_n]
    verbose(f"  按 {len(days)} 个交易日的成交额中位数，取前 {len(top)} 只")
    return top


def sync(client: TushareClient | None = None, cfg: dict | None = None,
         verbose=print) -> dict:
    """按配置同步全市场品种并补齐任务队列。"""
    cfg = cfg or {}
    auto = cfg.get("auto") or {}
    cli = client or TushareClient()
    rows = []
    if (auto.get("stocks") or {}).get("enabled", True):
        s = auto.get("stocks") or {}
        freqs = tuple(s.get("freqs", ["1d"]))
        minute = [f for f in freqs if f != "1d"]
        if minute and int(s.get("minute_top_n", 0)) > 0:
            # 分钟级只覆盖流动性前 N 只：全市场分钟数据磁盘放不下
            top = rank_stocks_by_liquidity(cli, int(s["minute_top_n"]),
                                           int(s.get("lookback_days", 20)), verbose)
            daily_rows = sync_stocks(cli, ("1d",),
                                     tuple(s.get("exclude_markets", [])),
                                     int(s.get("limit", 0)), verbose)
            keep = set(top)
            # 全市场跑日线；只有流动性前 N 只额外挂上分钟级周期
            for r in daily_rows:
                r["freqs"] = list(freqs) if r["symbol"] in keep else ["1d"]
            rows += daily_rows
            verbose(f"  其中 {len(keep & {r['symbol'] for r in daily_rows})} "
                    f"只带分钟级周期")
        else:
            rows += sync_stocks(cli, freqs,
                                tuple(s.get("exclude_markets", [])),
                                int(s.get("limit", 0)), verbose)
    if (auto.get("futures") or {}).get("enabled", True):
        f = auto.get("futures") or {}
        rows += sync_futures(cli, tuple(f.get("freqs", list(ALL_FREQS))),
                             tuple(f.get("exchanges", list(EXCHANGES))),
                             verbose)
    store.init()
    n = store.sync_instruments(rows)
    # 策略清单：config.auto.strategies 留空 -> 自动纳入**全部已注册策略**。
    # 这样新移植一个策略、跑一次 --sync 就会自动铺满全市场，不需要手工改配置。
    from .strategies import get as get_strategy
    from .strategies import list_strategies
    # 注意不能用 `or` 兜底：显式写 auto.strategies=[] 表示「全部」，
    # 而 `or` 会把空列表当假值、回退到顶层那份旧清单（踩过）。
    auto_keys = auto.get("strategies")
    if auto_keys is None:
        auto_keys = cfg.get("strategies") or []
    keys = list(auto_keys)
    if not keys:
        keys = [s.key for s in list_strategies()]
        verbose(f"  策略清单未指定，自动纳入全部 {len(keys)} 个已注册策略")
    sf: dict[str, tuple] = {}
    for key in keys:
        try:
            sf[key] = get_strategy(key).freqs
        except KeyError:
            continue
    added, total = store.sync_tasks(sf)
    stats = store.task_stats()
    store.set_state("instruments", {"instruments": n, "tasks": total,
                                    "tasks_new": added, **stats})
    verbose(f"  品种表 {n} 个，任务队列 {total} 个（新增 {added}）")
    return {"instruments": n, "tasks": total, "tasks_new": added, **stats}
