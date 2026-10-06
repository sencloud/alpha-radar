"""策略协议与注册表。

约定：策略函数签名 `fn(df, params) -> df`，并在返回表里给出：
  sig      1 做多 / -1 做空 / 0 无信号（当根收盘确认）
  stop_px  结构止损价（stop_mode="struct" 时使用）
  st_stop  逐根跟踪止损价（trail_stop=1 时使用）
  sig_tag  信号标签，便于报告里分组
策略只负责「信号」，撮合、成本、时段、T+N、统计都由 engine 负责。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable

import numpy as np
import pandas as pd


@dataclass
class Strategy:
    key: str
    name: str
    fn: Callable[[pd.DataFrame, dict], pd.DataFrame]
    source: str = ""                     # 出处（作者 / 链接）
    license: str = ""                    # 原脚本许可
    notes: str = ""
    defaults: dict = field(default_factory=dict)   # 覆盖引擎默认参数
    freqs: tuple = ()                    # 适用周期白名单（空 = 不限）。
                                         # 日内形态配日线数据必然 0 笔，
                                         # scheduler.build_cells 会跳过这类组合
    source_sid: str = ""                 # 移植来源：语料库脚本 id（可追溯到原作者）


REGISTRY: dict[str, Strategy] = {}


def register(key: str, name: str, source: str = "", license: str = "",
             notes: str = "", defaults: dict | None = None, freqs: tuple = (),
             source_sid: str = ""):
    def deco(fn):
        REGISTRY[key] = Strategy(key=key, name=name, fn=fn, source=source,
                                 license=license, notes=notes,
                                 defaults=dict(defaults or {}), freqs=tuple(freqs),
                                 source_sid=source_sid)
        return fn
    return deco


def get(key: str) -> Strategy:
    if key not in REGISTRY:
        raise KeyError(f"未知策略 {key}；可用：{', '.join(sorted(REGISTRY))}")
    return REGISTRY[key]


def list_strategies() -> list[Strategy]:
    return [REGISTRY[k] for k in sorted(REGISTRY)]


def signal_frame(df: pd.DataFrame, sig: np.ndarray,
                 stop: np.ndarray | None = None,
                 st_stop: np.ndarray | None = None,
                 tag: np.ndarray | None = None,
                 min_bars: int = 60) -> pd.DataFrame:
    """把策略输出统一成引擎要求的列。

    min_bars：**中央预热纪律** —— 序列前 min_bars 根一律不出信号。
    指标在预热期未成形，此时出的信号是噪音；更重要的是，把它放在这里
    而不是每个策略里，新移植的策略自动获得这个保证（verify 会检查）。
    """
    out = df.copy()
    n = len(out)
    s = np.asarray(sig, dtype=np.int8).copy()
    if min_bars and n > min_bars:
        s[:min_bars] = 0
    out["sig"] = s
    out["stop_px"] = np.full(n, np.nan) if stop is None else np.asarray(stop, float)
    out["st_stop"] = (out["stop_px"].to_numpy() if st_stop is None
                      else np.asarray(st_stop, float))
    if tag is None:
        out["sig_tag"] = np.where(out["sig"] > 0, "多",
                                  np.where(out["sig"] < 0, "空", ""))
    else:
        out["sig_tag"] = np.asarray(tag, dtype=object)
    return out
