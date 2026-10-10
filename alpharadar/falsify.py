"""证伪档案导出：精选档案 + 结果库自动判定 → 对外 JSON（给 aiquant 后端）。

入口：
    build_export(...)                         -> dict
    CLI   alpharadar falsify-export --out x.json [--include-insufficient]
    HTTP  GET /api/falsification[?include=insufficient][&limit=N]

对外契约见 docs/falsification-export.md。硬约束：
  - 只用白名单字段拼条目，**永远不带 report 路径**（HTML 报告不对外）；
  - 非商用 / 禁止再分发许可的策略不导出（licensing.py）；
  - insufficient 默认不导出（精选档案除外，它们单独成组）；
  - tradable 只能来自 config/verdict_overrides.json，且自动结论必须是 pending。
"""

from __future__ import annotations

import json
import math
import re
from datetime import datetime
from pathlib import Path

from . import config, judge, licensing
from .universe import PRESETS, is_fund

CURATED_PATH = config.ROOT / "docs" / "archive" / "curated.json"
OVERRIDES_PATH = config.ROOT / "config" / "verdict_overrides.json"
SCHEMA_VERSION = 1

VERDICT_RANK = {"tradable": 0, "pending": 1, "finding": 2, "reject": 3, "insufficient": 4}
FAMILY_LABEL = {
    "trend": "趋势跟随", "breakout": "突破", "reversal": "反转",
    "oscillator": "震荡指标", "bands": "通道", "level": "关键价位",
    "volatility": "波动率", "volume": "量价", "pattern": "形态",
    "research": "研究发现", "unknown": "其他",
}
BUILTIN_FAMILY = {
    "utbot": "trend", "supertrend": "trend", "chandelier": "trend",
    "ema_cross": "trend", "rangefilter": "trend", "vreversal": "reversal",
    "orb": "breakout", "orb_classic": "breakout",
    "false_breakout": "reversal", "breakout_fade": "reversal",
}
# 对外条目只允许这些字段（白名单），防止内部字段（report 等）意外泄露
ENTRY_FIELDS = (
    "id", "strategy", "strategy_key", "family", "family_key", "source", "origin",
    "license", "license_status", "symbol", "name", "asset_class", "freq",
    "verdict", "failed_gate", "gates", "threshold_version", "flags",
    "insufficient_reason", "editor_verdict", "headline", "metrics", "few", "yearly",
    "mechanism", "command", "window", "curated", "rerun_note", "judged_at",
    "updated_at",
)
METRIC_FIELDS = ("trades", "win", "pf", "avg_points", "max_dd_pct", "pnl_dd",
                 "positive_years", "years", "total_pnl", "max_dd")


def _clean(v):
    """JSON 安全：NaN / inf → None（标准 JSON 不允许 Infinity）。"""
    if isinstance(v, float) and not math.isfinite(v):
        return None
    if isinstance(v, dict):
        return {k: _clean(x) for k, x in v.items()}
    if isinstance(v, (list, tuple)):
        return [_clean(x) for x in v]
    return v


def _slug(*parts: str) -> str:
    s = "-".join(str(p) for p in parts if p)
    return re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-")


def asset_class(symbol: str, market: str | None) -> str:
    m = str(market or "").lower()
    if m.startswith("fut"):
        return "futures"
    if not m:
        inst = PRESETS.get(symbol)
        if inst is not None and inst.market == "futures":
            return "futures"
        if not symbol.upper().endswith((".SH", ".SZ", ".BJ")):
            return "futures"
    return "etf" if is_fund(symbol) else "stock"


def _gate_list(cfg: dict) -> list[dict]:
    return [{"id": gid, "name": cfg["gates"][gid]["name"],
             "rule": cfg["gates"][gid]["rule"], "why": cfg["gates"][gid]["why"]}
            for gid in judge.GATE_ORDER]


def _pct(x) -> str:
    return f"{x * 100:.0f}%"


def headline(entry: dict) -> str:
    """自动条目的一句话结论（精选档案用手写 headline）。"""
    v, fg, g, m = entry["verdict"], entry.get("failed_gate"), entry["gates"], entry["metrics"]
    if v == "insufficient":
        if (entry.get("insufficient_reason") or "").startswith("data:"):
            return "数据不全，暂不下结论"
        return f"样本不足：{m.get('trades') or 0} 笔 / {m.get('years') or 0} 个年份"
    if v == "reject":
        if fg == "scale":
            return f"成本吃掉平均振幅的 {_pct(g['scale']['value'])}，这个周期做不了"
        if fg == "yearly":
            yv = g["yearly"].get("value") or {}
            return (f"正收益年份 {yv.get('positive_years')}/{yv.get('years')}，"
                    f"利润不稳定")
        if fg == "drawdown":
            pdd = g["drawdown"].get("value")
            if (m.get("total_pnl") or 0) <= 0:
                return "扣完成本总盈亏为负"
            return f"收益回撤比 {pdd:.2f}，回撤吃掉了利润" if pdd is not None \
                else "收益回撤比不达标"
    if v == "pending":
        return "四道闸门全过，等待参数稳健性人工复核"
    if v == "tradable":
        return "通过全部闸门与人工稳健性复核"
    return ""


def _few(m: dict) -> str:
    parts = []
    if m.get("trades") is not None:
        parts.append(f"{m['trades']} 笔")
    if m.get("win") is not None:
        parts.append(f"胜率 {m['win'] * 100:.1f}%")
    if m.get("pf") is not None:
        parts.append(f"PF {m['pf']:.2f}")
    if m.get("avg_points") is not None:
        parts.append(f"每手 {m['avg_points']:+.2f} 点")
    if m.get("years"):
        parts.append(f"{m.get('positive_years') or 0}/{m['years']} 年为正")
    return " / ".join(parts)


# ==================== 精选档案 ====================
def load_curated(path: Path | None = None) -> list[dict]:
    p = Path(path or CURATED_PATH)
    doc = json.loads(p.read_text(encoding="utf-8"))
    recs = doc.get("archive", [])
    for r in recs:
        r.setdefault("updated_at", doc.get("updated_at") or "")
    return recs


def judge_curated(rec: dict, cfg: dict, now: str) -> dict:
    m = {k: rec.get("metrics", {}).get(k) for k in METRIC_FIELDS}
    scale = (rec.get("scale") or {})
    res = judge.judge(m, scale=scale.get("ratio"), yearly=rec.get("yearly") or None,
                      window=rec.get("window") or {}, cfg=cfg,
                      editor_verdict=rec.get("editor_verdict"),
                      scale_note=scale.get("source"))
    out = dict(rec)
    out.update(res)
    out["metrics"] = m
    out["flags"] = sorted(set(rec.get("flags") or []) | set(res["flags"]))
    out["curated"] = True
    out["judged_at"] = now
    out["updated_at"] = rec.get("updated_at") or ""
    return out


# ==================== 结果库自动条目 ====================
def latest_ok_results(db_path: Path | None = None) -> list[dict]:
    """每个 (品种, 策略, 周期) 最新的一条成功结果。"""
    from . import store
    store.init(db_path)
    sql = """
      SELECT r.* FROM results r
      JOIN (SELECT symbol, strategy, freq, MAX(id) mid FROM results
            WHERE status='ok' GROUP BY symbol, strategy, freq) t ON r.id = t.mid"""
    with store.connect(db_path) as con:
        return [dict(r) for r in con.execute(sql).fetchall()]


def _port_families(db_path: Path | None = None) -> dict[str, str]:
    try:
        from . import store
        with store.connect(db_path) as con:
            rows = con.execute("SELECT strategy_key, family FROM ports "
                               "WHERE strategy_key IS NOT NULL AND strategy_key<>''"
                               ).fetchall()
        return {r["strategy_key"]: r["family"] or "unknown" for r in rows}
    except Exception:
        return {}


def _yearly_for(row: dict, report_dir: Path | None) -> tuple[list | None, str | None]:
    """逐年盈亏：优先结果库的 yearly 列，其次报告目录里的逐笔 CSV。"""
    raw = row.get("yearly")
    if raw:
        try:
            return json.loads(raw), "yearly"
        except Exception:
            pass
    rep = str(row.get("report") or "")
    if rep.endswith(".html") and report_dir is not None:
        f = Path(report_dir) / (rep[:-5] + ".trades.csv")
        if f.is_file():
            try:
                import pandas as pd
                tr = pd.read_csv(f, encoding="utf-8-sig", dtype={"日期": str})
                return judge.yearly_from_trades(tr), "trades_csv"
            except Exception:
                pass
    return None, None


def judge_row(row: dict, strat, lic: licensing.LicenseInfo, cfg: dict, now: str,
              families: dict, report_dir: Path | None, cache_dir: Path | None) -> dict:
    sym, key, freq = row["symbol"], row["strategy"], row["freq"]
    metrics = {
        "trades": row.get("trades"), "win": row.get("win_rate"), "pf": row.get("pf"),
        "avg_points": row.get("avg_points"), "max_dd_pct": None,
        "pnl_dd": row.get("ret_dd"), "positive_years": row.get("pos_years"),
        "years": row.get("years"), "total_pnl": row.get("total_pnl"),
        "max_dd": row.get("max_dd"),
    }
    metrics = _clean(metrics)
    scale_note = None
    ratio = judge.scale_ratio(row.get("cost_rt"), row.get("avg_amp"))
    if ratio is None and cache_dir is not None and (metrics.get("trades") or 0) > 0:
        sc = judge.scale_from_cache(cache_dir, sym, freq, str(row.get("start") or ""),
                                    cfg["gates"]["scale"].get("slippage_ticks", 1))
        if sc:
            ratio, scale_note = sc["ratio"], "由本机行情缓存重算"
    yearly, ysrc = _yearly_for(row, report_dir)
    window = {"start": str(row.get("start") or ""), "end": str(row.get("end") or "")}
    res = judge.judge(metrics, scale=ratio, yearly=yearly, window=window, cfg=cfg,
                      scale_note=scale_note, yearly_source=ysrc)
    fam = families.get(key) or BUILTIN_FAMILY.get(key) or "unknown"
    inst = PRESETS.get(sym)
    e = {
        "id": _slug(key, sym, freq),
        "strategy": strat.name, "strategy_key": key,
        "family": FAMILY_LABEL.get(fam, FAMILY_LABEL["unknown"]), "family_key": fam,
        "source": strat.source, "origin": getattr(strat, "origin", "") or "",
        "license": lic.label, "license_status": lic.status,
        "symbol": sym, "name": row.get("name") or (inst.name if inst else sym),
        "asset_class": asset_class(sym, row.get("market")), "freq": freq,
        **res,
        "editor_verdict": None, "metrics": metrics, "yearly": yearly or [],
        "mechanism": "", "window": window, "curated": False,
        "command": f"alpharadar run --symbol {sym} --strategy {key} --freq {freq}"
                   f" --start {window['start'] or '20220101'}",
        "judged_at": now, "updated_at": str(row.get("ts") or ""),
    }
    e["headline"] = headline(e)
    e["few"] = _few(metrics)
    return e


# ==================== 人工覆盖 ====================
def load_overrides(path: Path | None = None) -> dict[str, dict]:
    p = Path(path or OVERRIDES_PATH)
    if not p.exists():
        return {}
    data = json.loads(p.read_text(encoding="utf-8"))
    return {o["id"]: o for o in data.get("overrides", []) if o.get("id")}


def apply_override(entry: dict, ov: dict | None) -> bool:
    """只有自动结论为 pending 时才允许人工改成 tradable；返回是否生效。"""
    if not ov:
        return False
    if ov.get("verdict") != "tradable" or entry["verdict"] != "pending":
        return False
    entry["verdict"] = "tradable"
    entry["gates"]["robust"] = {"status": "pass", "value": None, "threshold": None,
                                "note": f"人工复核：{ov.get('reviewer', '')} "
                                        f"{ov.get('reviewed_at', '')} {ov.get('note', '')}"
                                        .strip()}
    return True


# ==================== 汇总 ====================
def _project(entry: dict) -> dict:
    out = {k: entry.get(k) for k in ENTRY_FIELDS}
    out["metrics"] = {k: (entry.get("metrics") or {}).get(k) for k in METRIC_FIELDS}
    return _clean(out)


def build_export(*, db_path: Path | None = None, include_insufficient: bool = False,
                 limit: int | None = None, curated_path: Path | None = None,
                 gates_path: Path | None = None, overrides_path: Path | None = None,
                 report_dir: Path | None = config.REPORT_DIR,
                 cache_dir: Path | None = config.CACHE_DIR,
                 license_policy: dict | None = None, registry: dict | None = None,
                 header_lookup=None, now: datetime | None = None) -> dict:
    cfg = judge.load_gates(gates_path)
    now_s = (now or datetime.now()).isoformat(timespec="seconds")
    policy = license_policy if license_policy is not None else licensing.load_policy()
    if registry is None:
        from .strategies import REGISTRY as registry          # noqa: N811
    overrides = load_overrides(overrides_path)
    lic_cache: dict[str, licensing.LicenseInfo] = {}

    def lic_of(key: str):
        if key not in lic_cache:
            s = registry.get(key)
            lic_cache[key] = (licensing.resolve(s, policy, header_lookup) if s
                              else licensing.LicenseInfo("unknown", False, "未注册", "none"))
        return lic_cache[key]

    stats = {"excluded_license": 0, "insufficient_hidden": 0, "unregistered": 0,
             "override_applied": 0, "override_ignored": 0, "truncated": 0}

    curated: list[dict] = []
    for rec in load_curated(curated_path):
        e = judge_curated(rec, cfg, now_s)
        key = rec.get("strategy_key") or ""
        if key and key in registry:
            li = lic_of(key)
            if not li.commercial_ok:
                stats["excluded_license"] += 1
                continue
            e["license_status"] = li.status
        else:
            e["license_status"] = licensing.classify(rec.get("license"))
        ov = overrides.get(e["id"])
        if ov:
            ok = apply_override(e, ov)
            stats["override_applied" if ok else "override_ignored"] += 1
        curated.append(e)

    auto: list[dict] = []
    families = _port_families(db_path)
    for row in latest_ok_results(db_path):
        key = row["strategy"]
        strat = registry.get(key)
        if strat is None:
            stats["unregistered"] += 1
            continue
        li = lic_of(key)
        if not li.commercial_ok:
            stats["excluded_license"] += 1
            continue
        e = judge_row(row, strat, li, cfg, now_s, families, report_dir, cache_dir)
        ov = overrides.get(e["id"])
        if ov:
            ok = apply_override(e, ov)
            stats["override_applied" if ok else "override_ignored"] += 1
        if e["verdict"] == "insufficient" and not include_insufficient:
            stats["insufficient_hidden"] += 1
            continue
        auto.append(e)

    auto.sort(key=lambda e: (VERDICT_RANK.get(e["verdict"], 9), e["family_key"],
                             e["strategy_key"], e["symbol"], e["freq"]))
    if limit is not None and limit >= 0 and len(auto) > limit:
        stats["truncated"] = len(auto) - limit
        auto = auto[:limit]
    archive = [_project(e) for e in curated + auto]

    by_verdict = {v: 0 for v in VERDICT_RANK}
    by_gate = {g: 0 for g in judge.REJECT_GATES}
    for e in archive:
        by_verdict[e["verdict"]] = by_verdict.get(e["verdict"], 0) + 1
        if e["verdict"] == "reject" and e["failed_gate"] in by_gate:
            by_gate[e["failed_gate"]] += 1
    summary = {
        "archive_total": len(archive),
        "curated": len(curated),
        "auto": len(auto),
        "by_verdict": by_verdict,
        "failed_gate": by_gate,
        "tradable": by_verdict.get("tradable", 0),
        "pending": by_verdict.get("pending", 0),
        "rejected": by_verdict.get("reject", 0),
        "scale_marginal": sum(1 for e in archive if "scale_marginal" in (e["flags"] or [])),
        "rerun_pending": sum(1 for e in archive if "rerun_pending" in (e["flags"] or [])),
        "include_insufficient": include_insufficient,
        **stats,
    }
    return {
        "schema_version": SCHEMA_VERSION,
        "generated_at": now_s,
        "threshold_version": cfg["threshold_version"],
        "gates": _gate_list(cfg),
        "summary": summary,
        "archive": archive,
    }


def write_export(out: Path, **kw) -> dict:
    payload = build_export(**kw)
    out = Path(out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(payload, ensure_ascii=False, indent=2, allow_nan=False)
                   + "\n", encoding="utf-8")
    return payload
