"""行情装配：把 Tushare 原始数据整理成统一 K 线表。

统一列：trade_time / open / high / low / close / vol / sdate
  - 期货：按主力映射切段，逐段取数（换月跳空不污染指标）；
    夜盘（>=20:00）归下一个交易日，与国内行情软件口径一致。
  - A 股：直接取数，sdate = 自然交易日；分钟线需要 stk_mins 权限。
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from .tushare_client import FREQS, TushareClient
from .universe import Instrument, resolve


def _segments(mapping: pd.DataFrame) -> list[tuple[str, str, str]]:
    segs: list[list[str]] = []
    for code, d in zip(mapping["mapping_ts_code"], mapping["trade_date"]):
        if segs and segs[-1][0] == code:
            segs[-1][2] = d
        else:
            segs.append([code, d, d])
    return [(c, s, e) for c, s, e in segs]


def _assign_sdate(df: pd.DataFrame, calendar: list[str]) -> pd.DataFrame:
    """夜盘（>=20:00）归下一个交易日；日盘归当日。"""
    out = df.copy()
    cal = np.array(sorted(set(calendar)))
    day = out["trade_time"].dt.strftime("%Y%m%d")
    night = out["trade_time"].dt.hour >= 20
    idx = np.searchsorted(cal, day.to_numpy(), side="right")
    nxt = np.where(idx < len(cal), cal[np.minimum(idx, len(cal) - 1)], day.to_numpy())
    out["sdate"] = np.where(night, nxt, day.to_numpy())
    return out


def _resample(df: pd.DataFrame, tf: int) -> pd.DataFrame:
    """1 分钟合成 tf 分钟（桶 = 向上取整到 tf 的倍数，与行情软件一致）。"""
    if tf <= 1:
        return df
    d = df.copy()
    mins = d["trade_time"].dt.hour * 60 + d["trade_time"].dt.minute
    d["_b"] = ((mins + tf - 1) // tf) * tf
    g = d.groupby(["sdate", "_b"], sort=False)
    out = g.agg(open=("open", "first"), high=("high", "max"), low=("low", "min"),
                close=("close", "last"), vol=("vol", "sum"))
    out["trade_time"] = g["trade_time"].max()
    return (out.reset_index().drop(columns="_b")
            .sort_values("trade_time").reset_index(drop=True))


def _clean(df: pd.DataFrame) -> pd.DataFrame:
    keep = ["trade_time", "open", "high", "low", "close", "vol", "sdate"]
    for c in keep:
        if c not in df.columns:
            df[c] = np.nan
    out = df[keep].dropna(subset=["open", "high", "low", "close"])
    return out.sort_values("trade_time").reset_index(drop=True)


def load_bars(symbol: str, freq: str = "1d", start: str = "20220101",
              end: str | None = None, client: TushareClient | None = None,
              warmup_days: int = 10, verbose: bool = True,
              instrument: Instrument | None = None) -> pd.DataFrame:
    """取一个品种的 K 线序列。

    freq：1min / 5min / 15min / 30min / 60min / 1d
    期货按主力连续拼段（日线频率直接取主力日线）；A 股直接取。
    """
    if freq not in FREQS:
        raise ValueError(f"freq 仅支持 {FREQS}：{freq}")
    end = end or pd.Timestamp.now().strftime("%Y%m%d")
    inst = instrument or resolve(symbol)
    cli = client or TushareClient()

    if inst.market == "stock":
        raw = (cli.stk_daily(inst.ts_code, start, end) if freq == "1d"
               else cli.stk_minutes(inst.ts_code, freq, start, end))
        raw["trade_time"] = (pd.to_datetime(raw["trade_date"])
                             if freq == "1d" else pd.to_datetime(raw["trade_time"]))
        raw["sdate"] = raw["trade_time"].dt.strftime("%Y%m%d")
        if freq == "1d":
            raw["trade_time"] = raw["trade_time"] + pd.Timedelta(hours=15)
        return _clean(raw)

    # 期货
    if freq == "1d":
        mp = cli.fut_mapping(inst.ts_code, start, end)
        parts = []
        for code, s, e in _segments(mp):
            try:
                d = cli.fut_daily(code, s, e)
            except RuntimeError:
                continue
            d["trade_time"] = pd.to_datetime(d["trade_date"]) + pd.Timedelta(hours=15)
            d["sdate"] = d["trade_date"].astype(str)
            parts.append(d)
        if not parts:
            raise RuntimeError(f"未取到日线：{symbol} {start}~{end}")
        return _clean(pd.concat(parts).drop_duplicates("sdate"))

    mp = cli.fut_mapping(inst.ts_code, start, end)
    out = []
    for i, (code, s, e) in enumerate(_segments(mp), 1):
        ds = (pd.Timestamp(s) - pd.Timedelta(days=warmup_days)).strftime("%Y%m%d")
        try:
            daily = cli.fut_daily(code, ds, e)
            m = cli.fut_minutes(code, freq, ds, e)
        except RuntimeError as exc:
            if verbose:
                print(f"  [{i}] 跳过 {code}：{exc}")
            continue
        m = _assign_sdate(m, daily["trade_date"].astype(str).tolist())
        m = m[m["sdate"] <= e]                       # 段末夜盘属于下一段
        out.append(m)
        if verbose:
            print(f"  [{i}] {code} {s}~{e}  {len(m):,} 根 {freq}")
    if not out:
        raise RuntimeError(f"未取到 {freq} 数据：{symbol} {start}~{end}")
    return _clean(pd.concat(out).drop_duplicates("trade_time"))
