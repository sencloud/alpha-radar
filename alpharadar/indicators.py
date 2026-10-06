"""指标层：均线、ATR、RSI、量能基准，以及行情体制度量。

全部为「只用历史数据」的递推/滚动计算，不含未来函数。
"""

from __future__ import annotations

import numpy as np
import pandas as pd


def _wilder(s: pd.Series, n: int) -> pd.Series:
    return s.ewm(alpha=1.0 / n, adjust=False).mean()


def true_range(df: pd.DataFrame) -> pd.Series:
    pc = df["close"].shift(1)
    return pd.concat([df["high"] - df["low"],
                      (df["high"] - pc).abs(),
                      (df["low"] - pc).abs()], axis=1).max(axis=1)


def add_indicators(df: pd.DataFrame, params: dict) -> pd.DataFrame:
    """补上策略与引擎需要的全部指标列。"""
    out = df.reset_index(drop=True).copy()
    c = out["close"].astype(float)
    for n in (5, 10, 20, 40, 60):
        out[f"ma{n}"] = c.rolling(n, min_periods=n).mean()

    atr_n = int(params.get("atr_n", 14))
    out["atr"] = _wilder(true_range(out), atr_n)
    out["atr_ma"] = out["atr"].rolling(int(params.get("atr_ma_n", 50)),
                                       min_periods=int(params.get("atr_ma_n", 50))).mean()

    rsi_n = int(params.get("rsi_n", 14))
    d = c.diff()
    up = d.clip(lower=0.0)
    dn = (-d).clip(lower=0.0)
    au, ad = _wilder(up, rsi_n), _wilder(dn, rsi_n)
    out["rsi"] = 100.0 - 100.0 / (1.0 + (au / ad.replace(0.0, np.nan)))
    out.loc[(ad == 0) & (au > 0), "rsi"] = 100.0
    out.loc[(ad == 0) & (au == 0), "rsi"] = 50.0

    out["vma"] = out["vol"].rolling(int(params.get("vol_n", 20)),
                                    min_periods=int(params.get("vol_n", 20))).mean()
    return out


def add_regime(df: pd.DataFrame, params: dict) -> pd.DataFrame:
    """行情体制度量：ER 效率系数 / ATR 相对水位 / ADX，以及综合闸门 regime_ok。

    经验（见 docs/pitfalls.md）：棕榈油 2022 是单边年、2024 后转区间，
    所有日内策略的盈亏都被体制支配。因此「现在该不该开仓」比「怎么开仓」更重要。
    """
    out = df.copy()
    c, h, l = out["close"].astype(float), out["high"].astype(float), out["low"].astype(float)

    n = int(params.get("er_n", 48))
    path = c.diff().abs().rolling(n, min_periods=n).sum()
    out["er"] = ((c - c.shift(n)).abs() / path.replace(0.0, np.nan))

    n2 = int(params.get("atr_ratio_n", 480))
    med = out["atr"].rolling(n2, min_periods=max(2, n2 // 3)).median()
    out["atr_ratio"] = out["atr"] / med.replace(0.0, np.nan)

    diff_up, diff_dn = h.diff(), -l.diff()
    plus = pd.Series(np.where((diff_up > diff_dn) & (diff_up > 0), diff_up, 0.0),
                     index=out.index)
    minus = pd.Series(np.where((diff_dn > diff_up) & (diff_dn > 0), diff_dn, 0.0),
                      index=out.index)
    atr14 = _wilder(true_range(out), 14)
    pdi = 100.0 * _wilder(plus, 14) / atr14.replace(0.0, np.nan)
    mdi = 100.0 * _wilder(minus, 14) / atr14.replace(0.0, np.nan)
    dx = 100.0 * (pdi - mdi).abs() / (pdi + mdi).replace(0.0, np.nan)
    out["adx"] = _wilder(dx.fillna(0.0), 14)

    ok = pd.Series(True, index=out.index)
    if params.get("er_min"):
        ok &= out["er"] >= params["er_min"]
    if params.get("atr_ratio_min"):
        ok &= out["atr_ratio"] >= params["atr_ratio_min"]
    if params.get("adx_min"):
        ok &= out["adx"] >= params["adx_min"]
    out["regime_ok"] = ok.fillna(False).to_numpy(bool)
    return out


def apply_regime_gate(sig: np.ndarray, df: pd.DataFrame, params: dict) -> np.ndarray:
    """把体制闸门作用在信号上（连续两根确认，避免边界抖动）。"""
    if not params.get("use_regime") or "regime_ok" not in df.columns:
        return sig
    ok = df["regime_ok"].to_numpy(bool)
    if params.get("regime_confirm", 1):
        ok = ok & np.concatenate([[False], ok[:-1]])
    return np.where(ok, sig, 0).astype(sig.dtype)
