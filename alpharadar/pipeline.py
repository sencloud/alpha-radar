"""端到端编排：取数 → 生成信号 → 回测 → 汇总。供 CLI 与 agent 调用。

两个主入口：
  run_one(...)      单品种 × 单策略
  run_matrix(...)   多品种 × 多策略 × 多周期 的排行榜（策略筛选的主工具）
"""

from __future__ import annotations

from dataclasses import dataclass
from itertools import product
from pathlib import Path

import pandas as pd

from . import config
from .engine import BacktestResult, run_strategy
from .indicators import add_indicators, add_regime
from .loaders import load_bars
from .metrics import summarize
from .strategies import get as get_strategy
from .strategies import list_strategies
from .tushare_client import TushareClient
from .universe import Instrument, resolve


def build_bars(symbol: str, freq: str, start: str, end: str,
               params: dict, client: TushareClient | None = None,
               instrument: Instrument | None = None,
               verbose: bool = True) -> tuple[pd.DataFrame, Instrument]:
    inst = instrument or resolve(symbol)
    bars = load_bars(symbol, freq, start, end, client=client, verbose=verbose,
                     instrument=inst)
    bars = add_indicators(bars, params)
    if params.get("use_regime"):
        bars = add_regime(bars, params)
    # 指标吃预热，但回测只从 start 开始（预热期不下单）
    bars = bars[bars["sdate"].astype(str) >= start].reset_index(drop=True)
    return bars, inst


def make_params(strategy_key: str, freq: str, overrides: dict | None = None) -> dict:
    """引擎默认参数 + 策略默认参数 + 用户覆盖。"""
    p = dict(config.DEFAULTS)
    strat = get_strategy(strategy_key)
    p.update(strat.defaults)
    p.update(overrides or {})
    p["strategy"] = strategy_key
    p["strategy_display"] = strat.name
    p["freq"] = freq
    p["_daily"] = (freq == "1d")
    return p


def run_one(symbol: str, strategy: str, freq: str = "5min",
            start: str = "20220101", end: str | None = None,
            params: dict | None = None, capital: float = 100_000.0,
            client: TushareClient | None = None, verbose: bool = True
            ) -> BacktestResult:
    p = make_params(strategy, freq, params)
    bars, inst = build_bars(symbol, freq, start, end or _today(), p,
                            client=client, verbose=verbose)
    strat = get_strategy(strategy)
    sig_df = strat.fn(bars, p)
    return run_strategy(sig_df, inst, p, capital)


def run_matrix(symbols: list[str], strategies: list[str],
               freqs: tuple[str, ...] = ("5min",),
               start: str = "20220101", end: str | None = None,
               params: dict | None = None, capital: float = 100_000.0,
               client: TushareClient | None = None, verbose: bool = False
               ) -> pd.DataFrame:
    """多品种 × 多策略 × 多周期排行榜（每个组合一行）。"""
    end = end or _today()
    cli = client or TushareClient()
    rows = []
    for sym, key, fq in product(symbols, strategies, freqs):
        try:
            res = run_one(sym, key, fq, start, end, params, capital, cli, verbose)
        except Exception as exc:                      # 数据缺失/无权限时跳过
            rows.append({"品种": sym, "策略": key, "周期": fq, "笔数": 0,
                         "备注": f"{type(exc).__name__}: {exc}"[:120]})
            continue
        s = summarize(res)
        rows.append({"品种": sym, "策略": key, "周期": fq,
                     "笔数": s["笔数"], "胜率": round(s["胜率"], 4) if s["笔数"] else None,
                     "PF": round(s["PF"], 3) if s["笔数"] else None,
                     "均点": round(s["均点"], 3) if s["笔数"] else None,
                     "合计元": round(s["合计元"], 0),
                     "最大回撤": round(s["最大回撤"], 0),
                     "收益回撤比": round(s["收益回撤比"], 2) if s["笔数"] else None,
                     "正年数": f"{s['正年数']}/{s['年数']}"})
    return pd.DataFrame(rows)


def _today() -> str:
    return pd.Timestamp.now().strftime("%Y%m%d")


def save_result(res: BacktestResult, out_dir: Path | None = None,
                tag: str = "") -> tuple[Path, Path]:
    """把逐笔明细与 HTML 报告落盘。"""
    from .report import write_html
    d = Path(out_dir or config.REPORT_DIR)
    d.mkdir(parents=True, exist_ok=True)
    name = f"{res.instrument.ts_code}_{res.params.get('strategy','strat')}_" \
           f"{res.params.get('freq','')}{('_' + tag) if tag else ''}"
    csv = d / f"{name}.trades.csv"
    html = d / f"{name}.html"
    res.trades.to_csv(csv, index=False, encoding="utf-8-sig")
    write_html(res, html)
    return csv, html


@dataclass
class StrategyInfo:
    key: str
    name: str
    source: str
    license: str
    notes: str


def catalog() -> list[StrategyInfo]:
    return [StrategyInfo(s.key, s.name, s.source, s.license, s.notes)
            for s in list_strategies()]
