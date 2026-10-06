"""Tushare 数据客户端：限流、重试、增量缓存。

缓存策略（沿用实战验证过的做法）：
- 每个合约/品种一个 CSV + 一个 .cover.json 记录已知覆盖区间；
- 请求区间被覆盖则直接切，缺头补头、缺尾补尾，重复回测零请求；
- 空缺口（节假日/未上市）也记入覆盖，避免反复探测。
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

import pandas as pd

from .config import CACHE_DIR, load_env

# Tushare 分钟线单次上限 8000 行；按周期换算分块天数
MIN_CHUNK_DAYS = {"1min": 20, "5min": 45, "15min": 120, "30min": 240, "60min": 240}
FREQS = tuple(MIN_CHUNK_DAYS) + ("1d",)


class TushareClient:
    def __init__(self, token: str | None = None, cache_dir: Path | None = None,
                 interval: float = 0.45):
        load_env()
        token = token or os.environ.get("TUSHARE_TOKEN")
        if not token:
            raise SystemExit("未找到 TUSHARE_TOKEN：请复制 .env.example 为 .env 并填入 token")
        import tushare  # 延迟导入，未安装时给出明确提示
        self.pro = tushare.pro_api(token)
        self.cache = Path(cache_dir or CACHE_DIR)
        self.cache.mkdir(parents=True, exist_ok=True)
        self.interval = interval
        self._last = 0.0

    # ---------- 基础 ----------
    def _throttle(self) -> None:
        gap = time.time() - self._last
        if gap < self.interval:
            time.sleep(self.interval - gap)
        self._last = time.time()

    def call(self, name: str, retries: int = 2, **kw) -> pd.DataFrame | None:
        """带重试的接口调用；限流时 tushare 常返回空表，因此空结果也重试。"""
        fn = getattr(self.pro, name, None)
        if fn is None:
            raise AttributeError(f"当前 tushare 版本没有接口 {name}")
        last = None
        for i in range(retries + 1):
            self._throttle()
            try:
                last = fn(**kw)
            except Exception as exc:                     # 网络/权限错误
                if i == retries:
                    raise
                last = None
            if last is not None and not last.empty:
                return last
            time.sleep(self.interval * (i + 1))
        return last

    # ---------- 缓存 ----------
    def _cache_paths(self, key: str) -> tuple[Path, Path]:
        f = self.cache / f"{key}.csv"
        return f, self.cache / f"{key}.csv.cover.json"

    def _read_cache(self, key: str, time_col: str) -> tuple[pd.DataFrame | None, str | None, str | None]:
        f, c = self._cache_paths(key)
        if not f.exists():
            return None, None, None
        try:
            df = pd.read_csv(f, dtype={time_col: str})
        except Exception:
            return None, None, None
        try:
            cov = json.loads(c.read_text(encoding="utf-8"))
            return df, cov.get("min"), cov.get("max")
        except Exception:
            if df.empty:
                return None, None, None
            return df, str(df[time_col].min()), str(df[time_col].max())

    def _write_cache(self, key: str, df: pd.DataFrame, dmin: str, dmax: str) -> None:
        f, c = self._cache_paths(key)
        df.to_csv(f, index=False)
        c.write_text(json.dumps({"min": dmin, "max": dmax}), encoding="utf-8")

    # ---------- 期货 ----------
    def fut_mapping(self, symbol: str, start: str, end: str) -> pd.DataFrame:
        """主力连续 -> 当月主力合约的逐日映射（区间内）。"""
        key = f"{symbol}_mapping"
        old, dmin, dmax = self._read_cache(key, "trade_date")
        if old is not None and dmin and dmax and dmin <= start and dmax >= end:
            m = (old["trade_date"] >= start) & (old["trade_date"] <= end)
            return old.loc[m].reset_index(drop=True)
        rows: list[pd.DataFrame] = []
        cur, final = pd.Timestamp(start), pd.Timestamp(end)
        while cur <= final:
            stop = min(cur + pd.Timedelta(days=1000), final)
            df = self.call("fut_mapping", ts_code=symbol,
                           start_date=cur.strftime("%Y%m%d"),
                           end_date=stop.strftime("%Y%m%d"))
            if df is not None and not df.empty:
                rows.append(df[["trade_date", "mapping_ts_code"]])
            cur = stop + pd.Timedelta(days=1)
        if not rows:
            raise RuntimeError(f"未取到主力映射：{symbol} {start}~{end}")
        out = (pd.concat(rows).drop_duplicates("trade_date")
               .sort_values("trade_date").reset_index(drop=True))
        self._write_cache(key, out, start, end)
        m = (out["trade_date"] >= start) & (out["trade_date"] <= end)
        return out.loc[m].reset_index(drop=True)

    def fut_daily(self, code: str, start: str, end: str) -> pd.DataFrame:
        return self._daily_like("fut_daily", code, start, end, "trade_date")

    def fut_minutes(self, code: str, freq: str, start: str, end: str) -> pd.DataFrame:
        return self._minutes_like("ft_mins", code, freq, start, end)

    # ---------- A 股 ----------
    def stk_daily(self, code: str, start: str, end: str) -> pd.DataFrame:
        return self._daily_like("daily", code, start, end, "trade_date")

    def stk_minutes(self, code: str, freq: str, start: str, end: str) -> pd.DataFrame:
        """A 股分钟线（stk_mins，需要相应权限）。"""
        return self._minutes_like("stk_mins", code, freq, start, end)

    # ---------- 通用实现 ----------
    def _daily_like(self, api: str, code: str, start: str, end: str,
                    time_col: str) -> pd.DataFrame:
        key = f"{code}_{api}"
        old, dmin, dmax = self._read_cache(key, time_col)
        if old is not None and dmin and dmax and dmin <= start and dmax >= end:
            return self._slice(old, time_col, start, end)
        rows = [old] if old is not None else []
        new_min, new_max = dmin or start, dmax or end
        spans = []
        if old is None or not dmin:
            spans.append((start, end))
        else:
            if start < dmin:
                spans.append((start, self._prev(dmin)))
            if end > dmax:
                spans.append((self._next(dmax), end))
        for s, e in spans:
            cur, final = pd.Timestamp(s), pd.Timestamp(e)
            while cur <= final:
                stop = min(cur + pd.Timedelta(days=1500), final)
                df = self.call(api, ts_code=code, start_date=cur.strftime("%Y%m%d"),
                               end_date=stop.strftime("%Y%m%d"))
                if df is not None and not df.empty:
                    rows.append(df)
                cur = stop + pd.Timedelta(days=1)
        if not rows:
            raise RuntimeError(f"未取到日线：{code} {start}~{end}")
        out = (pd.concat(rows).drop_duplicates(time_col)
               .sort_values(time_col).reset_index(drop=True))
        self._write_cache(key, out, min(new_min, start), max(new_max, end))
        return self._slice(out, time_col, start, end)

    def _minutes_like(self, api: str, code: str, freq: str, start: str,
                      end: str) -> pd.DataFrame:
        if freq not in MIN_CHUNK_DAYS:
            raise ValueError(f"freq 仅支持 {tuple(MIN_CHUNK_DAYS)}：{freq}")
        key = f"{code}_{api}_{freq}"
        old, dmin, dmax = self._read_cache(key, "trade_time")
        if old is not None and "trade_time" in old.columns:
            old["trade_time"] = pd.to_datetime(old["trade_time"])   # CSV 读回是字符串
        if old is not None and dmin and dmax and dmin <= start and dmax >= end:
            return self._slice(old, "trade_time", start, end)
        rows = [old] if old is not None else []
        new_min, new_max = dmin or start, dmax or end
        spans = []
        if old is None or not dmin:
            spans.append((start, end))
        else:
            if start < dmin:
                spans.append((start, self._prev(dmin)))
            if end > dmax:
                spans.append((self._next(dmax), end))
        step = MIN_CHUNK_DAYS[freq]
        for s, e in spans:
            cur, final = pd.Timestamp(s), pd.Timestamp(e)
            while cur <= final:
                stop = min(cur + pd.Timedelta(days=step - 1), final)
                df = self.call(api, ts_code=code, freq=freq,
                               start_date=f"{cur:%Y-%m-%d} 00:00:00",
                               end_date=f"{stop:%Y-%m-%d} 23:59:59")
                if df is not None and not df.empty:
                    rows.append(df)
                cur = stop + pd.Timedelta(days=1)
        if not rows:
            raise RuntimeError(f"未取到 {freq} 分钟线：{code} {start}~{end}"
                               f"（该周期可能未开通权限）")
        out = pd.concat(rows)
        out["trade_time"] = pd.to_datetime(out["trade_time"])
        out = (out.drop_duplicates("trade_time").sort_values("trade_time")
               .reset_index(drop=True))
        self._write_cache(key, out, min(new_min, start), max(new_max, end))
        return self._slice(out, "trade_time", start, end)

    @staticmethod
    def _slice(df: pd.DataFrame, col: str, start: str, end: str) -> pd.DataFrame:
        if col == "trade_date":
            m = (df[col].astype(str) >= start) & (df[col].astype(str) <= end)
        else:
            t = pd.to_datetime(df[col])
            m = (t >= f"{start[:4]}-{start[4:6]}-{start[6:]}") & \
                (t <= f"{end[:4]}-{end[4:6]}-{end[6:]} 23:59:59")
        return df.loc[m].sort_values(col).reset_index(drop=True)

    @staticmethod
    def _prev(d: str) -> str:
        return (pd.Timestamp(d) - pd.Timedelta(days=1)).strftime("%Y%m%d")

    @staticmethod
    def _next(d: str) -> str:
        return (pd.Timestamp(d) + pd.Timedelta(days=1)).strftime("%Y%m%d")
