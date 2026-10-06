"""TradingView 开源策略采集（公开 HTTP 接口，非浏览器自动化）。

两个公开接口：
  pubscripts-suggest-json?search=<关键词>  -> 脚本清单
  pine-facade.tradingview.com/pine-facade/get/<id>/1/ -> 完整 Pine 源码 + 开源标记

只采集 `scriptAccess` 标记为 open 的脚本；受保护（闭源）脚本拿不到源码，直接跳过。
采集结果落盘为 corpus/sources/*.pine + corpus/index.json，供策略移植与溯源。

注意：TradingView 页面的脚本版权归原作者所有，本工具仅用于研究检索，
移植到本仓库的策略请在文件头保留原作者与许可声明。
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

import requests

from .config import CORPUS_DIR

SUGGEST = "https://www.tradingview.com/pubscripts-suggest-json/?search={}"
FACADE = "https://pine-facade.tradingview.com/pine-facade/get/{}/1/"
HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"),
    "Accept-Language": "en-US,en;q=0.9",
}

# 覆盖日内交易常见策略族；可用 --terms 覆盖
DEFAULT_TERMS = (
    "supertrend", "vwap", "opening range breakout", "orb", "squeeze",
    "scalping", "intraday", "momentum", "breakout", "mean reversion",
    "rsi divergence", "ema crossover", "macd", "bollinger", "atr trailing",
    "session", "pivot points", "donchian", "keltner", "chandelier",
    "adx", "volume", "order block", "liquidity", "smart money",
    "turtle", "half trend", "ut bot", "range filter", "trend following",
    "reversal", "pullback", "golden cross", "stochastic", "cci",
    "opening range", "gap", "noise", "volatility", "heikin ashi",
)


@dataclass
class Script:
    id: str
    slug: str = ""
    title: str = ""
    author: str = ""
    agree: int = 0
    editors_pick: bool = False
    kind: str = ""                # study / strategy / indicator
    access: str = ""              # open_no_auth = 开源
    lines: int = 0
    file: str = ""
    terms: list[str] = field(default_factory=list)

    @property
    def is_open(self) -> bool:
        return self.access.startswith("open")


def _get(url: str, tries: int = 3, timeout: int = 25) -> requests.Response | None:
    for i in range(tries):
        try:
            r = requests.get(url, headers=HEADERS, timeout=timeout)
            if r.status_code == 200:
                return r
        except requests.RequestException:
            pass
        time.sleep(0.6 * (i + 1))
    return None


def search(term: str, limit: int = 200) -> list[dict]:
    r = _get(SUGGEST.format(requests.utils.quote(term)))
    if r is None:
        return []
    try:
        return r.json().get("results", [])[:limit]
    except ValueError:
        return []


def fetch_source(script_id: str) -> dict | None:
    """取 Pine 源码；闭源脚本返回 None。"""
    r = _get(FACADE.format(script_id))
    if r is None:
        return None
    try:
        js = r.json()
    except ValueError:
        return None
    src = (js.get("source") or "").strip()
    if not src:
        return None
    return {"source": src, "access": js.get("scriptAccess", ""),
            "name": js.get("scriptName", ""), "created": js.get("created", "")}


def _safe(name: str) -> str:
    return re.sub(r"[^\w\u4e00-\u9fff-]+", "_", name).strip("_")[:60] or "script"


def harvest(terms: tuple[str, ...] = DEFAULT_TERMS, per_term: int = 200,
            max_fetch: int = 300, out_dir: Path | None = None,
            pause: float = 0.35, progress=print) -> list[Script]:
    """采集开源脚本并落盘，返回索引。重复调用只增量补充。"""
    out = Path(out_dir or CORPUS_DIR)
    src_dir = out / "sources"
    src_dir.mkdir(parents=True, exist_ok=True)

    seen: dict[str, Script] = {}
    for term in terms:
        for it in search(term, per_term):
            sid = it.get("scriptIdPart")
            if not sid:
                continue
            rec = seen.setdefault(sid, Script(
                id=sid, slug=it.get("imageUrl", ""), title=it.get("title", ""),
                author=(it.get("author") or {}).get("username", ""),
                agree=int(it.get("agreeCount", 0) or 0),
                editors_pick=bool(it.get("editorsPick")),
                kind=(it.get("extra") or {}).get("kind", "")))
            if term not in rec.terms:
                rec.terms.append(term)
            rec.agree = max(rec.agree, int(it.get("agreeCount", 0) or 0))
        time.sleep(pause)
    progress(f"搜索完成：{len(terms)} 个关键词，命中 {len(seen)} 个唯一脚本")

    ranked = sorted(seen.values(), key=lambda r: -r.agree)[:max_fetch]
    got: list[Script] = []
    for i, rec in enumerate(ranked, 1):
        path = src_dir / f"{rec.slug or rec.id}_{_safe(rec.title)}.pine"
        if not path.exists():
            info = fetch_source(rec.id)
            time.sleep(pause)
            if info is None:
                continue
            path.write_text(info["source"], encoding="utf-8")
            rec.access = info["access"]
        rec.file = path.name
        rec.lines = len(path.read_text(encoding="utf-8").splitlines())
        got.append(rec)
        if i % 25 == 0:
            progress(f"  抓取 {i}/{len(ranked)} …")

    (out / "index.json").write_text(
        json.dumps([asdict(s) for s in got], ensure_ascii=False, indent=1),
        encoding="utf-8")
    op = [s for s in got if s.is_open]
    progress(f"语料库更新：{len(got)} 个脚本（开源 {len(op)}）-> {src_dir}")
    return got


def load_index(out_dir: Path | None = None) -> list[Script]:
    f = Path(out_dir or CORPUS_DIR) / "index.json"
    if not f.exists():
        return []
    return [Script(**d) for d in json.loads(f.read_text(encoding="utf-8"))]


def top_open(n: int = 30, out_dir: Path | None = None) -> list[Script]:
    idx = [s for s in load_index(out_dir) if s.is_open]
    return sorted(idx, key=lambda s: -s.agree)[:n]
