"""回测指标与文本报告。

指标口径：
- 均点：每手（股票为每股）净盈亏，已扣全部成本 —— 跨品种可比的第一个数字；
- PF（盈利因子）= 总盈利 / 总亏损；
- 平衡胜率 = 1 / (1 + 平均止盈点数 / 平均止损点数)，用来判断胜率是否够；
- 收益回撤比 = 总盈亏 / 最大回撤，衡量这套参数值不值得做。
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from .engine import BacktestResult


def drawdown(equity: pd.DataFrame) -> tuple[float, float]:
    if equity.empty:
        return 0.0, 0.0
    eq = equity["equity"].astype(float)
    peak = eq.cummax()
    dd = eq - peak
    return float(dd.min()), float((dd / peak.replace(0, np.nan)).min() or 0.0)


def summarize(res: BacktestResult) -> dict:
    tr = res.trades
    dd_abs, dd_pct = drawdown(res.equity)
    out = {
        "symbol": res.instrument.ts_code, "name": res.instrument.name,
        "market": res.instrument.market, "笔数": 0, "胜率": np.nan,
        "PF": np.nan, "均点": np.nan, "合计元": 0.0,
        "最大回撤": dd_abs, "最大回撤%": dd_pct,
        "期末权益": float(res.equity["equity"].iloc[-1]) if not res.equity.empty
        else res.capital,
        "正年数": 0, "年数": 0, "收益回撤比": np.nan, "平均持有根数": np.nan,
    }
    if tr.empty:
        return out
    win = tr[tr["净利"] > 0]
    loss = tr[tr["净利"] <= 0]
    gw, gl = win["净利"].sum(), -loss["净利"].sum()
    yr = tr.assign(年=tr["日期"].str[:4]).groupby("年")["净利"].sum()
    total = tr["净利"].sum()
    unit = res.instrument.mult * res.instrument.lot
    out.update({
        "笔数": len(tr), "胜率": len(win) / len(tr),
        "PF": float(gw / gl) if gl > 0 else np.inf,
        # 每手净点数 = 毛点数 − 每手费用折成的点数
        "均点": float(tr["点数"].mean()
                      - (tr["费用"] / tr["手数"]).mean() / max(unit, 1e-9)),
        "合计元": float(total),
        "正年数": int((yr > 0).sum()), "年数": len(yr),
        "收益回撤比": float(total / abs(dd_abs)) if dd_abs else np.nan,
        "平均持有根数": float(tr["持有根数"].mean()),
        "平均止损点": float(tr["止损点"].mean()),
        "平均止盈点": float(tr["止盈点"].mean()),
    })
    sp, tp = out["平均止损点"], out["平均止盈点"]
    if sp > 0 and tp > 0:
        out["平衡胜率"] = 1.0 / (1.0 + tp / sp)
    return out


def yearly_table(res: BacktestResult) -> pd.DataFrame:
    tr = res.trades
    if tr.empty:
        return pd.DataFrame()
    return (tr.assign(年=tr["日期"].str[:4])
            .groupby("年").agg(笔数=("净利", "size"), 盈亏=("净利", "sum"),
                               胜率=("净利", lambda s: float((s > 0).mean())),
                               均点=("点数", "mean")).round(2))


def group_table(res: BacktestResult, col: str) -> pd.DataFrame:
    tr = res.trades
    if tr.empty or col not in tr.columns:
        return pd.DataFrame()
    return tr.groupby(col).agg(笔数=("净利", "size"), 盈亏=("净利", "sum"),
                               均值=("净利", "mean")).round(2)


def render_text(res: BacktestResult) -> str:
    s = summarize(res)
    scr = res.params.get("_strategy_name", res.params.get("strategy", "?"))
    p = res.params
    L = [
        f"===== {s['name']}（{s['symbol']} · {s['market']}） / {scr} =====",
        f"区间 {res.equity['date'].iloc[0] if not res.equity.empty else '-'}"
        f" ~ {res.equity['date'].iloc[-1] if not res.equity.empty else '-'}"
        f"   初始资金 {res.capital:,.0f}",
        f"期末权益 {s['期末权益']:,.0f}   最大回撤 {s['最大回撤']:,.0f}"
        f"（{s['最大回撤%']:.1%}）   收益回撤比 {s['收益回撤比']:.2f}"
        if s["笔数"] else "无交易",
    ]
    if s["笔数"]:
        L += [
            f"笔数 {s['笔数']}   胜率 {s['胜率']:.1%}   PF {s['PF']:.2f}"
            f"   每手均点 {s['均点']:+.3f}   合计 {s['合计元']:+,.0f} 元",
            f"平均止损 {s['平均止损点']:.1f} 点 / 平均止盈 {s['平均止盈点']:.1f} 点"
            f"   平衡胜率 {s.get('平衡胜率', float('nan')):.1%}"
            f"   平均持有 {s['平均持有根数']:.1f} 根",
            f"正年数 {s['正年数']}/{s['年数']}",
            "",
            "分年：",
            yearly_table(res).to_string(),
            "",
            "离场原因：",
            group_table(res, "原因").to_string(),
        ]
    return "\n".join(L)
