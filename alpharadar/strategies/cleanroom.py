"""Clean-room 原创实现：只按公开出版的交易思路编写，不参照任何 TradingView 源码。

为什么单独成文件：library.py 里的 `orb` / `false_breakout` 是 CC BY-NC-SA 4.0
脚本的移植，不能用于付费产品。这里的两个策略思路同源（都是公开了几十年的
经典交易思路），但实现从思路描述出发独立编写，许可 MIT，key 也不同，
旧结果不会被误当成新实现的结果。

  orb_classic    开盘区间突破   思路：Toby Crabel, "Day Trading with Short Term
                                Price Patterns and Opening Range Breakout" (1990)
  breakout_fade  假突破反向     思路：Linda Raschke & Larry Connors, "Street Smarts"
                                (1995) 中的 Turtle Soup

约定同 library.py：信号当根收盘确认，只用 <= t 的数据；撮合与成本交给引擎。
"""

from __future__ import annotations

from collections import deque

import numpy as np
import pandas as pd

from ..indicators import apply_regime_gate
from .base import register, signal_frame


def _hhmm_to_min(s: str, default: int) -> int:
    s = str(s or "").strip()
    if len(s) != 4 or not s.isdigit():
        return default
    return int(s[:2]) * 60 + int(s[2:])


# ==================== 开盘区间突破（Crabel ORB） ====================
# 规则（按公开思路）：
#   1. 每个交易日日盘开盘后的前 or_minutes 分钟构成「开盘区间」（高点 / 低点）；
#   2. 区间形成之后，第一根收盘站上区间高点 → 做多；跌破区间低点 → 做空；
#      每天只做第一次突破（一个方向，一笔）；
#   3. 止损放在区间另一侧；不设固定止盈，持有到止损或收盘平仓（日内）；
#   4. 区间过窄（< or_min_atr × ATR，成本占比过高）或过宽（> or_max_atr × ATR，
#      止损过远）的日子不做。
# K 线时间戳是「收盘时刻」（tushare 分钟线口径），所以开盘区间 = 时间戳落在
# (or_start, or_start + or_minutes] 内的 K 线。夜盘不参与区间。
_ORBC_DEFAULTS = {
    "or_start": "0900", "or_minutes": 30, "or_min_atr": 0.5, "or_max_atr": 6.0,
    "stop_mode": "struct", "use_target": 0, "trail_stop": 0,
    "max_hold_bars": 100000, "no_entry_before": "", "no_entry_after": "1430",
    "max_entries_per_day": 1, "cooldown_bars": 0,
}


@register("orb_classic", "开盘区间突破（原创实现）", source="原创实现", license="MIT",
          origin="思路来源：Toby Crabel《Day Trading with Short Term Price Patterns and "
                 "Opening Range Breakout》(1990) 公开的开盘区间突破思路；"
                 "独立实现，未参照任何 TradingView 源码。",
          defaults=_ORBC_DEFAULTS, freqs=("1min", "5min", "15min", "30min"),
          notes="日盘前 N 分钟定区间，收盘突破区间边沿入场，止损在区间另一侧，"
                "每天只做第一次突破，收盘平仓。")
def _orb_classic(df: pd.DataFrame, p: dict) -> pd.DataFrame:
    n = len(df)
    tt = df["trade_time"]
    mins = (tt.dt.hour * 60 + tt.dt.minute).to_numpy()
    sd = df["sdate"].astype(str).to_numpy()
    h = df["high"].to_numpy(float)
    lo = df["low"].to_numpy(float)
    c = df["close"].to_numpy(float)
    atr = df["atr"].to_numpy(float)

    open_m = _hhmm_to_min(p.get("or_start"), 9 * 60)
    end_m = open_m + int(p.get("or_minutes", 30))
    last_m = _hhmm_to_min(p.get("no_entry_after"), 15 * 60)
    min_w, max_w = float(p.get("or_min_atr", 0.0)), float(p.get("or_max_atr", 1e9))

    sig = np.zeros(n, dtype=np.int8)
    stop = np.full(n, np.nan)
    tag = np.array([""] * n, dtype=object)

    day, top, bot, traded = None, -np.inf, np.inf, False
    for i in range(n):
        if sd[i] != day:
            day, top, bot, traded = sd[i], -np.inf, np.inf, False
        m = mins[i]
        if open_m < m <= end_m:                      # 区间形成中：只记录，不交易
            top, bot = max(top, h[i]), min(bot, lo[i])
            continue
        if traded or not (end_m < m <= last_m):
            continue
        if not (np.isfinite(top) and np.isfinite(bot) and np.isfinite(atr[i])):
            continue
        width = top - bot
        if width < min_w * atr[i] or width > max_w * atr[i]:
            continue
        if c[i] > top:
            sig[i], stop[i], tag[i], traded = 1, bot, "ORB上破", True
        elif c[i] < bot:
            sig[i], stop[i], tag[i], traded = -1, top, "ORB下破", True
    sig = apply_regime_gate(sig, df, p)
    return signal_frame(df, sig, stop=stop, tag=tag)


# ==================== 假突破反向（Turtle Soup 思路） ====================
# 规则（按公开思路）：
#   1. 当根最高价创出「前 lookback 根」的新高，且被突破的那个旧高点至少是
#      min_age 根之前形成的（旧高点要「有分量」，不是刚刚才出现的）；
#   2. 突破后 window 根之内（含突破根），只要有一根收盘回到旧高点之下 → 做空：
#      突破失败，追高的人被套；
#   3. 止损 = 突破以来的最高价 + stop_buf_atr × ATR；止盈交给引擎（tgt_atr × ATR）；
#   4. 新低方向完全对称 → 做多。
_FADE_DEFAULTS = {
    "fade_lookback": 20, "fade_min_age": 4, "fade_window": 3,
    "stop_mode": "struct", "stop_buf_atr": 0.5, "use_target": 1, "tgt_atr": 2.5,
    "max_entries_per_day": 2, "cooldown_bars": 10,
}


def _prior_extreme(x: np.ndarray, n: int, want_max: bool) -> tuple[np.ndarray, np.ndarray]:
    """每根 K 线之前 n 根（不含当根）的极值与它距今的根数；不足 n 根为 NaN。"""
    size = len(x)
    val = np.full(size, np.nan)
    age = np.full(size, -1, dtype=np.int64)
    dq: deque[int] = deque()
    for i in range(size):
        while dq and dq[0] < i - n:
            dq.popleft()
        if i >= n and dq:
            val[i], age[i] = x[dq[0]], i - dq[0]
        # 相等时保留更新的那根：旧极值被「重新触及」后，按最近一次计龄
        if want_max:
            while dq and x[dq[-1]] <= x[i]:
                dq.pop()
        else:
            while dq and x[dq[-1]] >= x[i]:
                dq.pop()
        dq.append(i)
    return val, age


@register("breakout_fade", "假突破反向（原创实现）", source="原创实现", license="MIT",
          origin="思路来源：Linda Raschke 与 Larry Connors《Street Smarts》(1995) 公开的 "
                 "Turtle Soup 假突破反向思路；独立实现，未参照任何 TradingView 源码。",
          defaults=_FADE_DEFAULTS,
          notes="创出前 N 根新高（旧高点至少 M 根前形成）后几根内收盘跌回旧高点之下 → 做空；"
                "新低对称做多。止损在突破极值外侧。")
def _breakout_fade(df: pd.DataFrame, p: dict) -> pd.DataFrame:
    n = len(df)
    h = df["high"].to_numpy(float)
    lo = df["low"].to_numpy(float)
    c = df["close"].to_numpy(float)
    atr = df["atr"].to_numpy(float)
    look = max(2, int(p.get("fade_lookback", 20)))
    min_age = int(p.get("fade_min_age", 4))
    win = max(1, int(p.get("fade_window", 3)))
    buf = float(p.get("stop_buf_atr", 0.5))

    ph, ph_age = _prior_extreme(h, look, True)
    pl, pl_age = _prior_extreme(lo, look, False)

    sig = np.zeros(n, dtype=np.int8)
    stop = np.full(n, np.nan)
    tag = np.array([""] * n, dtype=object)

    up = None        # [旧高点, 截止根, 突破以来最高价]
    dn = None        # [旧低点, 截止根, 突破以来最低价]
    for i in range(n):
        if up is None and np.isfinite(ph[i]) and h[i] > ph[i] and ph_age[i] >= min_age:
            up = [ph[i], i + win - 1, h[i]]
        if dn is None and np.isfinite(pl[i]) and lo[i] < pl[i] and pl_age[i] >= min_age:
            dn = [pl[i], i + win - 1, lo[i]]
        if up is not None:
            up[2] = max(up[2], h[i])
            if c[i] < up[0] and np.isfinite(atr[i]):
                sig[i], stop[i], tag[i] = -1, up[2] + buf * atr[i], "假突破做空"
                up = None
            elif i >= up[1]:
                up = None
        if dn is not None:
            dn[2] = min(dn[2], lo[i])
            if sig[i] == 0 and c[i] > dn[0] and np.isfinite(atr[i]):
                sig[i], stop[i], tag[i] = 1, dn[2] - buf * atr[i], "假突破做多"
                dn = None
            elif i >= dn[1]:
                dn = None
    sig = apply_regime_gate(sig, df, p)
    return signal_frame(df, sig, stop=stop, tag=tag)
