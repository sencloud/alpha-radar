"""机器分诊：给语料库里的每个脚本打分、分类、检测风险，产出移植工单队列。

为什么要分诊：533 个脚本不可能平均用力。先按「可移植性 × 价值」排序，
让 agent 从最可能有产出的一批开始。

打分维度：
  + 是 strategy（本来就有入场出场规则）         +30
  + 命中明确指标族（有标准包装器可用）           +20
  + 代码短（<150 行，逻辑好核对）                +15
  + 点赞高（经过社区检验）                       +0~15
  - 有重绘/未来函数风险                         -25
  - 代码过长（>400 行）                          -10
  - 是 library（纯函数库，没有可交易信号）       -20
"""

from __future__ import annotations

import re
from datetime import datetime

from .. import store

# 指标族 -> 关键词。用于决定用哪个标准包装器把「指标」变成「策略」。
FAMILIES: dict[str, tuple[str, ...]] = {
    "oscillator": ("rsi", "stoch", "stochastic", "cci", "williams", "mfi",
                   "dmi", "adx", "momentum", "roc", "trix"),
    "bands": ("bollinger", "boll", "keltner", "donchian", "envelope", "channel",
              "band", "ribbon"),
    "trend": ("supertrend", "sar", "sma", "ema", "wma", "hma", "moving average",
              "ma cross", "trend", "vwap", "hull", "alma"),
    "level": ("pivot", "support", "resistance", "fib", "level", "value area",
              "poc", "vwap"),
    "volatility": ("atr", "true range", "squeeze", "volatility", "stddev",
                   "vix", "deviation"),
    "volume": ("volume", "obv", "open interest", "oi", "delta", "cvd", "money flow"),
    "pattern": ("engulf", "hammer", "doji", "fractal", "divergence", "candle",
                "pattern", "3 drives", "head and shoulders", "hook"),
}

# 未来函数 / 重绘风险：命中的扣分并写进工单，要求移植时按已确认口径改写
RISK_PATTERNS: tuple[tuple[str, str], ...] = (
    (r"lookahead\s*=\s*barmerge\.lookahead_on", "security 用了 lookahead_on"),
    (r"request\.security\s*\(", "request.security 跨周期，需确认收盘口径"),
    (r"pivothigh\s*\(|pivotlow\s*\(", "pivot 需 prd 根后才确认"),
    (r"barstate\.isrealtime", "实时 bar 判定，回测语义不同"),
    (r"varip\b", "varip 逐 tick 状态，分钟线上不可复现"),
    (r"timn?ow\b|timenow", "用到当前时间，非确定性"),
    (r"request\.financial|request\.dividends", "财务数据有发布延迟"),
)

def _kw_re(keywords: tuple[str, ...]) -> re.Pattern:
    """关键词 -> 正则。

    短的全字母关键词必须加「非字母数字」边界：否则 `rsi` 会命中
    `//@version=5` 里的 "ve-rsi-on"，`sar` 会命中 "ne-sar"? 之类。
    用 (?<![a-z0-9]) 而不是 \\b，是为了让 `ta.rsi` / `my_rsi` 仍能命中
    （下划线在 \\b 语义里算词字符，会把它们排除掉）。
    """
    parts = []
    for k in keywords:
        k = k.strip()
        if not k:
            continue
        if re.fullmatch(r"[a-z0-9%]+", k) and len(k) <= 6:
            parts.append(rf"(?<![a-z0-9]){re.escape(k)}(?![a-z0-9])")
        else:
            parts.append(re.escape(k))
    return re.compile("|".join(parts), re.I)


_RE = {k: _kw_re(v) for k, v in FAMILIES.items()}
_RISK = [(re.compile(p, re.I), msg) for p, msg in RISK_PATTERNS]


def classify(src: str, kind: str = "", agree: int = 0, lines: int = 0) -> dict:
    """给单个脚本算家族、风险与可移植性得分。"""
    body = src
    code_lines = lines or len(src.splitlines())
    scores = {fam: len(rx.findall(body)) for fam, rx in _RE.items()}
    fam = max(scores, key=lambda k: scores[k]) if any(scores.values()) else "unknown"

    risks = [msg for rx, msg in _RISK if rx.search(body)]
    is_strategy = bool(re.search(r"strategy\.(entry|order|close|exit)\s*\(", body))

    score = 0.0
    score += 30 if is_strategy else 0
    score += 20 if fam != "unknown" else 0
    score += 15 if code_lines <= 150 else (5 if code_lines <= 400 else -10)
    score += min(15.0, (agree or 0) / 6000.0)
    score -= 25 if risks else 0
    if kind == "library":
        score -= 20

    return {"family": fam, "is_strategy": is_strategy, "risks": risks,
            "score": round(score, 1), "lines": code_lines,
            "family_scores": {k: v for k, v in scores.items() if v}}


def triage(force: bool = False, verbose=print) -> dict:
    """遍历语料库，写入/更新移植台账，返回统计。"""
    from ..config import CORPUS_DIR
    store.init()
    scripts = store.list_scripts(limit=100000)[0]
    rows, stat = [], {"total": 0, "strategy": 0, "unknown": 0, "risky": 0}
    for s in scripts:
        f = CORPUS_DIR / "sources" / (s["file"] or "")
        if not f.exists():
            continue
        try:
            src = f.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        info = classify(src, s["kind"], s["agree"], s["lines"])
        stat["total"] += 1
        stat["strategy"] += int(info["is_strategy"])
        stat["unknown"] += int(info["family"] == "unknown")
        stat["risky"] += int(bool(info["risks"]))
        rows.append({"sid": s["sid"], "title": s["title"], "author": s["author"],
                     "agree": s["agree"], "kind": s["kind"], **info})
    with store.connect() as con:
        for r in rows:
            con.execute(
                "INSERT INTO ports(sid,title,author,agree,kind,family,risk,status,"
                "score,updated_at) VALUES(?,?,?,?,?,?,?,'pending',?,?) "
                "ON CONFLICT(sid) DO UPDATE SET title=excluded.title, "
                "agree=excluded.agree, kind=excluded.kind, family=excluded.family, "
                "risk=excluded.risk, score=excluded.score, updated_at=excluded.updated_at",
                (r["sid"], r["title"], r["author"], r["agree"], r["kind"],
                 r["family"], "; ".join(r["risks"]), r["score"],
                 datetime.now().isoformat(timespec="seconds")))
    stat["worklist"] = len(port_stats()["top"])
    verbose(f"[triage] 分诊 {stat['total']} 个脚本："
            f"原生策略 {stat['strategy']}，未识别家族 {stat['unknown']}，"
            f"有重绘风险 {stat['risky']}")
    return stat


def port_stats() -> dict:
    store.init()                 # 新库还没有 ports 表时不至于直接报错（幂等）
    with store.connect() as con:
        by = con.execute("SELECT status, COUNT(*) n FROM ports GROUP BY status").fetchall()
        fam = con.execute("SELECT family, COUNT(*) n FROM ports "
                          "GROUP BY family ORDER BY n DESC").fetchall()
        top = con.execute(
            "SELECT sid,title,author,kind,family,score,risk,status FROM ports "
            "WHERE status='pending' ORDER BY score DESC, agree DESC LIMIT 200").fetchall()
    return {"by_status": {r["status"]: r["n"] for r in by},
            "by_family": {r["family"]: r["n"] for r in fam},
            "top": [dict(r) for r in top]}


def main(argv: list[str] | None = None) -> int:
    import argparse
    ap = argparse.ArgumentParser(description="Pine 移植分诊")
    ap.add_argument("--top", type=int, default=20, help="打印前 N 个待移植候选")
    args = ap.parse_args(argv)
    triage()
    st = port_stats()
    print("\n状态分布:", st["by_status"])
    print("家族分布:", st["by_family"])
    print(f"\n待移植候选（前 {args.top}）：")
    for i, r in enumerate(st["top"][:args.top], 1):
        flag = "⚠" if r["risk"] else " "
        print(f"{i:>3}. [{r['score']:>5}] {flag} {r['kind']:<9} {r['family']:<11} "
              f"{(r['title'] or '')[:44]:<46} @{r['author']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
