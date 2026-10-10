"""判定层 / 许可过滤 / 导出契约 / clean-room 策略 / 管理口令 的离线测试（全部合成数据）。"""

from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

import numpy as np
import pandas as pd
import pytest

from alpharadar import falsify, judge, licensing, store
from alpharadar.strategies import REGISTRY
from alpharadar.universe import resolve

CFG = judge.load_gates()
W = {"start": "20220101", "end": "20260930"}           # 完整年度 2022–2025


def M(**kw):
    base = {"trades": 500, "years": 4, "positive_years": 4, "total_pnl": 50_000.0,
            "max_dd": -10_000.0, "pnl_dd": None}
    base.update(kw)
    return base


GOOD_YEARS = [["2022", 10], ["2023", 10], ["2024", 10], ["2025", 10]]


# ==================== 判定层 ====================
def test_gates_config_is_versioned_and_complete():
    assert CFG["threshold_version"]
    assert tuple(CFG["order"]) == judge.GATE_ORDER
    g = CFG["gates"]
    assert g["sample"]["min_trades"] == 200 and g["sample"]["min_years"] == 3
    assert g["scale"]["pass_below"] == 0.25 and g["scale"]["fail_at"] == 0.40
    assert g["yearly"]["min_positive_ratio"] == 0.8
    assert g["drawdown"]["min_pnl_dd"] == 1.0


def test_all_pass_is_pending_never_tradable():
    r = judge.judge(M(), scale=0.10, yearly=GOOD_YEARS, window=W, cfg=CFG)
    assert r["verdict"] == "pending" and r["failed_gate"] is None
    assert r["gates"]["robust"]["status"] == "review"
    assert r["threshold_version"] == CFG["threshold_version"]
    assert set(r["gates"]) == set(judge.GATE_ORDER)
    for g in r["gates"].values():
        assert {"status", "value", "threshold"} <= set(g)


@pytest.mark.parametrize("trades,years", [(199, 5), (1000, 2), (None, 5)])
def test_sample_gate_gives_insufficient_not_reject(trades, years):
    # 即使尺度也不过，样本不足优先：不算淘汰
    r = judge.judge(M(trades=trades, years=years), scale=0.9, yearly=GOOD_YEARS,
                    window=W, cfg=CFG)
    assert r["verdict"] == "insufficient" and r["failed_gate"] == "sample"


@pytest.mark.parametrize("ratio,status", [(0.2499, "pass"), (0.25, "marginal"),
                                          (0.3999, "marginal"), (0.40, "fail")])
def test_scale_gate_boundaries(ratio, status):
    r = judge.judge(M(), scale=ratio, yearly=GOOD_YEARS, window=W, cfg=CFG)
    assert r["gates"]["scale"]["status"] == status
    if status == "marginal":
        assert r["verdict"] == "pending" and "scale_marginal" in r["flags"]
    if status == "fail":
        assert r["verdict"] == "reject" and r["failed_gate"] == "scale"


def test_failed_gate_is_first_in_order():
    bad_years = [["2022", 100], ["2023", -1], ["2024", -1], ["2025", -1]]
    r = judge.judge(M(total_pnl=-5.0), scale=0.9, yearly=bad_years, window=W, cfg=CFG)
    assert r["failed_gate"] == "scale"
    r = judge.judge(M(total_pnl=-5.0), scale=0.1, yearly=bad_years, window=W, cfg=CFG)
    assert r["failed_gate"] == "yearly"
    r = judge.judge(M(total_pnl=-5.0), scale=0.1, yearly=GOOD_YEARS, window=W, cfg=CFG)
    assert r["failed_gate"] == "drawdown"


def test_yearly_gate_recent_three_full_years_rescue():
    # 正年数 3/5 = 0.6 < 0.8，但最近三个完整年度（2023–2025）都不为负 → 通过
    ys = [["2021", -5], ["2022", -5], ["2023", 0], ["2024", 3], ["2025", 4]]
    r = judge.judge(M(), scale=0.1, yearly=ys,
                    window={"start": "20210101", "end": "20260930"}, cfg=CFG)
    assert r["gates"]["yearly"]["status"] == "pass"
    # 2026 不是完整年度：它亏也不影响「最近三个完整年度」
    ys2 = ys + [["2026", -100]]
    r2 = judge.judge(M(), scale=0.1, yearly=ys2,
                     window={"start": "20210101", "end": "20260930"}, cfg=CFG)
    assert r2["gates"]["yearly"]["value"]["recent"][-1][0] == "2025"
    assert r2["gates"]["yearly"]["status"] == "pass"


def test_yearly_gate_degrades_to_summary_and_records_why():
    r = judge.judge(M(positive_years=4, years=5), scale=0.1, yearly=None, window=W, cfg=CFG)
    y = r["gates"]["yearly"]
    assert y["status"] == "pass" and y["value"]["source"] == "summary"
    assert "yearly_degraded" in r["flags"] and y.get("note")
    # 正年数 1/5：近三年不可能全为正 → 可以确定判负
    r = judge.judge(M(positive_years=1, years=5), scale=0.1, yearly=None, window=W, cfg=CFG)
    assert r["gates"]["yearly"]["status"] == "fail" and r["failed_gate"] == "yearly"
    # 正年数 3/5：判不了 → unknown → 按数据不足处理，不进主列表
    r = judge.judge(M(positive_years=3, years=5), scale=0.1, yearly=None, window=W, cfg=CFG)
    assert r["gates"]["yearly"]["status"] == "unknown"
    assert r["verdict"] == "insufficient" and r["insufficient_reason"] == "data:yearly"


def test_drawdown_gate():
    ok = judge.judge(M(total_pnl=10_000, max_dd=-10_000), scale=0.1, yearly=GOOD_YEARS,
                     window=W, cfg=CFG)
    assert ok["gates"]["drawdown"]["status"] == "pass"
    low = judge.judge(M(total_pnl=9_000, max_dd=-10_000), scale=0.1, yearly=GOOD_YEARS,
                      window=W, cfg=CFG)
    assert low["failed_gate"] == "drawdown" and low["gates"]["drawdown"]["value"] == 0.9
    neg = judge.judge(M(total_pnl=-1, max_dd=0.0, pnl_dd=None), scale=0.1,
                      yearly=GOOD_YEARS, window=W, cfg=CFG)
    assert neg["gates"]["drawdown"]["status"] != "pass"


def test_unknown_scale_never_becomes_pending():
    r = judge.judge(M(), scale=None, yearly=GOOD_YEARS, window=W, cfg=CFG)
    assert r["verdict"] == "insufficient" and r["insufficient_reason"] == "data:scale"


def test_custom_threshold_file_changes_version(tmp_path):
    cfg = json.loads((judge.GATES_PATH).read_text(encoding="utf-8"))
    cfg["threshold_version"] = "test.v9"
    cfg["gates"]["sample"]["min_trades"] = 1000
    f = tmp_path / "g.json"
    f.write_text(json.dumps(cfg), encoding="utf-8")
    c2 = judge.load_gates(f)
    r = judge.judge(M(), scale=0.1, yearly=GOOD_YEARS, window=W, cfg=c2)
    assert r["threshold_version"] == "test.v9" and r["verdict"] == "insufficient"


def test_full_years():
    assert judge.full_years("20220101", "20261009") == [2022, 2023, 2024, 2025]
    assert judge.full_years("20220301", "20251231") == [2023, 2024, 2025]
    assert judge.full_years("", "20251231") == []


def test_round_trip_cost_matches_aiquant_cost_scales():
    # P.DCE：2 跳 × 2 元 + 2 × 2.5 元 / 10 = 4.5 点（与 aiquant cost_scales 一致）
    assert judge.round_trip_cost(resolve("P.DCE"), 8000.0) == pytest.approx(4.5)
    # 茅台：2 × 0.01 + 1700 × (2.5bp×2 + 5bp) = 1.72
    assert judge.round_trip_cost(resolve("600519.SH"), 1700.0) == pytest.approx(1.72)
    # 按成交额收费的期货必须有价格
    assert judge.round_trip_cost(resolve("AG.SHF"), None) is None


def test_result_extras_records_scale_and_yearly():
    class R:                                         # 只模拟 BacktestResult 用到的字段
        params = {"_avg_amp": 20.0, "_avg_px": 8000.0, "slippage_ticks": 1}
        instrument = resolve("P.DCE")
        trades = pd.DataFrame({"日期": ["2022-01-05", "2022-06-01", "2023-02-01"],
                               "净利": [100.0, -40.0, 30.0]})
    ex = judge.result_extras(R())
    assert ex["cost_rt"] == pytest.approx(4.5) and ex["avg_amp"] == 20.0
    assert json.loads(ex["yearly"]) == [["2022", 60.0], ["2023", 30.0]]


def test_store_migrates_result_columns(tmp_path):
    import sqlite3
    db = tmp_path / "old.db"
    con = sqlite3.connect(db)
    # 加列之前的旧表结构
    con.execute("CREATE TABLE results(id INTEGER PRIMARY KEY AUTOINCREMENT, run_id INTEGER, "
                "ts TEXT, symbol TEXT, name TEXT, market TEXT, strategy TEXT, freq TEXT, "
                "start TEXT, \"end\" TEXT, trades INTEGER, win_rate REAL, pf REAL, "
                "avg_points REAL, total_pnl REAL, max_dd REAL, ret_dd REAL, "
                "pos_years INTEGER, years INTEGER, status TEXT, error TEXT, report TEXT)")
    con.commit()
    con.close()
    store.init(db)
    with store.connect(db) as c:
        cols = {r["name"] for r in c.execute("PRAGMA table_info(results)")}
    assert {"avg_amp", "avg_px", "cost_rt", "yearly"} <= cols


# ==================== 许可过滤 ====================
@pytest.mark.parametrize("text,want", [
    ("CC BY-NC-SA 4.0", "nc"),
    ("This work is licensed under a Attribution-NonCommercial-ShareAlike 4.0 "
     "International (CC BY-NC-SA 4.0)", "nc"),
    ("License: Custom © Fatich.id – Non-Redistributable", "restricted"),
    ("MPL-2.0", "open"),
    ("This source code is subject to the terms of the Mozilla Public License 2.0", "open"),
    ("MIT", "open"),
    ("CC BY-SA 4.0", "open"),
    ("—", "unknown"),
    ("", "unknown"),
])
def test_license_classify(text, want):
    assert licensing.classify(text) == want


class _S:
    def __init__(self, license="", source="", source_sid=""):
        self.license, self.source, self.source_sid = license, source, source_sid


def test_license_resolve_most_restrictive_wins():
    pol = {"unknown_policy": "allow", "nc_authors": ["LuxAlgo"]}
    nc_header = lambda sid: "// This work is licensed under (CC BY-NC-SA 4.0)"  # noqa: E731
    li = licensing.resolve(_S("MIT", "x", "PUB;1"), pol, nc_header)
    assert li.status == "nc" and not li.commercial_ok and li.basis == "corpus"
    assert not licensing.resolve(_S("CC BY-NC-SA 4.0"), pol, lambda s: "").commercial_ok
    li = licensing.resolve(_S("—", "TradingView @LuxAlgo"), pol, lambda s: "")
    assert li.status == "nc" and li.basis == "author"
    li = licensing.resolve(_S("—", "TradingView @someone"), pol, lambda s: "")
    assert li.status == "unknown" and li.commercial_ok
    deny = {"unknown_policy": "deny", "nc_authors": []}
    assert not licensing.resolve(_S("—"), deny, lambda s: "").commercial_ok


def test_registry_nc_ports_excluded_and_cleanroom_allowed():
    pol = licensing.load_policy()
    nolookup = lambda s: ""                                                     # noqa: E731
    assert not licensing.resolve(REGISTRY["orb"], pol, nolookup).commercial_ok
    assert not licensing.resolve(REGISTRY["false_breakout"], pol, nolookup).commercial_ok
    for key in ("orb_classic", "breakout_fade"):
        s = REGISTRY[key]
        assert s.source == "原创实现" and s.license == "MIT" and "思路来源" in s.origin
        assert licensing.resolve(s, pol, nolookup).status == "open"


# ==================== 导出 ====================
def _row(strategy, symbol="P.DCE", freq="5min", **kw):
    r = {"run_id": 1, "ts": "2026-10-09T12:00:00", "symbol": symbol, "name": "棕榈油",
         "market": "futures", "strategy": strategy, "freq": freq, "start": "20220101",
         "end": "20260930", "trades": 500, "win_rate": 0.45, "pf": 1.3,
         "avg_points": 2.0, "total_pnl": 50_000.0, "max_dd": -10_000.0, "ret_dd": 5.0,
         "pos_years": 4, "years": 4, "status": "ok", "error": "",
         "report": f"{symbol}_{strategy}_{freq}_worker.html",
         "avg_amp": 40.0, "avg_px": 8000.0, "cost_rt": 4.5,
         "yearly": json.dumps(GOOD_YEARS)}
    r.update(kw)
    return r


@pytest.fixture()
def fx_db(tmp_path):
    db = tmp_path / "r.db"
    store.init(db)
    store.add_result(_row("utbot", pf=float("inf")), db)                  # pending
    store.add_result(_row("supertrend", avg_amp=15.0), db)                # 0.30 勉强 → pending
    store.add_result(_row("ema_cross", avg_amp=8.0), db)                  # 0.56 → reject scale
    store.add_result(_row("chandelier", trades=50), db)                   # insufficient
    store.add_result(_row("orb"), db)                                     # NC → 排除
    store.add_result(_row("ghost_strategy"), db)                          # 未注册 → 排除
    store.add_result(_row("breakout_fade", freq="15min", status="error"), db)  # 失败行不导出
    return db


def _export(db, tmp_path, **kw):
    return falsify.build_export(db_path=db, report_dir=tmp_path, cache_dir=None,
                                header_lookup=lambda s: "", **kw)


def test_export_shape_and_filters(fx_db, tmp_path):
    out = _export(fx_db, tmp_path)
    raw = json.dumps(out, ensure_ascii=False, allow_nan=False)    # 必须是严格 JSON
    assert set(out) >= {"generated_at", "threshold_version", "gates", "summary", "archive"}
    assert [g["id"] for g in out["gates"]] == list(judge.GATE_ORDER)
    for g in out["gates"]:
        assert set(g) >= {"id", "name", "rule", "why"}
    # 报告路径绝不外泄
    assert "worker.html" not in raw and '"report"' not in raw
    keys = {e["strategy_key"] for e in out["archive"] if not e["curated"]}
    assert keys == {"utbot", "supertrend", "ema_cross"}
    s = out["summary"]
    assert s["excluded_license"] >= 1 and s["unregistered"] == 1
    assert s["insufficient_hidden"] == 1 and s["curated"] == 8
    by = {e["strategy_key"]: e for e in out["archive"] if not e["curated"]}
    assert by["utbot"]["verdict"] == "pending" and by["utbot"]["metrics"]["pf"] is None
    assert by["supertrend"]["verdict"] == "pending"
    assert "scale_marginal" in by["supertrend"]["flags"]
    assert by["ema_cross"]["verdict"] == "reject" and by["ema_cross"]["failed_gate"] == "scale"
    for e in out["archive"]:
        assert e["threshold_version"] == out["threshold_version"]
        assert set(e) == set(falsify.ENTRY_FIELDS)
        assert set(e["metrics"]) == set(falsify.METRIC_FIELDS)
        if e["verdict"] == "reject":
            assert e["failed_gate"] in ("scale", "yearly", "drawdown")
    assert not any(e["verdict"] == "tradable" for e in out["archive"])


def test_export_include_insufficient_and_limit(fx_db, tmp_path):
    out = _export(fx_db, tmp_path, include_insufficient=True)
    v = {e["strategy_key"]: e["verdict"] for e in out["archive"] if not e["curated"]}
    assert v["chandelier"] == "insufficient"
    out = _export(fx_db, tmp_path, limit=1)
    assert out["summary"]["auto"] == 1 and out["summary"]["truncated"] == 2
    assert out["summary"]["curated"] == 8              # 精选档案不受 limit 影响


def test_curated_archive_is_source_of_truth(fx_db, tmp_path):
    out = _export(fx_db, tmp_path)
    cur = {e["id"]: e for e in out["archive"] if e["curated"]}
    assert set(cur) == {"utbot-5min", "utbot-regime", "orb-5min", "false-breakout-1min",
                        "vreversal-1min", "atr-sweep", "partial-tp", "breakeven-stop"}
    assert cur["utbot-5min"]["verdict"] == "reject"
    assert cur["utbot-5min"]["failed_gate"] == "yearly"
    assert cur["utbot-regime"]["verdict"] == "insufficient"     # 55 笔
    assert cur["vreversal-1min"]["failed_gate"] == "scale"       # 1 分钟成本占比 58%
    for k in ("orb-5min", "false-breakout-1min"):
        e = cur[k]
        assert e["strategy_key"] in ("orb_classic", "breakout_fade")
        assert e["source"] == "原创实现" and "思路来源" in e["origin"]
        assert "rerun_pending" in e["flags"] and e["rerun_note"]
        assert "NC" not in e["source"] and e["license"] == "MIT"
    for k in ("atr-sweep", "partial-tp", "breakeven-stop"):
        assert cur[k]["verdict"] == "finding" and cur[k]["failed_gate"] is None


def test_tradable_only_via_override_on_pending(fx_db, tmp_path):
    ov = tmp_path / "ov.json"
    ov.write_text(json.dumps({"overrides": [
        {"id": "utbot-p-dce-5min", "verdict": "tradable", "reviewer": "eric",
         "reviewed_at": "2026-10-10", "note": "参数扫描平台"},
        {"id": "ema-cross-p-dce-5min", "verdict": "tradable", "reviewer": "eric"},
    ]}), encoding="utf-8")
    out = _export(fx_db, tmp_path, overrides_path=ov)
    v = {e["id"]: e["verdict"] for e in out["archive"]}
    assert v["utbot-p-dce-5min"] == "tradable"
    assert v["ema-cross-p-dce-5min"] == "reject"                 # 被淘汰的不能人工翻案
    assert out["summary"]["override_applied"] == 1
    assert out["summary"]["override_ignored"] == 1


def test_yearly_falls_back_to_trades_csv(tmp_path):
    db = tmp_path / "r.db"
    store.init(db)
    store.add_result(_row("utbot", yearly=None, pos_years=3, years=5,
                          report="P.DCE_utbot_5min_worker.html"), db)
    pd.DataFrame({"日期": ["2022-03-01", "2023-03-01", "2024-03-01", "2025-03-01"],
                  "净利": [-5.0, 1.0, 1.0, 1.0]}).to_csv(
        tmp_path / "P.DCE_utbot_5min_worker.trades.csv", index=False, encoding="utf-8-sig")
    out = _export(db, tmp_path)
    e = next(x for x in out["archive"] if x["strategy_key"] == "utbot" and not x["curated"])
    assert e["gates"]["yearly"]["value"]["source"] == "trades_csv"
    assert e["gates"]["yearly"]["status"] == "pass"              # 近三年都不为负


def test_cli_falsify_export(tmp_path, fx_db):
    from alpharadar.cli import main
    out = tmp_path / "x.json"
    assert main(["falsify-export", "--out", str(out), "--db", str(fx_db)]) == 0
    data = json.loads(out.read_text(encoding="utf-8"))
    assert data["threshold_version"] and data["archive"]


# ==================== clean-room 策略 ====================
def _day_bars(day: str, closes_highs_lows, start="0905", step=5):
    rows = []
    t = pd.Timestamp(f"{day[:4]}-{day[4:6]}-{day[6:]} {start[:2]}:{start[2:]}")
    for c, h, lo in closes_highs_lows:
        rows.append({"trade_time": t, "sdate": day, "open": c, "high": h, "low": lo,
                     "close": c, "vol": 1000.0})
        t += pd.Timedelta(minutes=step)
    return rows


def test_cleanroom_strategies_pass_porting_gates():
    from alpharadar.porting.verify import synth_bars, verify_strategy
    bars = synth_bars(1500)
    for key in ("orb_classic", "breakout_fade"):
        r = verify_strategy(key, bars=bars)
        assert r["ok"], f"{key}: {r['errors']}"


def test_orb_classic_rules():
    s = REGISTRY["orb_classic"]
    # 前 6 根（09:05–09:30）构成区间 [99, 101]，09:35 收盘 102 上破，之后再破也不做
    seq = [(100, 101, 99)] * 6 + [(102, 102.5, 100), (103, 103, 102), (98, 103, 97)]
    df = pd.DataFrame(_day_bars("20240102", seq))
    df["atr"] = 1.0
    p = dict(s.defaults)
    out = s.fn(df, {**p, "or_min_atr": 0.5}).reset_index(drop=True)
    # signal_frame 默认前 60 根静默，这里直接看原始逻辑：把预热关掉重跑
    from alpharadar.strategies import cleanroom
    import alpharadar.strategies.base as base
    orig = base.signal_frame
    try:
        cleanroom.signal_frame = lambda d, sig, **kw: orig(d, sig, min_bars=0, **kw)
        out = s.fn(df, p).reset_index(drop=True)
    finally:
        cleanroom.signal_frame = orig
    assert out["sig"].tolist() == [0] * 6 + [1, 0, 0]
    assert out.loc[6, "stop_px"] == 99                          # 止损在区间另一侧
    assert out.loc[:5, "sig"].eq(0).all()                       # 区间形成期内绝不交易


def test_breakout_fade_rules():
    s = REGISTRY["breakout_fade"]
    # 第 1 根定旧高 110（第 21 根时它正好在前 20 根窗口内、已有 20 根「年龄」），
    # 其余在 100 附近横盘；第 21 根刺破到 112 但收在 111，下一根收回 108 → 做空
    seq = ([(100, 101, 99), (105, 110, 100)] + [(100, 101, 99)] * 19
           + [(111, 112, 105), (108, 111, 107), (100, 101, 99)])
    df = pd.DataFrame(_day_bars("20240102", seq, start="0901", step=1))
    df["atr"] = 1.0
    p = dict(s.defaults)
    from alpharadar.strategies import cleanroom
    import alpharadar.strategies.base as base
    orig = base.signal_frame
    try:
        cleanroom.signal_frame = lambda d, sig, **kw: orig(d, sig, min_bars=0, **kw)
        out = s.fn(df, p).reset_index(drop=True)
    finally:
        cleanroom.signal_frame = orig
    sig = out["sig"].to_numpy()
    assert sig[22] == -1 and (np.delete(sig, 22) == 0).all()
    assert out.loc[22, "stop_px"] == pytest.approx(112 + 0.5)   # 突破极值 + 0.5 ATR


# ==================== 管理口令 ====================
@pytest.fixture()
def server(monkeypatch):
    from alpharadar import web
    calls = []

    def fake(self):
        calls.append(self.path)
        self._redirect("/job/x")

    monkeypatch.setattr(web.Handler, "_trigger_cycle", fake)
    monkeypatch.setattr(web.Handler, "_trigger_harvest", fake)
    monkeypatch.setattr(web.Handler, "log_message", lambda *a, **k: None)
    srv = ThreadingHTTPServer(("127.0.0.1", 0), web.Handler)
    th = threading.Thread(target=srv.serve_forever, daemon=True)
    th.start()
    yield f"http://127.0.0.1:{srv.server_address[1]}", calls
    srv.shutdown()
    srv.server_close()


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **k):
        return None


def _post(url, data=b"", headers=None):
    req = urllib.request.Request(url, data=data, headers=headers or {}, method="POST")
    opener = urllib.request.build_opener(_NoRedirect)
    try:
        return opener.open(req, timeout=5).status
    except urllib.error.HTTPError as e:
        return e.code


@pytest.mark.parametrize("path", ["/runs/trigger", "/scripts/harvest"])
def test_admin_post_fails_closed_without_env(server, monkeypatch, path):
    base, calls = server
    monkeypatch.delenv("ALPHARADAR_ADMIN_TOKEN", raising=False)
    assert _post(base + path, b"token=anything") == 403
    assert calls == []


@pytest.mark.parametrize("path", ["/runs/trigger", "/scripts/harvest"])
def test_admin_post_requires_correct_token(server, monkeypatch, path):
    base, calls = server
    monkeypatch.setenv("ALPHARADAR_ADMIN_TOKEN", "s3cret")
    assert _post(base + path) == 403
    assert _post(base + path, b"token=wrong") == 403
    assert _post(base + path, headers={"X-Admin-Token": "s3cre"}) == 403
    assert calls == []
    assert _post(base + path, b"token=s3cret") == 303
    assert _post(base + path, headers={"X-Admin-Token": "s3cret"}) == 303
    assert _post(base + path, headers={"Authorization": "Bearer s3cret"}) == 303
    assert len(calls) == 3


def test_api_falsification_endpoint(server, monkeypatch, fx_db, tmp_path):
    from alpharadar import web
    base, _ = server
    real = falsify.build_export
    monkeypatch.setattr(falsify, "build_export",
                        lambda **kw: real(db_path=fx_db, report_dir=tmp_path, cache_dir=None,
                                          header_lookup=lambda s: "", **kw))
    web._fx_cache.clear()
    with urllib.request.urlopen(base + "/api/falsification", timeout=10) as r:
        data = json.loads(r.read())
    assert data["summary"]["include_insufficient"] is False
    assert all(e["verdict"] != "insufficient" for e in data["archive"] if not e["curated"])
    with urllib.request.urlopen(base + "/api/falsification?include=insufficient",
                                timeout=10) as r:
        data2 = json.loads(r.read())
    assert any(e["verdict"] == "insufficient" for e in data2["archive"] if not e["curated"])
    assert "report" not in json.dumps(data2)
    web._fx_cache.clear()
