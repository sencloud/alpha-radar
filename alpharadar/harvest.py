"""TradingView 开源策略采集（公开 HTTP 接口，非浏览器自动化）。

三条通道（都实测过）：
  1) 搜索  pubscripts-suggest-json?search=<关键词>       —— 按关键词找「热门」脚本
  2) 脚本流 api/v1/scripts/                            —— 「最新发布」脚本，分页 1000 条
  3) 论坛  api/v1/ideas/                               —— 社区帖子；脚本型帖子含 script_id_part
  源码统一走 pine-facade.tradingview.com/pine-facade/get/<script_id_part>/1/

实测数据（2026-10-06）：
  - 搜索通道：40 个关键词 -> 约 1180 个唯一脚本，开源 233 个
  - 脚本流：1000 条（994 个唯一），其中 script_access=1（开源）779 个
  - 论坛帖：996 条，**0 条含代码块、0 条带 script_id_part** —— 都是看盘分析，
    没有源码。通道保留并记录产出，一旦出现代码块会自动入库。

只采集 `scriptAccess` 标记为 open 的脚本；受保护（闭源）脚本拿不到源码，直接跳过。
采集结果落盘为 corpus/sources/*.pine + corpus/index.json，供策略移植与溯源。

注意：TradingView 页面的脚本版权归原作者所有，本工具仅用于研究检索，
移植到本仓库的策略请在文件头保留原作者与许可声明。
"""

from __future__ import annotations

import html as html_lib
import json
import re
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

import requests

from .config import CORPUS_DIR

SUGGEST = "https://www.tradingview.com/pubscripts-suggest-json/?search={}"
FACADE = "https://pine-facade.tradingview.com/pine-facade/get/{}/1/"
SCRIPTS_FEED = "https://www.tradingview.com/api/v1/scripts/"
IDEAS_FEED = "https://www.tradingview.com/api/v1/ideas/"
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


# ==================== 脚本流 / 论坛 ====================
def api_pages(endpoint: str, pages: int = 50, per_page: int = 20,
              pause: float = 0.2, progress=None) -> list[dict]:
    """翻页拉取 TradingView 的 v1 JSON 列表接口（每页固定 20 条）。"""
    out: list[dict] = []
    for p in range(1, pages + 1):
        try:
            r = requests.get(endpoint, headers={**HEADERS, "Accept": "application/json"},
                             params={"page": p}, timeout=25)
        except requests.RequestException:
            break
        if r.status_code != 200:
            break
        try:
            res = r.json().get("results") or []
        except ValueError:
            break
        if not res:
            break
        out += res
        if progress and p % 10 == 0:
            progress(f"    {endpoint.rsplit('/', 2)[-2]} 第 {p} 页，累计 {len(out)} 条")
        time.sleep(pause)
    return out


_CODE_RE = re.compile(r"<(?:pre|code)[^>]*>(.*?)</(?:pre|code)>", re.I | re.S)
_PINE_HINT = re.compile(r"//@version|indicator\(|strategy\(|study\(|plot\(", re.I)


def extract_pine_blocks(text: str, min_len: int = 40) -> list[str]:
    """从论坛帖子正文（HTML）里抠出 Pine 代码块。

    实测最新的近千条帖子都没有代码块，但这个通道保留：论坛里偶尔有人直接贴源码，
    而且帖子转成脚本发布后会同时出现在脚本流里。
    """
    blocks = []
    for m in _CODE_RE.finditer(text or ""):
        code = html_lib.unescape(re.sub(r"<[^>]+>", "", m.group(1))).strip()
        if len(code) >= min_len and _PINE_HINT.search(code):
            blocks.append(code)
    return blocks


def _feed_record_to_script(rec: dict, kind: str = "") -> Script | None:
    sid = rec.get("script_id_part")
    if not sid:
        return None
    user = rec.get("user") or {}
    stype = rec.get("script_type") or kind
    return Script(id=sid, slug=rec.get("image_url", "") or str(rec.get("id", "")),
                  title=rec.get("name", ""), author=user.get("username", ""),
                  agree=int(rec.get("likes_count", 0) or 0),
                  editors_pick=bool(rec.get("is_picked")),
                  kind=stype if stype in ("strategy", "library") else "study")


def _safe(name: str) -> str:
    return re.sub(r"[^\w\u4e00-\u9fff-]+", "_", name).strip("_")[:60] or "script"


def harvest(terms: tuple[str, ...] = DEFAULT_TERMS, per_term: int = 200,
            max_fetch: int = 300, out_dir: Path | None = None,
            pause: float = 0.35, progress=print,
            channels: tuple[str, ...] = ("search", "feed", "forum"),
            feed_pages: int = 50, forum_pages: int = 50,
            stats: dict | None = None) -> list[Script]:
    """采集开源脚本并落盘，返回索引。

    channels 选通道：search（关键词搜索）/ feed（最新脚本流）/ forum（论坛帖）。
    max_fetch 限制**每次新增下载**的数量（已有文件不重复下载），所以语料库会
    在连续几轮里逐步补齐，而不是一轮打满。
    """
    out = Path(out_dir or CORPUS_DIR)
    src_dir = out / "sources"
    src_dir.mkdir(parents=True, exist_ok=True)
    st = stats if stats is not None else {}
    pending: dict[str, str] = {}        # sid -> 源码（论坛正文里抠出来的，免下载）
    st.update({"search": 0, "feed": 0, "forum": 0, "forum_snippets": 0})

    seen: dict[str, Script] = {}
    # ---------- 通道 1：关键词搜索（热门脚本） ----------
    if "search" in channels:
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
                rec.terms.append(term)
                rec.agree = max(rec.agree, int(it.get("agreeCount", 0) or 0))
            time.sleep(pause)
        st["search"] = len(seen)
        progress(f"搜索通道：{len(terms)} 个关键词，命中 {len(seen)} 个唯一脚本")

    # ---------- 通道 2：最新脚本流（补齐搜索漏掉的近期发布） ----------
    if "feed" in channels:
        n0 = len(seen)
        for it in api_pages(SCRIPTS_FEED, feed_pages, progress=progress):
            if str(it.get("script_access")) != "1":     # 1 = 开源
                continue
            rec = _feed_record_to_script(it)
            if rec:
                seen.setdefault(rec.id, rec)
        st["feed"] = len(seen) - n0
        progress(f"脚本流通道：新增 {st['feed']} 个候选（累计 {len(seen)}）")

    # ---------- 通道 3：论坛帖（脚本型帖子 + 正文代码块） ----------
    if "forum" in channels:
        n0 = len(seen)
        for it in api_pages(IDEAS_FEED, forum_pages, progress=progress):
            if it.get("script_id_part") and str(it.get("script_access")) == "1":
                rec = _feed_record_to_script(it)
                if rec:
                    seen.setdefault(rec.id, rec)
                continue
            for j, code in enumerate(extract_pine_blocks(it.get("description") or "")):
                sid = f"IDEA;{it.get('id')}-{j}"
                user = it.get("user") or {}
                seen.setdefault(sid, Script(
                    id=sid, slug=f"idea{it.get('id')}", title=it.get("name", ""),
                    author=user.get("username", ""),
                    agree=int(it.get("likes_count", 0) or 0),
                    kind="idea-snippet", access="open_forum"))
                pending[sid] = code
        st["forum"] = len(seen) - n0
        st["forum_snippets"] = len(pending)
        progress(f"论坛通道：新增 {st['forum']} 个候选，其中正文代码块 {len(pending)} 段")

    # ---------- 落盘：已有文件不重复下载，max_fetch 限制新增下载数 ----------
    ranked = sorted(seen.values(), key=lambda r: -r.agree)
    got: list[Script] = []
    downloaded = 0
    for i, rec in enumerate(ranked, 1):
        path = src_dir / f"{rec.slug or rec.id}_{_safe(rec.title)}.pine"
        if not path.exists():
            if rec.id in pending:
                path.write_text(pending[rec.id], encoding="utf-8")
            else:
                if downloaded >= max_fetch:
                    continue                    # 本轮配额用完，下一轮继续
                info = fetch_source(rec.id)
                time.sleep(pause)
                if info is None:
                    continue
                path.write_text(info["source"], encoding="utf-8")
                rec.access = info["access"]
                downloaded += 1
                if downloaded % 25 == 0:
                    progress(f"  本轮已下载 {downloaded}/{max_fetch} …")
        rec.file = path.name
        rec.lines = len(path.read_text(encoding="utf-8").splitlines())
        # 能落盘的一定是开源脚本（闭源脚本 pine-facade 不返回 source）；
        # 旧文件复用时拿不到 access，补一个默认值，否则列表按 access 过滤会漏掉它们
        if not rec.access:
            rec.access = "open_no_auth"
        got.append(rec)

    st["downloaded"] = downloaded
    st["total"] = len(got)
    (out / "index.json").write_text(
        json.dumps([asdict(s) for s in got], ensure_ascii=False, indent=1),
        encoding="utf-8")
    op = [s for s in got if s.is_open]
    progress(f"语料库更新：{len(got)} 个脚本（开源 {len(op)}，本轮新下载 {downloaded}）"
             f" -> {src_dir}")
    return got


def load_index(out_dir: Path | None = None) -> list[Script]:
    f = Path(out_dir or CORPUS_DIR) / "index.json"
    if not f.exists():
        return []
    return [Script(**d) for d in json.loads(f.read_text(encoding="utf-8"))]


def top_open(n: int = 30, out_dir: Path | None = None) -> list[Script]:
    idx = [s for s in load_index(out_dir) if s.is_open]
    return sorted(idx, key=lambda s: -s.agree)[:n]
