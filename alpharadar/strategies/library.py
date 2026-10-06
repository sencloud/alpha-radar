"""策略库实现。

每个策略都标注了出处与许可；TradingView 来源的脚本按 CC BY-NC-SA / MPL 等
原许可保留署名，商业使用请自行确认许可条款。
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from ..indicators import apply_regime_gate
from .base import register, signal_frame


def _wilder_atr(h, l, c, n: int) -> np.ndarray:
    pc = np.concatenate([[np.nan], c[:-1]])
    tr = np.nanmax(np.vstack([h - l, np.abs(h - pc), np.abs(l - pc)]), axis=0)
    return pd.Series(tr).ewm(alpha=1.0 / n, adjust=False).mean().to_numpy()


# ==================== 趋势族：ATR 跟踪止损 ====================

def _supertrend(h, l, c, period, mult):
    n = len(c)
    src = (h + l) / 2.0
    atr = _wilder_atr(h, l, c, period)
    trend = np.ones(n, dtype=np.int8)
    stop = np.full(n, np.nan)
    up_p = dn_p = np.nan
    t = 1
    for i in range(1, n):
        if not np.isfinite(atr[i]):
            continue
        up, dn = src[i] - mult * atr[i], src[i] + mult * atr[i]
        up1 = up_p if np.isfinite(up_p) else up
        dn1 = dn_p if np.isfinite(dn_p) else dn
        if c[i - 1] > up1:
            up = max(up, up1)
        if c[i - 1] < dn1:
            dn = min(dn, dn1)
        if t == -1 and c[i] > dn1:
            t = 1
        elif t == 1 and c[i] < up1:
            t = -1
        up_p, dn_p = up, dn
        trend[i], stop[i] = t, (up if t == 1 else dn)
    return trend, stop


def _utbot(h, l, c, period, key):
    n = len(c)
    atr = _wilder_atr(h, l, c, period)
    ts = np.full(n, np.nan)
    trend = np.ones(n, dtype=np.int8)
    prev = np.nan
    t = 1
    for i in range(1, n):
        if not np.isfinite(atr[i]):
            continue
        if not np.isfinite(prev):
            ts[i] = prev = c[i] - key * atr[i]
            continue
        if c[i] > prev and c[i - 1] > prev:
            cur = max(prev, c[i] - key * atr[i])
        elif c[i] < prev and c[i - 1] < prev:
            cur = min(prev, c[i] + key * atr[i])
        elif c[i] > prev:
            cur = c[i] - key * atr[i]
        else:
            cur = c[i] + key * atr[i]
        if c[i - 1] < prev and c[i] > prev:
            t = 1
        elif c[i - 1] > prev and c[i] < prev:
            t = -1
        ts[i], prev, trend[i] = cur, cur, t
    return trend, ts


def _chandelier(h, l, c, period, mult):
    n = len(c)
    atr = _wilder_atr(h, l, c, period)
    hh = pd.Series(h).rolling(period, min_periods=period).max().to_numpy()
    ll = pd.Series(l).rolling(period, min_periods=period).min().to_numpy()
    lp = sp = np.nan
    d = 1
    trend = np.ones(n, dtype=np.int8)
    stop = np.full(n, np.nan)
    for i in range(1, n):
        if not np.isfinite(atr[i]) or not np.isfinite(hh[i]):
            continue
        lr, sr = hh[i] - mult * atr[i], ll[i] + mult * atr[i]
        lp = max(lr, lp) if (np.isfinite(lp) and c[i - 1] > lp) else lr
        sp = min(sr, sp) if (np.isfinite(sp) and c[i - 1] < sp) else sr
        d = 1 if c[i] > sp else (-1 if c[i] < lp else d)
        trend[i], stop[i] = d, (lp if d == 1 else sp)
    return trend, stop


def _trend_family(df: pd.DataFrame, p: dict, kind: str) -> pd.DataFrame:
    h = df["high"].to_numpy(float)
    l = df["low"].to_numpy(float)
    c = df["close"].to_numpy(float)
    period, mult = int(p["atr_period"]), float(p["atr_key"])
    if kind == "utbot":
        trend, stop = _utbot(h, l, c, period, mult)
    elif kind == "chandelier":
        trend, stop = _chandelier(h, l, c, period, mult)
    else:
        trend, stop = _supertrend(h, l, c, period, mult)
    flip = np.zeros(len(df), dtype=np.int8)
    flip[1:] = np.where(trend[1:] != trend[:-1], trend[1:], 0)
    warm = df["ma20"].notna().to_numpy() & np.isfinite(stop)
    flip = np.where(warm, flip, 0).astype(np.int8)
    flip = apply_regime_gate(flip, df, p)
    tag = np.where(flip > 0, f"{kind}翻多", np.where(flip < 0, f"{kind}翻空", ""))
    return signal_frame(df, flip, stop=stop, st_stop=stop, tag=tag)


_TREND_DEFAULTS = {"use_target": 0, "trail_stop": 1, "max_hold_bars": 100000,
                   "cooldown_bars": 0, "max_entries_per_day": 99,
                   "atr_period": 10, "atr_key": 3.0}


@register("supertrend", "SuperTrend", source="TradingView @KivancOzbilgic",
          license="MPL-2.0", defaults=_TREND_DEFAULTS,
          notes="hl2 ± k×ATR 双轨棘轮，收盘穿越对侧翻向。")
def _supertrend_strategy(df, p):
    return _trend_family(df, p, "supertrend")


@register("utbot", "UT Bot", source="TradingView @QuantNomad", license="—",
          defaults=_TREND_DEFAULTS,
          notes="close ± k×ATR 单轨棘轮。回测中棕榈油 5 分钟表现最好的趋势变体。")
def _utbot_strategy(df, p):
    return _trend_family(df, p, "utbot")


@register("chandelier", "Chandelier Exit", source="TradingView @everget",
          license="MIT", defaults=_TREND_DEFAULTS,
          notes="highest(n) − k×ATR 棘轮止损。")
def _chandelier_strategy(df, p):
    return _trend_family(df, p, "chandelier")


# ==================== 冰点反转（原创，形态来自沪银 1 分钟图） ====================

_VR_DEFAULTS = {
    "cap_vol_mult": 2.0, "cap_low_n": 30, "cap_ext_atr": 1.5, "cap_expire": 120,
    "cap_same_day": True, "require_trend": True, "rally_atr": 1.5, "hl_n": 5,
    "hl_atr": 0.5, "ma_sqz_atr": 1.0, "box_max_atr": 8.0, "rsi_min": 50.0,
    "entry_mode": "breakout", "retest_bars": 30,
    "stop_mode": "struct", "stop_buf_atr": 0.5, "tgt_atr": 2.5,
    "max_hold_bars": 60, "max_entries_per_day": 2, "cooldown_bars": 10,
    "no_entry_before": "0910", "no_entry_after": "1430",
}


@register("vreversal", "冰点反转", source="原创（形态来自沪银 2606 一分钟图）",
          license="MIT", defaults=_VR_DEFAULTS,
          freqs=("1min", "5min", "15min", "30min", "60min"),
          notes="量能高潮创新低 → V 型回抽 → 二次探底抬高 → 均线粘合后突破箱体。")
def _vreversal(df: pd.DataFrame, p: dict) -> pd.DataFrame:
    n = len(df)
    c = df["close"].to_numpy(float)
    h = df["high"].to_numpy(float)
    lo = df["low"].to_numpy(float)
    atr = df["atr"].to_numpy(float)
    rsi = df["rsi"].to_numpy(float)
    vol = df["vol"].to_numpy(float)
    vma = df["vma"].to_numpy(float)
    ma5, ma10 = df["ma5"].to_numpy(float), df["ma10"].to_numpy(float)
    ma20, ma60 = df["ma20"].to_numpy(float), df["ma60"].to_numpy(float)
    sdate = df["sdate"].to_numpy(object)

    llow = df["low"].shift(1).rolling(int(p["cap_low_n"]),
                                      min_periods=int(p["cap_low_n"])).min().to_numpy()
    hhigh = df["high"].shift(1).rolling(int(p["cap_low_n"]),
                                        min_periods=int(p["cap_low_n"])).max().to_numpy()
    recent_min = pd.Series(lo).rolling(int(p["hl_n"]), min_periods=1).min().to_numpy()
    cmax = np.maximum(np.maximum(ma5, ma10), ma20)
    with np.errstate(invalid="ignore"):
        sqz = (cmax - np.minimum(np.minimum(ma5, ma10), ma20)) <= p["ma_sqz_atr"] * atr
        cap_long = ((vol >= p["cap_vol_mult"] * vma) & (lo <= llow)
                    & ((c - ma20) <= -p["cap_ext_atr"] * atr))
        cap_short = ((vol >= p["cap_vol_mult"] * vma) & (h >= hhigh)
                     & ((c - ma20) >= p["cap_ext_atr"] * atr))
    if p["require_trend"]:
        cap_long &= ma20 < ma60
        cap_short &= ma20 > ma60
    ok = np.isfinite(atr) & np.isfinite(cmax) & (vma > 0)
    cap_long &= ok
    cap_short &= ok

    sig = np.zeros(n, dtype=np.int8)
    stop = np.full(n, np.nan)
    tag = np.array([""] * n, dtype=object)
    Li, Ll, Lh, Lsd = -1, np.nan, np.nan, None
    Si, Sh, Sl, Ssd = -1, np.nan, np.nan, None
    for t in range(n):
        if Li >= 0:
            if t - Li > p["cap_expire"] or (p["cap_same_day"] and sdate[t] != Lsd):
                Li = -1
            else:
                lv = Lh
                broke = (c[t] > lv) and (c[t] > cmax[t])
                if lo[t] < Ll:
                    Ll = lo[t]
                if h[t] > Lh:
                    Lh = h[t]
                if (Lh - Ll >= p["rally_atr"] * atr[t] and broke
                        and recent_min[t] >= Ll + p["hl_atr"] * atr[t]
                        and (Lh - Ll) <= p["box_max_atr"] * atr[t]
                        and sqz[t] and rsi[t] >= p["rsi_min"] and c[t] > ma20[t]):
                    sig[t] = 1
                    stop[t] = recent_min[t] - p["stop_buf_atr"] * atr[t]
                    tag[t] = "冰点反转"
                    Li = -1
        if Si >= 0:
            if t - Si > p["cap_expire"] or (p["cap_same_day"] and sdate[t] != Ssd):
                Si = -1
            else:
                sv = Sl
                broke = (c[t] < sv) and (c[t] < np.minimum(np.minimum(ma5, ma10), ma20)[t])
                if h[t] > Sh:
                    Sh = h[t]
                if lo[t] < Sl:
                    Sl = lo[t]
                if (Sh - Sl >= p["rally_atr"] * atr[t] and broke
                        and recent_min[t] <= Sh - p["hl_atr"] * atr[t]
                        and (Sh - Sl) <= p["box_max_atr"] * atr[t]
                        and sqz[t] and rsi[t] <= 100 - p["rsi_min"] and c[t] < ma20[t]):
                    sig[t] = -1
                    stop[t] = (pd.Series(h).rolling(int(p["hl_n"]), min_periods=1)
                               .max().to_numpy()[t] + p["stop_buf_atr"] * atr[t])
                    tag[t] = "顶部脉冲反转"
                    Si = -1
        if cap_long[t]:
            Li, Ll, Lh, Lsd = t, lo[t], h[t], sdate[t]
        if cap_short[t]:
            Si, Sh, Sl, Ssd = t, h[t], lo[t], sdate[t]
    sig = apply_regime_gate(sig, df, p)
    return signal_frame(df, sig, stop=stop, tag=tag)


# ==================== 开盘区间突破（ORB） ====================

_ORB_DEFAULTS = {"ib_end": "0930", "ib_ext": 1.0, "ib_min_atr": 1.0,
                 "stop_mode": "struct", "tgt_atr": 2.5, "no_entry_after": "1430",
                 "max_entries_per_day": 1, "cooldown_bars": 0}


@register("orb", "开盘区间突破", source="TradingView @LuxAlgo（Initial Balance Breakout）",
          license="CC BY-NC-SA 4.0", defaults=_ORB_DEFAULTS,
          freqs=("1min", "5min", "15min", "30min", "60min"),
          notes="开盘首 30 分钟为箱体，突破上/下沿入场，目标 = 箱体高度 × 倍数。")
def _orb(df: pd.DataFrame, p: dict) -> pd.DataFrame:
    n = len(df)
    hm = df["trade_time"].dt.strftime("%H%M").to_numpy()
    sdate = df["sdate"].to_numpy(object)
    h, lo = df["high"].to_numpy(float), df["low"].to_numpy(float)
    c, atr = df["close"].to_numpy(float), df["atr"].to_numpy(float)
    ib_end, brk_end = p["ib_end"], p.get("no_entry_after") or "1500"

    box: dict[str, list[float]] = {}
    for i in np.where((hm >= "0900") & (hm <= ib_end))[0]:
        b = box.setdefault(sdate[i], [-np.inf, np.inf])
        b[0], b[1] = max(b[0], h[i]), min(b[1], lo[i])

    sig = np.zeros(n, dtype=np.int8)
    stop = np.full(n, np.nan)
    tag = np.array([""] * n, dtype=object)
    fired: set = set()
    for i in range(n):
        k = sdate[i]
        if k not in box or not (ib_end < hm[i] <= brk_end):
            continue
        hi, low = box[k]
        if not (np.isfinite(hi) and np.isfinite(low) and np.isfinite(atr[i])):
            continue
        if hi - low < p["ib_min_atr"] * atr[i]:
            continue
        if (k, 1) not in fired and c[i] > hi:
            sig[i], stop[i], tag[i] = 1, low, "IB上破"
            fired.add((k, 1))
        elif (k, -1) not in fired and c[i] < low:
            sig[i], stop[i], tag[i] = -1, hi, "IB下破"
            fired.add((k, -1))
    sig = apply_regime_gate(sig, df, p)
    return signal_frame(df, sig, stop=stop, tag=tag)


# ==================== 假突破反向（False Breakout） ====================

_FB_DEFAULTS = {"fb_prd": 20, "fb_min_bars": 5, "fb_max_valid": 5,
                "stop_mode": "struct", "stop_buf_atr": 0.5, "tgt_atr": 2.5,
                "max_entries_per_day": 2, "cooldown_bars": 10}


@register("false_breakout", "假突破反向",
          source="TradingView @Zeiierman（False Breakout (Expo)）",
          license="CC BY-NC-SA 4.0", defaults=_FB_DEFAULTS,
          notes="连续创新高后收回跌破突破根低点 → 反向做空；实测在棕榈油上为负期望。")
def _false_breakout(df: pd.DataFrame, p: dict) -> pd.DataFrame:
    n = len(df)
    h, lo = df["high"].to_numpy(float), df["low"].to_numpy(float)
    c, atr = df["close"].to_numpy(float), df["atr"].to_numpy(float)
    prd, minp, maxp = int(p["fb_prd"]), int(p["fb_min_bars"]), int(p["fb_max_valid"])
    hi = pd.Series(h).rolling(prd, min_periods=prd).max().to_numpy()
    low = pd.Series(lo).rolling(prd, min_periods=prd).min().to_numpy()
    sig = np.zeros(n, dtype=np.int8)
    stop = np.full(n, np.nan)
    tag = np.array([""] * n, dtype=object)
    count, val, i0, i1 = 0, np.nan, -1, -1
    for i in range(2, n):
        if np.isfinite(hi[i]) and hi[i] > hi[i - 1] and hi[i - 1] <= hi[i - 2]:
            count = (0 if count > 0 else count) - 1
            val, i1, i0 = lo[i], i0, i
        if np.isfinite(low[i]) and low[i] < low[i - 1] and low[i - 1] >= low[i - 2]:
            count = (0 if count < 0 else count) + 1
            val, i1, i0 = h[i], i0, i
        if not (np.isfinite(val) and np.isfinite(atr[i]) and i0 >= 0):
            continue
        share = i1 >= 0 and i1 + minp < i0 and (i - maxp) <= i0
        if count < -1 and c[i] < val <= c[i - 1] and share:
            sig[i] = -1
            stop[i] = float(np.nanmax(h[max(0, i0 - prd):i + 1])) + p["stop_buf_atr"] * atr[i]
            tag[i] = "假突破做空"
            count = 0
        elif count > 1 and c[i] > val >= c[i - 1] and share:
            sig[i] = 1
            stop[i] = float(np.nanmin(lo[max(0, i0 - prd):i + 1])) - p["stop_buf_atr"] * atr[i]
            tag[i] = "假突破做多"
            count = 0
    sig = apply_regime_gate(sig, df, p)
    return signal_frame(df, sig, stop=stop, tag=tag)


# ==================== 基线：双均线交叉 ====================

_EMA_DEFAULTS = {"fast": 9, "slow": 21, "atr_key": 3.0,
                 "use_target": 0, "trail_stop": 1,
                 "max_hold_bars": 100000, "cooldown_bars": 0,
                 "max_entries_per_day": 99}


@register("ema_cross", "双均线交叉（基线）", source="通用", license="MIT",
          defaults=_EMA_DEFAULTS,
          notes="最朴素的对照组：快慢均线金叉做多、死叉做空，用 ATR 跟踪止损离场。")
def _ema_cross(df: pd.DataFrame, p: dict) -> pd.DataFrame:
    c = df["close"].astype(float)
    fast = c.ewm(span=int(p["fast"]), adjust=False).mean()
    slow = c.ewm(span=int(p["slow"]), adjust=False).mean()
    up = (fast > slow).to_numpy()
    flip = np.zeros(len(df), dtype=np.int8)
    flip[1:] = np.where(up[1:] != up[:-1], np.where(up[1:], 1, -1), 0)
    atr = df["atr"].to_numpy(float)
    stop = np.where(flip == 1, c.to_numpy() - p["atr_key"] * atr,
                    np.where(flip == -1, c.to_numpy() + p["atr_key"] * atr, np.nan))
    flip = apply_regime_gate(flip, df, p)
    tag = np.where(flip > 0, "金叉", np.where(flip < 0, "死叉", ""))
    return signal_frame(df, flip, stop=stop, st_stop=stop, tag=tag)
