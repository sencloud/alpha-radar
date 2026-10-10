"""判定层：把一条回测结果按五道闸门判成结论。

五道闸门以前只写在 docs/findings.md 里，结论全靠手写。这里把它们改写成
机器可判定的规则，阈值全部来自 config/gates.json（带 threshold_version）：

    样本 sample → 尺度 scale → 分年 yearly → 收益回撤比 drawdown → 稳健性 robust

结论映射：
    insufficient  样本闸门没过，或某道闸门所需数据缺失 —— 不算淘汰，不进主列表
    reject        尺度 / 分年 / 收益回撤比 任一不过；failed_gate = 第一道没过的
    pending       1–4 道全过，稳健性待人工复核（自动流程的最好结论）
    tradable      只能由 config/verdict_overrides.json 人工给出（见 falsify.py）
    finding       精选研究笔记（docs/archive/curated.json 里 editor_verdict=finding）

每个闸门输出 {status, value, threshold}，status ∈ pass / marginal / fail /
review / unknown。marginal 只出现在尺度闸门：算通过，但带 scale_marginal 标记。
"""

from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any

import pandas as pd

from .config import ROOT

GATES_PATH = ROOT / "config" / "gates.json"
GATE_ORDER = ("sample", "scale", "yearly", "drawdown", "robust")
REJECT_GATES = ("scale", "yearly", "drawdown")


# ==================== 配置 ====================
def load_gates(path: Path | None = None) -> dict:
    cfg = json.loads(Path(path or GATES_PATH).read_text(encoding="utf-8"))
    if not cfg.get("threshold_version"):
        raise ValueError("gates.json 缺少 threshold_version")
    missing = [g for g in GATE_ORDER if g not in cfg.get("gates", {})]
    if missing:
        raise ValueError(f"gates.json 缺少闸门：{missing}")
    return cfg


def _num(v) -> float | None:
    """转成有限浮点数；None / NaN / inf / 非数字一律返回 None。"""
    if v is None or isinstance(v, bool):
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


# ==================== 尺度：往返成本 ÷ 平均振幅 ====================
def round_trip_cost(inst, px: float | None, slippage_ticks: float = 1) -> float | None:
    """一次开平的总成本，单位 = 报价点（与振幅同单位）。

    口径与 aiquant 的 build_falsification_data.py 一致：
      期货：每边 slippage_ticks 跳滑点 + 双边按手手续费折点（fee / 乘数），
            按成交额收费的品种（如沪银）再加 2 × 价格 × 费率；
      A 股：每边滑点 + 价格 × (双边佣金 + 卖出印花税)。
    """
    slip = 2 * float(slippage_ticks) * inst.tick
    if inst.market == "futures":
        cost = slip + 2 * inst.fee_per_lot / max(inst.mult, 1e-9)
        if inst.fee_rate:
            p = _num(px)
            if p is None:
                return None
            cost += 2 * p * inst.fee_rate
        return cost
    p = _num(px)
    if p is None:
        return None
    return slip + p * (inst.fee_rate * 2 + inst.fee_rate_sell)


def bar_stats(bars: pd.DataFrame) -> tuple[float | None, float | None]:
    """(平均振幅, 平均收盘价)，空表返回 (None, None)。"""
    if bars is None or bars.empty:
        return None, None
    amp = _num((bars["high"].astype(float) - bars["low"].astype(float)).mean())
    px = _num(bars["close"].astype(float).mean())
    return amp, px


def scale_ratio(cost: float | None, amp: float | None) -> float | None:
    c, a = _num(cost), _num(amp)
    if c is None or a is None or a <= 0:
        return None
    return c / a


# 旧结果（加列之前跑的）没有振幅：从本机行情缓存离线重算（不联网）。
# 逻辑移植自 aiquant/tools/strategy-mvp/build_falsification_data.py。
_DERIVED = {"15min": 3, "30min": 6, "60min": 12}


def _segments(mapping: pd.DataFrame) -> list[tuple[str, str, str]]:
    segs: list[list[str]] = []
    for code, d in zip(mapping["mapping_ts_code"], mapping["trade_date"]):
        if segs and segs[-1][0] == code:
            segs[-1][2] = d
        else:
            segs.append([code, d, d])
    return [(c, s, e) for c, s, e in segs]


def _fut_minutes(cache: Path, mapping: pd.DataFrame, freq: str) -> pd.DataFrame:
    import numpy as np
    cal = np.array(sorted(set(mapping["trade_date"])))
    frames = []
    for code, s, e in _segments(mapping):
        f = cache / f"{code}_ft_mins_{freq}.csv"
        if not f.exists():
            continue
        df = pd.read_csv(f, parse_dates=["trade_time"])
        day = df["trade_time"].dt.strftime("%Y%m%d").to_numpy()
        idx = np.searchsorted(cal, day, side="right")
        nxt = np.where(idx < len(cal), cal[np.minimum(idx, len(cal) - 1)], day)
        df["sdate"] = np.where(df["trade_time"].dt.hour >= 20, nxt, day)
        df = df[(df["sdate"] >= s) & (df["sdate"] <= e)]
        if not df.empty:
            frames.append(df[["trade_time", "sdate", "high", "low", "close"]])
    if not frames:
        return pd.DataFrame()
    out = pd.concat(frames, ignore_index=True).drop_duplicates("trade_time")
    return out.sort_values("trade_time").reset_index(drop=True)


def _bucket(df: pd.DataFrame, step: int) -> pd.DataFrame:
    """把 step 根小周期 K 线合成一根（按交易日内序号分桶）。"""
    d = df.copy()
    d["_b"] = d.groupby("sdate", sort=False).cumcount() // step
    g = d.groupby(["sdate", "_b"], sort=False)
    return g.agg(high=("high", "max"), low=("low", "min"),
                 close=("close", "last")).reset_index()


def bars_from_cache(cache: Path, symbol: str, freq: str, start: str = "") -> pd.DataFrame:
    """只读本机缓存拼出 K 线（high/low/close），拿不到返回空表。"""
    from .universe import is_fund, resolve
    cache = Path(cache)
    inst = resolve(symbol)
    try:
        if inst.market == "futures":
            mf = cache / f"{symbol}_mapping.csv"
            if not mf.exists():
                return pd.DataFrame()
            mp = pd.read_csv(mf, dtype={"trade_date": str}).sort_values("trade_date")
            if start:
                mp = mp[mp["trade_date"] >= start]
            if mp.empty:
                return pd.DataFrame()
            if freq == "1d":
                parts = []
                for code, s, e in _segments(mp):
                    f = cache / f"{code}_fut_daily.csv"
                    if f.exists():
                        d = pd.read_csv(f, dtype={"trade_date": str})
                        parts.append(d[(d["trade_date"] >= s) & (d["trade_date"] <= e)])
                if not parts:
                    return pd.DataFrame()
                return pd.concat(parts).drop_duplicates("trade_date")
            df = _fut_minutes(cache, mp, freq)
            if df.empty and freq in _DERIVED:
                base = _fut_minutes(cache, mp, "5min")
                df = _bucket(base, _DERIVED[freq]) if not base.empty else base
            return df
        if freq != "1d":
            f = cache / f"{symbol}_stk_mins_{freq}.csv"
        else:
            f = cache / (f"{symbol}_fund_daily.csv" if is_fund(symbol)
                         else f"{symbol}_daily.csv")
        if not f.exists():
            return pd.DataFrame()
        d = pd.read_csv(f, dtype={"trade_date": str})
        if start and "trade_date" in d.columns:
            d = d[d["trade_date"] >= start]
        return d.dropna(subset=["high", "low", "close"])
    except Exception:
        return pd.DataFrame()


def scale_from_cache(cache: Path, symbol: str, freq: str, start: str = "",
                     slippage_ticks: float = 1) -> dict | None:
    from .universe import resolve
    bars = bars_from_cache(cache, symbol, freq, start)
    amp, px = bar_stats(bars)
    cost = round_trip_cost(resolve(symbol), px, slippage_ticks)
    r = scale_ratio(cost, amp)
    if r is None:
        return None
    return {"ratio": r, "cost": cost, "amplitude": amp, "source": "cache"}


# ==================== 分年 ====================
def yearly_from_trades(trades: pd.DataFrame) -> list[list]:
    """逐笔明细 → [[年, 盈亏], ...]（按年升序，盈亏取整）。"""
    if trades is None or trades.empty or "日期" not in trades.columns:
        return []
    yr = (trades.assign(_y=trades["日期"].astype(str).str[:4])
          .groupby("_y")["净利"].sum())
    return [[str(y), round(float(v), 0)] for y, v in yr.items()]


def full_years(start: str | None, end: str | None) -> list[int]:
    """回测窗口里完整覆盖的自然年（起点在 1 月 10 日前、终点在 12 月 25 日后算完整）。"""
    s, e = str(start or ""), str(end or "")
    if len(s) < 8 or len(e) < 8 or not (s[:4].isdigit() and e[:4].isdigit()):
        return []
    out = []
    for y in range(int(s[:4]), int(e[:4]) + 1):
        if s > f"{y}0110":
            continue
        if e < f"{y}1225":
            continue
        out.append(y)
    return out


# ==================== 判定 ====================
def _gate(status: str, value: Any = None, threshold: Any = None, **extra) -> dict:
    g = {"status": status, "value": value, "threshold": threshold}
    g.update({k: v for k, v in extra.items() if v is not None})
    return g


def _gate_sample(m: dict, c: dict) -> dict:
    trades, years = _num(m.get("trades")), _num(m.get("years"))
    thr = {"min_trades": c["min_trades"], "min_years": c["min_years"]}
    val = {"trades": None if trades is None else int(trades),
           "years": None if years is None else int(years)}
    if trades is None or years is None:
        return _gate("unknown", val, thr, note="缺少笔数或年数")
    ok = trades >= c["min_trades"] and years >= c["min_years"]
    return _gate("pass" if ok else "fail", val, thr)


def _gate_scale(ratio: float | None, c: dict, note: str | None = None) -> dict:
    thr = {"pass_below": c["pass_below"], "fail_at": c["fail_at"]}
    r = _num(ratio)
    if r is None:
        return _gate("unknown", None, thr, note=note or "缺少平均振幅，无法计算成本占比")
    st = "pass" if r < c["pass_below"] else ("marginal" if r < c["fail_at"] else "fail")
    return _gate(st, round(r, 4), thr, note=note)


def _gate_yearly(m: dict, yearly: list | None, window: dict, c: dict,
                 source: str | None) -> dict:
    thr = {"min_positive_ratio": c["min_positive_ratio"],
           "recent_full_years": c["recent_full_years"]}
    k = int(c["recent_full_years"])
    rows = [(str(y), _num(p)) for y, p in (yearly or []) if _num(p) is not None]
    if rows:
        pos = sum(1 for _, p in rows if p > 0)
        n = len(rows)
        src = source or "yearly"
    else:
        pos, n = _num(m.get("positive_years")), _num(m.get("years"))
        src = "summary"
    if pos is None or not n:
        return _gate("unknown", None, thr, note="缺少分年数据")
    pos, n = int(pos), int(n)
    ratio = pos / n
    val: dict = {"positive_years": pos, "years": n, "ratio": round(ratio, 3),
                 "recent": None, "source": src}
    if rows:
        by = {y: p for y, p in rows}
        fy = full_years(window.get("start"), window.get("end"))
        recent_ok = None
        if len(fy) >= k:
            last = fy[-k:]
            val["recent"] = [[str(y), by.get(str(y), 0.0)] for y in last]
            recent_ok = all(by.get(str(y), 0.0) >= 0 for y in last)
        ok = ratio >= c["min_positive_ratio"] or bool(recent_ok)
        return _gate("pass" if ok else "fail", val, thr)
    # 只有汇总数（正年数/年数），没有逐年盈亏：能确定的照判，确定不了的标 unknown
    if ratio >= c["min_positive_ratio"]:
        return _gate("pass", val, thr, note="仅有汇总正年数，未核对近三年")
    if pos < k:
        # 正年数不足 k，最近 k 年不可能全为正；全部「不为负」只剩盈亏恰为 0 的年份，忽略
        return _gate("fail", val, thr, note="仅有汇总正年数：正年数不足，近三年不可能全不为负")
    return _gate("unknown", val, thr, note="仅有汇总正年数，近三个完整年度无法核对")


def _gate_drawdown(m: dict, c: dict) -> dict:
    thr = {"min_pnl_dd": c["min_pnl_dd"], "require_positive_pnl": c["require_positive_pnl"]}
    total, dd, pnl_dd = _num(m.get("total_pnl")), _num(m.get("max_dd")), _num(m.get("pnl_dd"))
    if pnl_dd is None and total is not None and dd is not None and dd != 0:
        pnl_dd = total / abs(dd)
    if pnl_dd is None:
        if total is not None and dd is not None and dd == 0 and total > 0:
            return _gate("pass", None, thr, note="无回撤")
        return _gate("unknown", None, thr, note="缺少总盈亏或最大回撤")
    positive = (total > 0) if total is not None else (pnl_dd > 0)
    ok = pnl_dd >= c["min_pnl_dd"] and (positive or not c["require_positive_pnl"])
    return _gate("pass" if ok else "fail", round(pnl_dd, 3), thr)


def judge(metrics: dict, *, scale: float | None = None, yearly: list | None = None,
          window: dict | None = None, cfg: dict | None = None,
          editor_verdict: str | None = None, scale_note: str | None = None,
          yearly_source: str | None = None) -> dict:
    """按闸门顺序判定一条结果。

    metrics: trades / years / positive_years / total_pnl / max_dd / pnl_dd（任一可缺）
    scale:   往返成本 ÷ 平均振幅（None = 未知）
    yearly:  [[年, 盈亏], ...]；为空时退化为用 positive_years/years 判定并记录原因
    window:  {"start": "YYYYMMDD", "end": "YYYYMMDD"}，用于确定「完整年度」
    返回 {verdict, failed_gate, gates, threshold_version, flags, insufficient_reason}
    """
    cfg = cfg or load_gates()
    gc = cfg["gates"]
    window = window or {}
    gates = {
        "sample": _gate_sample(metrics, gc["sample"]),
        "scale": _gate_scale(scale, gc["scale"], scale_note),
        "yearly": _gate_yearly(metrics, yearly, window, gc["yearly"], yearly_source),
        "drawdown": _gate_drawdown(metrics, gc["drawdown"]),
        "robust": _gate("review", None, None, note="MVP 不做自动判定，待人工复核"),
    }
    flags: list[str] = []
    if gates["scale"]["status"] == "marginal":
        flags.append("scale_marginal")
    if gates["yearly"].get("value") and gates["yearly"]["value"].get("source") == "summary":
        flags.append("yearly_degraded")

    verdict, failed, reason = "pending", None, None
    if editor_verdict == "finding":
        verdict = "finding"
    elif gates["sample"]["status"] != "pass":
        verdict, failed = "insufficient", "sample"
        reason = "sample" if gates["sample"]["status"] == "fail" else "data:sample"
    else:
        for gid in REJECT_GATES:
            if gates[gid]["status"] == "fail":
                verdict, failed = "reject", gid
                break
        else:
            unknown = [g for g in REJECT_GATES if gates[g]["status"] == "unknown"]
            if unknown:
                # 判不了 ≠ 通过：数据缺失的条目按「样本不足」处理，不进主列表
                verdict, reason = "insufficient", f"data:{unknown[0]}"
    return {"verdict": verdict, "failed_gate": failed, "gates": gates,
            "threshold_version": cfg["threshold_version"], "flags": flags,
            "insufficient_reason": reason}


# ==================== 回测时顺手记下判定所需的原料 ====================
def result_extras(res) -> dict:
    """worker / scheduler 写结果时调用：振幅、均价、往返成本、逐年盈亏。

    这几列让导出不必再回读行情缓存（worker 跑完一个品种就删缓存）。
    任何异常都吞掉 —— 判定原料缺失只会让条目落到 insufficient，不能拖垮回测。
    """
    out: dict = {"avg_amp": None, "avg_px": None, "cost_rt": None, "yearly": None}
    try:
        p = res.params or {}
        amp, px = _num(p.get("_avg_amp")), _num(p.get("_avg_px"))
        out["avg_amp"] = None if amp is None else round(amp, 6)
        out["avg_px"] = None if px is None else round(px, 6)
        cost = round_trip_cost(res.instrument, px, p.get("slippage_ticks", 1))
        out["cost_rt"] = None if cost is None else round(cost, 6)
        out["yearly"] = json.dumps(yearly_from_trades(res.trades), ensure_ascii=False)
    except Exception:
        pass
    return out
