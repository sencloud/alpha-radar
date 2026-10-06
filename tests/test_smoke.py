"""离线冒烟测试：不访问网络，用合成数据验证引擎契约。"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from alpharadar.engine import Backtester
from alpharadar.indicators import add_indicators
from alpharadar.metrics import summarize
from alpharadar.strategies import get, list_strategies
from alpharadar.universe import Instrument, resolve


def synth(n_days: int = 80, per_day: int = 48, seed: int = 7,
          drift_switch: int = 20) -> pd.DataFrame:
    """合成日盘 5 分钟 K：分段趋势 + 噪声，足够让趋势策略有信号。"""
    rng = np.random.default_rng(seed)
    rows = []
    price = 9000.0
    for d in range(n_days):
        day = pd.Timestamp("2023-01-02") + pd.Timedelta(days=d)
        if day.weekday() >= 5:
            continue
        drift = 4.0 if (d // drift_switch) % 2 == 0 else -4.0
        for b in range(per_day):
            step = drift + rng.normal(0, 6)
            o = price
            c = price + step
            hi = max(o, c) + abs(rng.normal(0, 3))
            lo = min(o, c) - abs(rng.normal(0, 3))
            t = day + pd.Timedelta(hours=9, minutes=5 * (b + 1))
            rows.append({"trade_time": t, "sdate": day.strftime("%Y%m%d"),
                         "open": o, "high": hi, "low": lo, "close": c,
                         "vol": float(rng.integers(500, 5000))})
            price = c
    return pd.DataFrame(rows)


def with_signals(bars: pd.DataFrame, atr_key: float = 3.0) -> pd.DataFrame:
    """用快慢均线翻转当信号，止损 = 收盘 − k×ATR（结构止损）。"""
    df = add_indicators(bars, {"atr_n": 14, "rsi_n": 14, "vol_n": 20, "atr_ma_n": 50})
    c = df["close"]
    fast = c.ewm(span=12, adjust=False).mean()
    slow = c.ewm(span=48, adjust=False).mean()
    up = (fast > slow).to_numpy()
    sig = np.zeros(len(df), dtype=np.int8)
    sig[1:] = np.where(up[1:] != up[:-1], np.where(up[1:], 1, -1), 0)
    atr = df["atr"].to_numpy(float)
    stop = np.where(sig == 1, c.to_numpy() - atr_key * atr,
                    np.where(sig == -1, c.to_numpy() + atr_key * atr, np.nan))
    df["sig"], df["stop_px"] = sig, stop
    df["st_stop"] = stop
    df["sig_tag"] = np.where(sig > 0, "多", np.where(sig < 0, "空", ""))
    return df


PARAMS = {"slippage_ticks": 1, "stop_mode": "struct", "stop_buf_atr": 0.5,
          "stop_min_points": 0.0, "stop_max_atr": 8.0, "partial_tps": (),
          "tgt_atr": 2.5, "use_target": 0, "trail_stop": 1, "max_hold_bars": 10000,
          "no_entry_before": "0910", "no_entry_after": "1430", "allow_night": 0,
          "cooldown_bars": 0, "max_entries_per_day": 99, "lots": 1,
          "breakeven_points": 0.0, "allow_overnight": 0}


def test_engine_balances():
    """期末权益 − 初始资金 必须等于逐笔净利之和。"""
    p = dict(PARAMS)
    bars = with_signals(synth())
    res = Backtester(bars, resolve("P.DCE"), p, capital=100_000).run()
    assert not res.trades.empty
    total = res.trades["净利"].sum()
    # 逐笔净利在报表里保留两位小数，允许舍入差
    assert abs(res.equity["equity"].iloc[-1] - 100_000 - total) < 0.01 * len(res.trades) + 1e-6
    assert res.equity["equity"].notna().all()


def test_costs_are_charged():
    bars = with_signals(synth())
    res = Backtester(bars, resolve("P.DCE"), dict(PARAMS), 100_000).run()
    assert (res.trades["费用"] > 0).all()


def test_t_plus_1_blocks_same_day_exit():
    """A 股当日买入当日不可卖：成交日期必须晚于开仓日。"""
    bars = with_signals(synth())
    inst = resolve("600519.SH")
    res = Backtester(bars, inst, dict(PARAMS), 100_000).run()
    if res.trades.empty:
        pytest.skip("合成数据未触发股票信号")
    opened = pd.to_datetime(res.trades["开仓时间"]).dt.strftime("%Y%m%d")
    assert (res.trades["日期"].astype(str) > opened).all()


def test_metrics_keys():
    bars = with_signals(synth())
    res = Backtester(bars, resolve("P.DCE"), dict(PARAMS), 100_000).run()
    s = summarize(res)
    for k in ("笔数", "胜率", "PF", "均点", "合计元", "最大回撤", "正年数"):
        assert k in s
    assert s["笔数"] == len(res.trades)


def test_all_strategies_produce_signal_columns():
    """所有内置策略在合成数据上都要能产出引擎需要的列。"""
    bars = add_indicators(synth(), {"atr_n": 14, "rsi_n": 14, "vol_n": 20,
                                    "atr_ma_n": 50})
    for strat in list_strategies():
        p = dict(PARAMS)
        p.update(strat.defaults)
        out = strat.fn(bars.copy(), p)
        for col in ("sig", "stop_px", "st_stop", "sig_tag"):
            assert col in out.columns, f"{strat.key} 缺少 {col}"
        assert len(out) == len(bars)
        assert set(np.unique(out["sig"])) <= {-1, 0, 1}


def test_regime_gate_silences_signals():
    """体制闸门开启且阈值极高时，信号应被全部过滤掉。"""
    bars = with_signals(synth())
    p = dict(PARAMS)
    p.update({"use_regime": 1, "er_min": 0.99, "atr_ratio_min": 99.0})
    df = add_indicators(bars, {"atr_n": 14, "rsi_n": 14, "vol_n": 20, "atr_ma_n": 50})
    from alpharadar.indicators import add_regime
    df = add_regime(df, p)
    res = Backtester(df, resolve("P.DCE"), p, 100_000).run()
    assert res.trades.empty


def test_instrument_defaults():
    assert resolve("P.DCE").market == "futures"
    assert resolve("600519.SH").t_plus_1 is True
    assert Instrument("X.SHF", "x", "futures", 10, 1.0).t_plus_1 is False
    assert get("utbot").key == "utbot"


# ==================== 结果库 / 调度器 ====================
def test_store_roundtrip(tmp_path):
    from alpharadar import store

    db = tmp_path / "t.db"
    store.init(db)
    rid = store.start_run("cycle", "test", path=db)
    store.finish_run(rid, "ok", n_ok=2, n_err=0, path=db)

    base = {"run_id": rid, "ts": "2026-10-06T12:00:00", "symbol": "P.DCE",
            "name": "棕榈油", "market": "futures", "strategy": "utbot",
            "freq": "5min", "start": "20220101", "end": "20261006",
            "trades": 100, "win_rate": 0.42, "pf": 1.05, "avg_points": 1.06,
            "total_pnl": 11227.0, "max_dd": -25076.0, "ret_dd": 0.45,
            "pos_years": 2, "years": 5, "status": "ok", "error": "", "report": "r.html"}
    store.add_result(base, path=db)
    store.add_result({**base, "ts": "2026-10-07T12:00:00", "pf": 1.20}, path=db)

    rows = store.latest_results(path=db)
    assert len(rows) == 1                      # 同 (品种,策略,周期) 只留最新
    assert rows[0]["pf"] == 1.20
    assert store.symbols_in_results(path=db)[0]["symbol"] == "P.DCE"
    assert len(store.history("P.DCE", "utbot", "5min", path=db)) == 2
    assert store.last_ok("P.DCE", "utbot", "5min", path=db)["status"] == "ok"
    assert store.recent_runs(5, path=db)[0]["n_ok"] == 2


def test_store_state_roundtrip(tmp_path):
    from alpharadar import store

    db = tmp_path / "t.db"
    store.init(db)
    store.set_state("harvest", {"total": 319, "new": 12}, path=db)
    got = store.get_state("harvest", path=db)
    assert got["value"]["new"] == 12 and got["ts"]
    assert store.get_state("missing", {"x": 1}, path=db) == {"x": 1}


def test_scheduler_cells_and_universe():
    from alpharadar import scheduler

    cfg = {"start": "20240101", "max_age_days": 7,
           "strategies": ["utbot", "orb"],
           "futures": {"freqs": ["5min", "1d"], "symbols": ["P.DCE", "Y.DCE"]},
           "stocks": {"freqs": ["1d"], "symbols": ["600519.SH"]}}
    cells = scheduler.build_cells(cfg)
    assert len(cells) == (2 * 2 + 1) * 2      # (品种×周期) × 策略
    assert {c["market"] for c in cells} == {"futures", "stocks"}
