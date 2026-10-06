"""移植校验：用确定性测试卡死「翻译错了」的高代价失败模式。

为什么需要它：Pine 与 Python 的语义差异（序列索引、var 状态、逐 bar 执行）
写错了不会报错，只会安静地产出一条假曲线。人工逐行核对不可扩展，
所以把最容易出错、代价最高的一类问题做成机械测试。

四道闸门（任一不过 -> 拒绝入库，不给回测队列）：
  1. 无未来函数  —— 在 bars[:k] 上算出的信号必须与在全量上算出的前 k 个信号完全一致。
     这是决定性的：任何用到未来数据的实现都会在这里暴露。
  2. 信号健全    —— 取值只能是 -1/0/1；不能全 0；不能几乎每根都触发（退化成"永远在场"）；
     结构止损模式下非零信号必须有有限的止损价。
  3. 可复现      —— 同样输入跑两次结果完全一致（排除随机数/时间依赖）。
  4. 暖机纪律    —— 指标预热期内不得出信号（前 warmup 根必须为 0）。

用法：
    python -m alpharadar.porting.verify --strategy my_key
    python -m alpharadar.porting.verify --all
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime

import numpy as np
import pandas as pd

from .. import store
from ..config import DEFAULTS
from ..indicators import add_indicators
from ..strategies import REGISTRY, get
from ..strategies import list_strategies

# 与 signal_frame 的中央预热纪律保持同一来源，避免两边阈值不一致
WARMUP = int(DEFAULTS.get("min_bars", 60))


def synth_bars(n: int = 3000, seed: int = 20261006) -> pd.DataFrame:
    """确定性合成 K 线（校验用的压力测试数据）。

    覆盖面要求：让各类策略都有机会触发，否则「没有信号」无法区分是
    「移植写错了」还是「测试数据没覆盖」。所以刻意构造：
      - 分段趋势（趋势类、均线类）
      - 波动收缩/扩张（通道、布林、ATR 类）
      - 周期性急跌 + 放量（冰点反转、假突破类量价形态）
      - 区间震荡（区间突破、假突破）
    """
    rng = np.random.default_rng(seed)
    price = 100.0
    drift = 0.0
    vol = 1.0
    volume = 2000.0
    rows = []
    t = pd.Timestamp("2024-01-02 09:00")
    for i in range(n):
        if i % 250 == 0:                      # 每 250 根换一次体制
            regime = (i // 250) % 4
            drift = (0.25, -0.25, 0.0, 0.1)[regime]
            vol = (1.0, 1.3, 0.5, 1.8)[regime]
            volume = (2000.0, 2600.0, 1200.0, 3200.0)[regime]
        shock = 0.0
        if i % 250 == 120:                    # 急跌 + 放量：给量价形态制造机会
            shock = -6.0 * vol
            volume = 9000.0
        elif i % 250 == 121:                  # 次日放量反弹
            shock = 2.0 * vol
            volume = 7000.0
        step = rng.normal(drift, vol) + shock
        o = price
        c = price + step
        hi = max(o, c) + abs(rng.normal(0, vol * 0.5))
        lo = min(o, c) - abs(rng.normal(0, vol * 0.5))
        rows.append({"trade_time": t, "sdate": t.strftime("%Y%m%d"),
                     "open": o, "high": hi, "low": lo, "close": c,
                     "vol": float(max(1.0, rng.normal(volume, volume * 0.25)))})
        price = c
        volume = max(500.0, volume * 0.9 + 200.0)   # 冲击后回落到常态
        t += pd.Timedelta(minutes=5)
        if i % 48 == 47:                      # 换日
            t = (t.normalize() + pd.Timedelta(days=1)).replace(hour=9)
    return pd.DataFrame(rows)


def _signals(key: str, bars: pd.DataFrame, params: dict) -> pd.DataFrame:
    strat = get(key)
    p = {**params, **strat.defaults}
    df = add_indicators(bars, p)
    return strat.fn(df, p)


def check_no_lookahead(key: str, bars: pd.DataFrame, params: dict) -> list[str]:
    """闸门 1：截断测试。前 k 根的信号必须与全量结果的前 k 根完全一致。"""
    full = _signals(key, bars, params)
    errs = []
    n = len(bars)
    for k in (n // 4, n // 2, n - 400, n - 60):
        if k < WARMUP + 50:
            continue
        part = _signals(key, bars.iloc[:k].copy(), params)
        a = part["sig"].to_numpy()[:k]
        b = full["sig"].to_numpy()[:k]
        if not np.array_equal(a, b):
            diff = int((a != b).sum())
            first = int(np.argmax(a != b))
            errs.append(f"用到了未来数据：截断到 {k} 根时，前 {k} 根里有 {diff} 个信号"
                        f"与全量不一致（首个分歧在 {first}）")
    return errs


def check_signal_sanity(key: str, out: pd.DataFrame, params: dict) -> list[str]:
    """闸门 2：信号取值/频率/止损价健全性。"""
    errs = []
    sig = out["sig"].to_numpy()
    if not set(np.unique(sig)) <= {-1, 0, 1}:
        errs.append(f"sig 取值越界：{sorted(set(np.unique(sig)))}")
    if np.isnan(sig.astype(float)).any():
        errs.append("sig 含 NaN")
    nz = int((sig != 0).sum())
    if nz == 0:
        errs.append("没有任何信号（包装器可能选错，或阈值不可达）")
    elif nz > len(sig) * 0.5:
        errs.append(f"信号过密：{nz}/{len(sig)} 根都在触发，策略退化成「永远在场」")
    elif nz < 5:
        errs.append(f"信号过少（{nz} 个）：合成数据可能未覆盖该形态，"
                    f"请用真实数据复核，别直接入库")
    if params.get("stop_mode", "struct") == "struct" and not params.get("use_target") == 0:
        bad = (sig != 0) & ~np.isfinite(out["stop_px"].to_numpy())
        if bad.any():
            errs.append(f"{int(bad.sum())} 个信号缺少结构止损价")
    return errs


def check_warmup(key: str, out: pd.DataFrame) -> list[str]:
    """闸门 4：预热期不得出信号。"""
    early = out["sig"].to_numpy()[:WARMUP]
    return ([f"预热期（前 {WARMUP} 根）出现 {int((early != 0).sum())} 个信号"]
            if (early != 0).any() else [])


def verify_strategy(key: str, params: dict | None = None,
                    bars: pd.DataFrame | None = None) -> dict:
    """跑全部闸门，返回 {ok, errors, stats}。"""
    p = dict(params or {})
    b = bars if bars is not None else synth_bars()
    ok, errors = True, []
    try:
        out = _signals(key, b, p)
    except Exception as exc:
        return {"ok": False, "errors": [f"执行异常：{type(exc).__name__}: {exc}"],
                "stats": {}}
    try:
        again = _signals(key, b, p)
        if not out["sig"].equals(again["sig"]):
            errors.append("不可复现：两次运行信号不一致")
    except Exception as exc:
        errors.append(f"重复运行异常：{type(exc).__name__}: {exc}")
    errors += check_no_lookahead(key, b, p)
    errors += check_signal_sanity(key, out, p)
    errors += check_warmup(key, out)
    sig = out["sig"].to_numpy()
    return {"ok": not errors, "errors": errors,
            "stats": {"bars": len(b), "longs": int((sig == 1).sum()),
                      "shorts": int((sig == -1).sum())}}


def record(key: str, result: dict) -> None:
    """把校验结果写回移植台账。"""
    strat = get(key)
    sid = getattr(strat, "source_sid", "") or ""
    if not sid:
        return
    with store.connect() as con:
        con.execute(
            "UPDATE ports SET status=?, strategy_key=?, notes=?, updated_at=? "
            "WHERE sid=? OR sid LIKE ?",
            ("verified" if result["ok"] else "rejected", key,
             "; ".join(result["errors"])[:500],
             datetime.now().isoformat(timespec="seconds"), sid, f"%;{sid}"))


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Pine 移植校验")
    ap.add_argument("--strategy", help="只校验某个 key")
    ap.add_argument("--all", action="store_true", help="校验所有已注册策略")
    ap.add_argument("--record", action="store_true", help="把结果写回移植台账")
    ap.add_argument("--real", metavar="SYMBOL:FREQ",
                    help="用真实行情校验（如 P.DCE:5min）。形态类策略在合成数据上"
                         "可能信号过少，必须用真实数据复核。")
    args = ap.parse_args(argv)

    keys = [k for k in REGISTRY] if args.all else ([args.strategy] if args.strategy else [])
    if not keys:
        ap.error("需要 --strategy <key> 或 --all")
    if args.real:
        sym, _, fq = args.real.partition(":")
        fq = fq or "5min"
        from ..loaders import load_bars
        bars = load_bars(sym, fq, "20230101", verbose=False)
        print(f"[verify] 真实数据 {sym} {fq}：{len(bars):,} 根")
    else:
        bars = synth_bars()
    bad = 0
    for k in keys:
        r = verify_strategy(k, bars=bars)
        flag = "PASS" if r["ok"] else "FAIL"
        s = r["stats"]
        print(f"[{flag}] {k:<18} bars={s.get('bars', 0)} "
              f"longs={s.get('longs', 0)} shorts={s.get('shorts', 0)}")
        for e in r["errors"]:
            print(f"        ! {e}")
        if args.record:
            record(k, r)
        bad += 0 if r["ok"] else 1
    print(f"\n{len(keys) - bad}/{len(keys)} 通过")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
