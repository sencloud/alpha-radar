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
    # orb 只支持日内周期：futures 5min 两个策略、1d 只剩 utbot；stocks 1d 只剩 utbot
    # = 2 × (2 + 1) + 1 = 7
    assert len(cells) == 7
    assert {c["market"] for c in cells} == {"futures", "stocks"}
    assert not any(c["strategy"] == "orb" and c["freq"] == "1d" for c in cells)
    assert any(c["strategy"] == "orb" and c["freq"] == "5min" for c in cells)


def _fake_scripts(n=5):
    from alpharadar.harvest import Script
    return [Script(id=f"PUB;hash{i:03d}", slug=f"slug{i}", title=f"Strategy {i}",
                   author="alice" if i % 2 else "bob", agree=100 - i,
                   kind="study" if i % 2 else "strategy", access="open_no_auth",
                   lines=10 * i, file=f"slug{i}_Strategy_{i}.pine")
            for i in range(1, n + 1)]


def test_script_listing_and_lookup(tmp_path):
    from alpharadar import store

    db = tmp_path / "t.db"
    store.init(db)
    total, new = store.upsert_scripts(_fake_scripts(5), path=db)
    assert (total, new) == (5, 5)
    assert store.upsert_scripts(_fake_scripts(5), path=db)[1] == 0   # 幂等

    rows, cnt = store.list_scripts(path=db)
    assert cnt == 5 and rows[0]["agree"] == 99                      # 默认按点赞
    rows, cnt = store.list_scripts(kind="strategy", path=db)
    assert cnt == 2 and all(r["kind"] == "strategy" for r in rows)
    rows, cnt = store.list_scripts(q="alice", path=db)
    assert cnt == 3
    page2, _ = store.list_scripts(limit=2, offset=2, path=db)
    assert len(page2) == 2
    assert store.get_script("hash003", path=db)["title"] == "Strategy 3"   # 按哈希查
    assert store.get_script("PUB;hash003", path=db)["title"] == "Strategy 3"
    assert store.get_script("nope", path=db) is None
    assert store.script_kinds(path=db) == ["study", "strategy"]


def test_pine_highlight_escapes_html():
    """语料库内容是不可信输入：渲染源码时必须转义，不能原样注入 HTML。"""
    from alpharadar.web import _highlight_pine

    out = _highlight_pine('<script>alert(1)</script>\n// comment\nta.sma(close, 20)')
    assert "<script>" not in out
    assert "&lt;script&gt;" in out
    assert "c-com" in out and "c-ns" in out
    assert out.count('class="ln"') == 3          # 行号包裹


def test_extract_pine_blocks():
    """论坛正文里的代码块抽取（含噪声过滤与 HTML 反转义）。"""
    from alpharadar.harvest import extract_pine_blocks

    body = ('<p>my idea</p>'
            '<pre class="language-pine">//@version=5\n'
            'indicator("demo")\nplot(close, color=color.red)</pre>'
            '<pre>this is just a paragraph, not code at all</pre>'
            '<code>ta.sma(close, 20)</code>')
    blocks = extract_pine_blocks(body)
    assert len(blocks) == 1
    assert "//@version=5" in blocks[0] and "indicator(" in blocks[0]
    # 没写 Pine 提示词的代码块不算
    assert extract_pine_blocks("<pre>" + "x" * 100 + "</pre>") == []
    # HTML 实体要还原
    ent = extract_pine_blocks("<pre>//@version=5\nindicator('x')\n"
                              "plot(a &amp;&amp; b, title=&#39;x&#39;)\nplot(close)</pre>")
    assert ent and "&&" in ent[0] and "'x'" in ent[0]


def test_feed_record_to_script():
    """脚本流/论坛记录 -> 语料库条目（闭源或缺 id 的被拒）。"""
    from alpharadar.harvest import _feed_record_to_script

    rec = {"script_id_part": "PUB;abc", "image_url": "xyz", "name": "Demo",
           "user": {"username": "alice"}, "likes_count": 7, "is_picked": True,
           "script_type": "strategy"}
    s = _feed_record_to_script(rec)
    assert s and s.id == "PUB;abc" and s.kind == "strategy" and s.agree == 7
    assert s.is_open is False                     # access 未知时不算开源
    assert _feed_record_to_script({"name": "no id"}) is None
    ind = _feed_record_to_script({**rec, "script_type": "indicator"})
    assert ind.kind == "study"                    # 非 strategy/library 归为 study


def test_harvest_channels_are_configurable():
    """channels 参数要能关掉某些通道（离线测试用 search 之外的不联网）。"""
    import inspect
    from alpharadar.harvest import harvest

    sig = inspect.signature(harvest)
    assert sig.parameters["channels"].default == ("search", "feed", "forum")
    assert "stats" in sig.parameters


# ==================== 全市场任务队列 ====================
def test_futures_product_parse():
    """RB2601.SHF -> RB.SHF；不同交易所后缀都要保留。"""
    from alpharadar.instruments import _product

    assert _product("RB2601.SHF") == "RB.SHF"
    assert _product("TA1001.ZCE") == "TA.ZCE"
    assert _product("IF1906.CFX") == "IF.CFX"
    assert _product("SC2508.INE") == "SC.INE"


def test_task_queue_lifecycle(tmp_path):
    """队列：生成 -> 领取 -> 完成排期；策略周期白名单要参与过滤。"""
    from alpharadar import store

    db = tmp_path / "t.db"
    store.init(db)
    n = store.sync_instruments([
        {"symbol": "P.DCE", "name": "棕榈油", "market": "futures",
         "freqs": ["5min", "1d"]},
        {"symbol": "600519.SH", "name": "贵州茅台", "market": "stocks",
         "freqs": ["1d"]},
    ], path=db)
    assert n == 2
    # utbot 不限周期；orb 只做日内
    added, total = store.sync_tasks({"utbot": (), "orb": ("1min", "5min")}, path=db)
    # P.DCE: 5min×2 + 1d×1 = 3；600519: 1d×1 = 1
    assert (added, total) == (4, 4)
    # 幂等
    assert store.sync_tasks({"utbot": (), "orb": ("1min", "5min")}, path=db)[0] == 0

    due = store.claim_tasks(10, path=db)
    assert len(due) == 4 and all(d["market"] for d in due)
    assert not any(d["strategy"] == "orb" and d["freq"] == "1d" for d in due)

    store.finish_task(due[0]["id"], "ok", requeue_days=7, path=db)
    store.finish_task(due[1]["id"], "error", path=db, err="boom")
    st = store.task_stats(path=db)
    assert st["total"] == 4 and st["ok"] == 1 and st["err"] == 1
    assert st["due"] == 2                     # 成功/失败都排到了未来
    # 失败的下次到期时间应该在成功之前（6 小时 vs 7 天）
    rows = {r["symbol"] + r["strategy"] + r["freq"]: r
            for r in store.claim_tasks(0, path=db)} if False else None
    with store.connect(db) as con:
        a = con.execute("SELECT next_due FROM tasks WHERE id=?", (due[0]["id"],)).fetchone()[0]
        b = con.execute("SELECT next_due FROM tasks WHERE id=?", (due[1]["id"],)).fetchone()[0]
    assert b < a


def test_task_stats_shape(tmp_path):
    from alpharadar import store

    db = tmp_path / "t.db"
    store.init(db)
    store.sync_instruments([{"symbol": "P.DCE", "name": "p", "market": "futures",
                             "freqs": ["1d"]}], path=db)
    store.sync_tasks({"utbot": (), "orbtest": ()}, path=db)
    st = store.task_stats(path=db)
    assert st["total"] == 2 and st["due"] == 2
    assert st["by_market"] == {"futures": 2}
    assert st["by_freq"] == {"1d": 2}


def test_cache_eviction_keeps_mapping(tmp_path):
    """磁盘回收：删本进程写过的合约行情，但保留主力映射表（小且每个任务都要用）。"""
    from alpharadar.tushare_client import TushareClient

    cli = TushareClient.__new__(TushareClient)      # 不触发 token 校验
    cli.cache = tmp_path
    cli.written = set()
    names = ["P2505.DCE_ft_mins_5min.csv", "P2505.DCE_ft_mins_5min.csv.cover.json",
             "P2505.DCE_fut_daily.csv", "P.DCE_mapping.csv", "P.DCE_mapping.csv.cover.json"]
    for n in names:
        (tmp_path / n).write_text("x" * 100)
    cli.written = set(names)

    n, mb = cli.evict_written()
    left = {p.name for p in tmp_path.glob("*")}
    assert n == 3 and mb > 0
    assert left == {"P.DCE_mapping.csv", "P.DCE_mapping.csv.cover.json"}
    assert cli.written == set()                     # 记录要清空，避免误删下一批


def test_cache_lru_eviction(tmp_path):
    """全局兜底：超限时按 mtime 从旧到新删，映射表依旧保留。"""
    import os
    import time as _t
    from alpharadar.tushare_client import TushareClient

    cli = TushareClient.__new__(TushareClient)
    cli.cache = tmp_path
    cli.written = set()
    big = 400_000
    for i, name in enumerate(["a_ft_mins_1min.csv", "b_ft_mins_1min.csv",
                              "c_ft_mins_1min.csv", "RB.DCE_mapping.csv"]):
        p = tmp_path / name
        p.write_text("x" * big)
        os.utime(p, (_t.time() - 1000 + i * 10, _t.time() - 1000 + i * 10))
    total_before = sum(p.stat().st_size for p in tmp_path.glob("*"))
    n, _ = cli.evict_lru(target_gb=total_before / 1e9 * 0.5)
    left = {p.name for p in tmp_path.glob("*")}
    assert n >= 1 and "RB.DCE_mapping.csv" in left
    assert "a_ft_mins_1min.csv" not in left         # 最旧的先删


# ==================== Pine 移植流水线 ====================
def test_porting_classify():
    """分诊要能识别指标族、原生策略、重绘风险，并据此打分排序。"""
    from alpharadar.porting.triage import classify

    rsi_study = '//@version=5\nindicator("RSI")\nr = ta.rsi(close, 14)\nplot(r)'
    c = classify(rsi_study, kind="study", agree=1000)
    assert c["family"] == "oscillator" and not c["is_strategy"]
    assert c["risks"] == []

    st = ('//@version=5\nstrategy("ST")\n[_, d] = ta.supertrend(3, 10)\n'
          'if d < 0\n    strategy.entry("L", strategy.long)')
    c2 = classify(st, kind="strategy")
    assert c2["is_strategy"] and c2["family"] == "trend"
    assert c2["score"] > c["score"]                 # 原生策略应排更前

    risky = '//@version=5\nx = request.security(syminfo.tickerid, "D", close, lookahead=barmerge.lookahead_on)'
    c3 = classify(risky, kind="study")
    assert c3["risks"] and c3["score"] < c["score"]  # 重绘风险要扣分

    c4 = classify('//@version=5\nlibrary("x")', kind="library")
    assert c4["family"] == "unknown"


def test_port_stats_shape(tmp_path):
    from alpharadar import store
    from alpharadar.porting.triage import port_stats

    db = tmp_path / "t.db"
    store.init(db)
    with store.connect(db) as con:
        con.execute("INSERT INTO ports(sid,title,kind,family,status,score) "
                    "VALUES('PUB;a','A','study','trend','pending',50)")
        con.execute("INSERT INTO ports(sid,title,kind,family,status,score) "
                    "VALUES('PUB;b','B','study','bands','verified',40)")
    # port_stats 走默认库，这里只验证 SQL 结构正确
    st = port_stats()
    assert set(st) == {"by_status", "by_family", "top"}


def test_verify_gates_catch_lookahead():
    """最关键的一道闸门：用了未来数据必须被抓出来。"""
    import numpy as np
    from alpharadar.porting.verify import check_no_lookahead

    bad = '//@version=5'
    # 伪造一个「偷看未来」的策略：用 shift(-1)
    def _lookahead(df, p):
        c = df["close"].astype(float)
        sig = np.zeros(len(df), dtype=np.int8)
        fut = c.shift(-1).to_numpy()                 # 未来数据！
        cur = c.to_numpy()
        sig[:-1] = np.where(fut[:-1] > cur[:-1], 1, -1)
        from alpharadar.strategies.base import signal_frame
        return signal_frame(df, sig, min_bars=0)

    from alpharadar.strategies.base import REGISTRY, Strategy
    REGISTRY["_test_lookahead"] = Strategy("_test_lookahead", "bad", _lookahead)
    try:
        from alpharadar.porting.verify import synth_bars
        errs = check_no_lookahead("_test_lookahead", synth_bars(1200), {})
        assert errs and "未来数据" in errs[0]
    finally:
        REGISTRY.pop("_test_lookahead", None)


def test_verify_gates_pass_for_existing():
    """现有 7 个策略在合成数据上至少要通过「无未来函数 + 可复现」两道闸门。"""
    from alpharadar.porting.verify import synth_bars, verify_strategy

    bars = synth_bars(1500)
    for key in ("utbot", "supertrend", "chandelier", "ema_cross", "orb",
                "false_breakout"):
        r = verify_strategy(key, bars=bars)
        fatal = [e for e in r["errors"] if "未来数据" in e or "不可复现" in e
                 or "取值越界" in e]
        assert not fatal, f"{key} 出现致命问题：{fatal}"


def test_scaffold_worksheet_contains_key_parts(tmp_path):
    """工单必须带齐：源码头、建议包装器、Python 模板、硬要求。"""
    import alpharadar.porting.scaffold as sc
    from alpharadar import store
    from alpharadar.config import CORPUS_DIR

    src_dir = CORPUS_DIR / "sources"
    src_dir.mkdir(parents=True, exist_ok=True)
    f = src_dir / "_test_ws.pine"
    f.write_text('//@version=5\nindicator("T")\nr = ta.rsi(close, 14)\nplot(r)',
                 encoding="utf-8")
    store.init()
    store.upsert_scripts([__import__("alpharadar.harvest", fromlist=["Script"]).Script(
        id="PUB;wstest", slug="wstest", title="RSI Demo", author="alice",
        agree=10, kind="study", access="open_no_auth", lines=4, file="_test_ws.pine")])
    with store.connect() as con:
        con.execute("INSERT OR REPLACE INTO ports(sid,title,kind,family,status,score,risk)"
                    " VALUES('PUB;wstest','RSI Demo','study','oscillator','pending',40,'')")
    text = sc.make_worksheet("PUB;wstest")
    for part in ("移植工单", "建议包装器", "oscillator", "@register", "原始 Pine 源码",
                 "verify", "硬要求"):
        assert part in text, f"工单缺少 {part}"
    f.unlink()


# ==================== 自动移植（LLM 生成代码的安全边界） ====================
def test_extract_code_strips_fence():
    from alpharadar.porting.autoport import _extract_code

    txt = "说明\n```python\n@register('x','y')\ndef _x(df,p): pass\n```\n尾巴"
    assert _extract_code(txt).startswith("@register")
    try:
        _extract_code("这里没有代码")
        raise AssertionError("应该抛错")
    except RuntimeError as exc:
        assert "@register" in str(exc)


def test_autoport_rolls_back_on_gate_failure(monkeypatch):
    """最关键的安全边界：闸门不过时必须回滚，绝不能在 generated.py 留下坏代码。"""
    import alpharadar.porting.autoport as ap

    before = ap.GEN.read_text(encoding="utf-8") if ap.GEN.exists() else None
    # 一个偷看未来的实现：闸门 1 必须拦住它
    bad = ('@register("tv_zzzzzzzzzz", "bad", defaults={})\n'
           'def _tv_zzzzzzzzzz(df, p):\n'
           '    c = df["close"].astype(float)\n'
           '    sig = np.zeros(len(df), dtype=np.int8)\n'
           '    fut = c.shift(-1).to_numpy()\n'
           '    cur = c.to_numpy()\n'
           '    sig[:-1] = np.where(fut[:-1] > cur[:-1], 1, -1)\n'
           '    return signal_frame(df, sig, min_bars=0)\n')
    monkeypatch.setattr(ap, "_llm", lambda *a, **k: f"```python\n{bad}```")
    monkeypatch.setattr(ap.scaffold, "make_worksheet", lambda sid: "fake worksheet")

    res = ap.port_one({"sid": "PUB;zzzzzzzzzz", "title": "t", "family": "trend",
                       "score": 1}, max_tries=1, verbose=lambda *a: None)
    assert not res["ok"]
    assert "未来数据" in res["errors"][0]
    now = ap.GEN.read_text(encoding="utf-8") if ap.GEN.exists() else None
    # 关键性质：被拒的代码不能留在文件里（允许文件被创建成只有文件头）
    assert not now or ("tv_zzzzzzzzzz" not in now and "shift(-1)" not in now)
    assert res["key"] not in __import__("alpharadar.strategies",
                                        fromlist=["REGISTRY"]).REGISTRY


def test_autoport_accepts_good_code(monkeypatch):
    """一个干净实现应当通过闸门并进入注册表。"""
    import alpharadar.porting.autoport as ap
    from alpharadar.strategies import REGISTRY

    before = ap.GEN.read_text(encoding="utf-8") if ap.GEN.exists() else None
    key = "tv_goodgood00"
    good = (f'@register("{key}", "good", defaults={{"n": 20}})\n'
            f'def _{key}(df, p):\n'
            '    c = df["close"].astype(float)\n'
            '    m = c.rolling(int(p["n"]), min_periods=int(p["n"])).mean()\n'
            '    up = (c > m).to_numpy()\n'
            '    sig = np.zeros(len(df), dtype=np.int8)\n'
            '    sig[1:] = np.where(up[1:] != up[:-1], np.where(up[1:], 1, -1), 0)\n'
            '    stop = np.where(sig == 1, c.to_numpy() - df["atr"].to_numpy(),\n'
            '                    np.where(sig == -1, c.to_numpy() + df["atr"].to_numpy(), np.nan))\n'
            '    return signal_frame(df, sig, stop=stop, st_stop=stop)\n')
    monkeypatch.setattr(ap, "_llm", lambda *a, **k: f"```python\n{good}```")
    monkeypatch.setattr(ap.scaffold, "make_worksheet", lambda sid: "fake worksheet")

    try:
        res = ap.port_one({"sid": "PUB;goodgood00", "title": "t",
                           "family": "trend", "score": 1}, max_tries=1,
                          verbose=lambda *a: None)
        assert res["ok"] and res["key"] in REGISTRY
    finally:
        if before is None:                                   # 清理到测试前状态
            ap.GEN.unlink(missing_ok=True)
        else:
            ap.GEN.write_text(before, encoding="utf-8")
        ap._reload()
