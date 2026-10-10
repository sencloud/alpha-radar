"""alpha-radar 轻量 Web 控制台。

只用标准库（http.server），不引入 Flask/FastAPI —— 服务器上少一个依赖就少一类故障。

路由：
    GET  /              落地页（项目介绍 + 封面 + 策略表 + 回测表单 + 最近报告）
    POST /run           提交回测任务（后台线程执行），跳转到任务页
    GET  /job/<id>      任务进度页（运行中自动刷新，完成后跳报告）
    GET  /report/<名称> 查看已生成的 HTML 报告
    GET  /api/health    健康检查（供 Caddy / 监控用）
    GET  /api/strategies 策略清单 JSON
    GET  /api/falsification  证伪档案（五道闸门判定结果，只读、免鉴权；
                         契约见 docs/falsification-export.md）。只读
                         alpharadar-falsify.timer 预生成的文件，绝不在请求里判定；
                         文件还没生成时 503 {"error": "not_ready"}
    GET  /cover.png     推广封面
    POST /runs/trigger   手动扫描一轮（需管理口令）
    POST /scripts/harvest 手动采集一次（需管理口令）

安全与配额（公开站点必须考虑）：
    - 品种默认只允许 universe.PRESETS 里的白名单；设 ALPHARADAR_ALLOW_ANY=1 才放开
    - 策略只允许注册表里的 key
    - 全局同一时刻只跑一个任务，且两次任务之间有冷却
    - 报告文件名做白名单校验，禁止路径穿越
    - 会触发后台任务的管理 POST（/runs/trigger、/scripts/harvest）必须带
      ALPHARADAR_ADMIN_TOKEN：请求头 X-Admin-Token / Authorization: Bearer，
      或表单字段 token。环境变量没配时一律拒绝（fail closed）
"""

from __future__ import annotations

import hmac
import html
import json
import logging
import os
import re
import threading
import time
import traceback
import uuid
from email.utils import formatdate
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from . import __version__, config
from .pipeline import catalog, run_one, save_result
from . import store
from .universe import PRESETS

PORT = int(os.environ.get("ALPHARADAR_PORT", "8901"))
HOST = os.environ.get("ALPHARADAR_HOST", "127.0.0.1")
ALLOW_ANY = os.environ.get("ALPHARADAR_ALLOW_ANY", "0") == "1"
COOLDOWN = float(os.environ.get("ALPHARADAR_COOLDOWN", "10"))
FREQS = ("1d", "5min", "15min", "30min", "60min", "1min")

_lock = threading.Lock()
_jobs: dict[str, dict] = {}
_last_run = 0.0
NAME_RE = re.compile(r"^[A-Za-z0-9._\-]+$")

log = logging.getLogger("alpharadar.web")
ADMIN_ENV = "ALPHARADAR_ADMIN_TOKEN"
MAX_FORM_BYTES = 64 * 1024
# 预生成文件所在目录（默认 data/；测试里 monkeypatch 这个变量）
FALSIFY_DIR: Path = config.DATA_DIR
FALSIFY_FILES = {False: "falsification.json", True: "falsification.insufficient.json"}
_fx_cache: dict[tuple, tuple[tuple, bytes]] = {}
_fx_lock = threading.Lock()
_FX_CACHE_MAX = 16


def admin_token() -> str:
    """每次请求时读环境变量（改了 .env 重启即可，测试也能直接 monkeypatch）。"""
    return os.environ.get(ADMIN_ENV, "").strip()


def check_admin(headers, form: dict) -> tuple[bool, str]:
    """校验管理口令，返回 (是否放行, 拒绝原因)。

    口令来源（任一）：X-Admin-Token 头、Authorization: Bearer <token>、表单字段 token。
    未配置 ALPHARADAR_ADMIN_TOKEN 时 fail closed：一律拒绝并记警告。
    """
    want = admin_token()
    if not want:
        log.warning("%s 未配置，管理 POST 已拒绝（fail closed）", ADMIN_ENV)
        print(f"[warn] {ADMIN_ENV} 未配置，管理 POST 已拒绝", flush=True)
        return False, "服务端未配置管理口令，管理操作已停用"
    got = (headers.get("X-Admin-Token") or "").strip()
    if not got:
        auth = headers.get("Authorization") or ""
        if auth[:7].lower() == "bearer ":
            got = auth[7:].strip()
    if not got:
        got = ((form.get("token") or [""])[0]).strip()
    if got and hmac.compare_digest(got.encode("utf-8"), want.encode("utf-8")):
        return True, ""
    return False, "管理口令错误或缺失"


def falsification_json(include_insufficient: bool = False,
                       limit: int | None = None) -> tuple[bytes, os.stat_result] | None:
    """/api/falsification 的响应体：只读预生成文件，绝不现场判定。

    全表判定（11 万+ 条）要几分钟、上百 MB 内存，曾把线上 web 进程拖到 1.1 GB；
    现在由 alpharadar-falsify.timer 定时跑 `alpharadar falsify-pregen` 原子写文件。
    文件不存在返回 None（上层回 503）。按文件 mtime 缓存，文件一换自动失效。
    limit 只在比文件里的自动条目少时才解析 JSON 截断（文件本身已按
    ALPHARADAR_FALSIFY_LIMIT 截过，体积有界）。
    """
    path = Path(FALSIFY_DIR) / FALSIFY_FILES[bool(include_insufficient)]
    try:
        st = path.stat()
    except OSError:
        return None
    sig = (str(path), st.st_mtime_ns, st.st_size)
    key = (bool(include_insufficient), limit)
    with _fx_lock:
        hit = _fx_cache.get(key)
        if hit and hit[0] == sig:
            return hit[1], st
    try:
        body = path.read_bytes()
    except OSError:
        return None
    if limit is not None:
        data = json.loads(body)
        auto = [e for e in data.get("archive", []) if not e.get("curated")]
        if len(auto) > limit:
            cur = [e for e in data["archive"] if e.get("curated")]
            sm = data.setdefault("summary", {})
            sm["truncated"] = int(sm.get("truncated") or 0) + len(auto) - limit
            data["archive"] = cur + auto[:limit]
            sm["auto"], sm["archive_total"] = limit, len(data["archive"])
            body = json.dumps(data, ensure_ascii=False, allow_nan=False).encode("utf-8")
    with _fx_lock:
        if len(_fx_cache) >= _FX_CACHE_MAX:
            _fx_cache.clear()
        _fx_cache[key] = (sig, body)
    return body, st


# ==================== 页面 ====================
CSS = """
:root{--bg:#0f1115;--panel:#171a21;--line:#232833;--fg:#e6e6e6;--mut:#8b93a1;
--up:#31c48d;--down:#f05252;--acc:#f0b429}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--fg);font:15px/1.65 -apple-system,
'Segoe UI','Microsoft YaHei',sans-serif}
.wrap{max-width:1000px;margin:0 auto;padding:36px 22px 80px}
h1{font-size:30px;margin:0 0 6px}
h2{font-size:17px;margin:34px 0 10px;color:#c9d1d9}
.sub{color:var(--mut);margin-bottom:24px}
a{color:var(--acc);text-decoration:none}a:hover{text-decoration:underline}
img.cover{width:100%;border-radius:10px;border:1px solid var(--line);margin:12px 0 8px}
table{border-collapse:collapse;width:100%;font-size:14px}
th,td{padding:8px 10px;border-bottom:1px solid var(--line);text-align:left}
th{color:var(--mut);font-weight:500;background:#14171d}
code{background:#1d2129;padding:2px 6px;border-radius:4px;font-size:13px}
form{background:var(--panel);border:1px solid var(--line);border-radius:10px;
padding:16px;display:flex;flex-wrap:wrap;gap:12px;align-items:flex-end}
label{display:block;color:var(--mut);font-size:12px;margin-bottom:4px}
select,input{background:#0f1115;color:var(--fg);border:1px solid var(--line);
border-radius:6px;padding:8px 10px;font-size:14px;min-width:150px}
button{background:var(--acc);color:#1a1a1a;border:0;border-radius:6px;
padding:9px 20px;font-size:14px;font-weight:600;cursor:pointer}
button:disabled{opacity:.5;cursor:not-allowed}
.note{color:var(--mut);font-size:13px}
.warn{border-left:3px solid var(--acc);padding-left:12px;color:var(--mut);font-size:13px}
.nav{display:flex;gap:18px;margin-bottom:26px;padding-bottom:12px;
border-bottom:1px solid var(--line);font-size:14px}
.cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(140px,1fr));gap:10px;
margin:10px 0 6px}
.card{background:var(--panel);border:1px solid var(--line);border-radius:10px;
padding:11px 14px}
.card .k{color:var(--mut);font-size:12px}
.card .v{font-size:19px;font-weight:600;margin-top:3px}
.up{color:var(--up)}.down{color:var(--down)}
form.filters{background:transparent;border:0;padding:0;margin:10px 0}
pre.code{counter-reset:line;background:#0b0d11;border:1px solid var(--line);
border-radius:8px;padding:12px 0;overflow:auto;max-height:72vh;margin:8px 0 20px;
font:12.5px/1.6 Consolas,'Cascadia Mono',Menlo,monospace;tab-size:4}
pre.code .ln{display:block;counter-increment:line;padding-left:58px;
position:relative;white-space:pre;color:#c9d1d9}
pre.code .ln::before{content:counter(line);position:absolute;left:0;width:44px;
text-align:right;color:#4a5160;padding-right:10px;user-select:none}
.c-com{color:#6b7a90;font-style:italic}
.c-str{color:#7ec699}
.c-num{color:#f0b429}
.c-kw{color:#c792ea}
.c-ns{color:#4ea3ff}
"""


def _page(title: str, body: str, refresh: str = "") -> bytes:
    meta = f'<meta http-equiv="refresh" content="{refresh}">' if refresh else ""
    nav = ('<div class="nav"><a href="/">首页</a>'
           '<a href="/scripts">策略库</a>'
           '<a href="/runs">状态与历史</a>'
           '<a href="/api/health">health</a>'
           '<a href="https://github.com/sencloud/alpha-radar">GitHub</a></div>')
    return f"""<!doctype html><html lang="zh"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">{meta}
<title>{html.escape(title)}</title><style>{CSS}</style></head>
<body><div class="wrap">{nav}{body}</div></body></html>""".encode("utf-8")


def _index() -> bytes:
    opts = "".join(f'<option value="{k}">{html.escape(v.name)}</option>'
                   for k, v in PRESETS.items())
    freqs = "".join(f'<option value="{f}"{" selected" if f == "5min" else ""}>{f}</option>'
                    for f in FREQS)
    rows = "".join(
        f"<tr><td>{html.escape(s.key)}</td><td>{html.escape(s.name)}</td>"
        f"<td>{html.escape(s.source)}</td><td>{html.escape(s.license)}</td></tr>"
        for s in catalog())
    reports = sorted(config.REPORT_DIR.glob("*.html"),
                     key=lambda p: -p.stat().st_mtime)[:12]
    rlist = "".join(
        f'<li><a href="/report/{html.escape(p.name)}">{html.escape(p.stem)}</a>'
        f' <span class="note">{time.strftime("%m-%d %H:%M", time.localtime(p.stat().st_mtime))}</span></li>'
        for p in reports) or "<li class='note'>还没有报告</li>"
    body = f"""
<h1>alpha-radar · 策略雷达</h1>
<div class="sub">持续采集 TradingView 开源策略，在 A 股 / 期货上做含成本回测与证伪。
数据源 Tushare · v{__version__}</div>
<img class="cover" src="/cover.png" alt="alpha-radar">
<div class="warn">这个项目不是策略生成器，是策略证伪器。回测已含双边成本与滑点，
但<b>不含</b>冲击成本、涨跌停无法成交等实盘约束；历史表现不代表未来收益。</div>

<h2>跑一次回测</h2>
<form method="post" action="/run">
  <div><label>品种</label><select name="symbol">{opts}</select></div>
  <div><label>策略</label><select name="strategy">
  {''.join(f'<option value="{s.key}">{html.escape(s.name)}</option>' for s in catalog())}
  </select></div>
  <div><label>周期</label><select name="freq">{freqs}</select></div>
  <div><label>起始日期</label><input name="start" value="20220101" size="10"></div>
  <div><button type="submit">回测</button></div>
</form>
<p class="note">期货分钟线需要 Tushare 的 ft_mins 权限；A 股日线权限门槛最低。
首次拉数据较慢，之后走缓存。</p>

<h2>内置策略</h2>
<table><thead><tr><th>key</th><th>名称</th><th>出处</th><th>许可</th></tr></thead>
<tbody>{rows}</tbody></table>

<h2>最近的报告</h2><ul>{rlist}</ul>

<h2>说明</h2>
<p class="note">源码：<a href="https://github.com/sencloud/alpha-radar">
github.com/sencloud/alpha-radar</a>
&nbsp;·&nbsp; <a href="/api/health">/api/health</a>
&nbsp;·&nbsp; <a href="/api/strategies">/api/strategies</a>
&nbsp;·&nbsp; <a href="/api/falsification">/api/falsification</a></p>
"""
    return _page("alpha-radar · 策略雷达", body)


def _job_page(job: dict) -> bytes:
    """任务进度页。"""
    if job["state"] == "done":
        link = (f"<p><a href='/report/{html.escape(job['report'])}'>查看报告 →</a></p>"
                if job.get("report") else "")
        back = ("<p><a href='/scripts'>← 返回策略库</a></p>" if not job.get("report")
                else "<p><a href='/'>← 返回首页</a></p>")
        return _page("完成", f"""<h1>完成</h1>{link}
<p class="note">{html.escape(job.get('summary', ''))}</p>{back}""")
    if job["state"] == "error":
        return _page("失败", f"""<h1>回测失败</h1>
<pre class="note">{html.escape(job.get('error', ''))}</pre>
<p><a href="/">← 返回首页</a></p>""")
    return _page("运行中", f"""<h1>回测运行中…</h1>
<p class="note">{html.escape(job['label'])}</p>
<p class="note">首次拉取数据较慢（期货 1 分钟线可能要数分钟），请稍候。</p>
<p><a href="/">← 返回首页</a></p>""", refresh="4")


# ==================== 策略语料库 ====================
# Pine 语法着色：先分词再逐段转义，避免转义后再正则匹配到实体
_PINE_TOKEN = re.compile(
    r'(?P<comment>//[^\n]*)'
    r'|(?P<string>"(?:[^"\\]|\\.)*")'
    r'|(?P<number>\b\d+(?:\.\d+)?\b)'
    r'|(?P<kw>\b(?:if|else|for|to|by|while|var|varip|float|int|bool|string|'
    r'color|true|false|na|and|or|not|import|export|type|method|switch|break|'
    r'continue|return|series|simple|const)\b)'
    r'|(?P<ns>\b(?:ta|math|str|array|matrix|map|request|input|plot|plotshape|'
    r'plotchar|plotcandle|hline|fill|label|line|box|table|strategy|indicator|'
    r'study|alertcondition|barstate|syminfo|timeframe|color|dayofweek|hour)\b)(?=\.)'
    r'|(?P<fn>\b(?:plot|plotshape|plotchar|plotcandle|hline|fill|label|line|box|'
    r'table|strategy|indicator|study|input|alertcondition|alert|nz|na)\b)(?=\()'
)
_PINE_CLASS = {"comment": "c-com", "string": "c-str", "number": "c-num",
               "kw": "c-kw", "ns": "c-ns", "fn": "c-ns"}


def _highlight_pine(src: str) -> str:
    parts, pos = [], 0
    for m in _PINE_TOKEN.finditer(src):
        parts.append(html.escape(src[pos:m.start()]))
        cls = _PINE_CLASS.get(m.lastgroup, "")
        parts.append(f'<span class="{cls}">{html.escape(m.group())}</span>')
        pos = m.end()
    parts.append(html.escape(src[pos:]))
    body = "".join(parts)
    lines = body.split("\n")
    return "\n".join(f'<span class="ln">{ln or " "}</span>' for ln in lines)


def _script_url(rec: dict) -> str:
    slug = rec.get("slug") or ""
    return f"https://www.tradingview.com/script/{slug}/" if slug else \
        "https://www.tradingview.com/scripts/"


def _scripts_page(q: dict) -> bytes:
    """采集到的开源策略清单：搜索 / 按类型筛选 / 排序 / 分页。"""
    qs = (q.get("q") or [""])[0].strip()
    kind = (q.get("kind") or [""])[0]
    sort = (q.get("sort") or ["agree"])[0]
    page = max(1, int((q.get("page") or ["1"])[0] or 1))
    per = 60

    store.init()
    rows, total = store.list_scripts(kind=kind, q=qs, sort=sort,
                                     limit=per, offset=(page - 1) * per)
    kinds = store.script_kinds()
    hv = (store.get_state("harvest") or {})
    stats = store.script_stats()
    pages = max(1, (total + per - 1) // per)

    def _ch(hv_state: dict, key: str) -> str:
        """读取 harvest 状态里的通道计数。"""
        return str(((hv_state.get("value") or {}).get("channels") or {}).get(key, "—"))

    def qs_with(**kw):
        base = {"q": qs, "kind": kind, "sort": sort, "page": page}
        base.update(kw)
        return "&".join(f"{k}={html.escape(str(v))}" for k, v in base.items() if v)

    body_rows = "".join(
        f"<tr><td><a href='/script/{html.escape(r['sid'].split(';')[-1])}'>"
        f"{html.escape(r['title'] or '(无题)')}</a></td>"
        f"<td class='note'>{html.escape(r['author'] or '')}</td>"
        f"<td>{r['agree']:,}</td><td>{html.escape(r['kind'] or '')}</td>"
        f"<td>{r['lines'] or 0}</td>"
        f"<td class='note'>{html.escape(str(r['first_seen'] or '')[:10])}</td>"
        f"<td><a href='{_script_url(r)}' target='_blank' rel='noopener'>TV</a></td></tr>"
        for r in rows) or "<tr><td colspan='7' class='note'>没有匹配的脚本</td></tr>"

    opts = "".join(f'<option value="{k}"{" selected" if k == kind else ""}>{k}</option>'
                   for k in kinds)
    sorts = [("agree", "按点赞"), ("lines", "按行数"), ("new", "按首次采集"),
             ("seen", "按最近更新"), ("title", "按标题")]
    sopts = "".join(f'<option value="{k}"{" selected" if k == sort else ""}>{v}</option>'
                    for k, v in sorts)
    pager = " ".join(
        f"<a href='/scripts?{qs_with(page=p)}'>{'[' + str(p) + ']' if p == page else p}</a>"
        for p in range(max(1, page - 3), min(pages, page + 3) + 1))

    body = f"""
<h1>策略语料库</h1>
<div class="sub">调度器每轮都会增量采集 TradingView 的<b>开源</b> Pine 脚本
（闭源脚本拿不到源码，不入库）。三条通道并行：
<b>关键词搜索</b>（热门脚本）、<b>脚本流</b>（最新发布）、<b>论坛帖</b>（社区帖子）。
这里可以搜索、按类型筛选、点开看完整源码。</div>

<div class="cards">
  <div class="card"><div class="k">开源脚本</div><div class="v">{stats.get('total') or 0}</div></div>
  <div class="card"><div class="k">strategy 类型</div><div class="v">{stats.get('strat') or 0}</div></div>
  <div class="card"><div class="k">study 类型</div><div class="v">{stats.get('study') or 0}</div></div>
  <div class="card"><div class="k">上次采集新增</div><div class="v">{hv.get('value', {}).get('new', '—')}</div></div>
</div>
<p class="note">最近采集：{html.escape(str(hv.get('ts') or '—'))}
&nbsp;·&nbsp; 通道产出：搜索 {_ch(hv, 'search')} / 脚本流 {_ch(hv, 'feed')} /
论坛 {_ch(hv, 'forum')}（正文代码块 {_ch(hv, 'forum_snippets')}）
&nbsp;·&nbsp; 本轮新下载 {_ch(hv, 'downloaded')}
&nbsp;·&nbsp; 采集由系统定时任务驱动；也可以手动补一次：
<form method="post" action="/scripts/harvest" style="display:inline">
<input type="password" name="token" placeholder="管理口令" autocomplete="current-password"
 style="min-width:110px;padding:4px 8px;font-size:13px">
<button type="submit" style="padding:4px 12px;font-size:13px">立即采集</button></form></p>

<form class="filters" method="get" action="/scripts">
  <label>搜索</label><input name="q" value="{html.escape(qs)}" placeholder="标题 / 作者 / ID">
  <label>类型</label><select name="kind"><option value="">全部</option>{opts}</select>
  <label>排序</label><select name="sort">{sopts}</select>
  <button type="submit">筛选</button>
  <span class="note">共 {total} 个</span>
</form>

<table><thead><tr><th>标题</th><th>作者</th><th>点赞</th><th>类型</th>
<th>行数</th><th>首次采集</th><th>原页</th></tr></thead>
<tbody>{body_rows}</tbody></table>
<p class="note">第 {page}/{pages} 页　{pager}</p>
"""
    return _page("策略语料库 · alpha-radar", body)


def _script_page(sid: str) -> bytes:
    """查看单个脚本的 Pine 源码。"""
    store.init()
    rec = store.get_script(sid)
    if rec is None:
        return _page("未找到", "<h1>未找到该脚本</h1><p><a href='/scripts'>← 返回列表</a></p>")
    # 只允许读语料库目录内的文件，且文件名来自数据库而非 URL
    fname = os.path.basename(str(rec.get("file") or ""))
    f = (config.CORPUS_DIR / "sources" / fname).resolve()
    if not fname or f.parent != (config.CORPUS_DIR / "sources").resolve() or not f.exists():
        return _page("源码缺失",
                     f"<h1>源码文件缺失</h1><p class='note'>{html.escape(fname)}</p>"
                     f"<p><a href='/scripts'>← 返回列表</a></p>")
    src = f.read_text(encoding="utf-8", errors="replace")
    body = f"""
<h1>{html.escape(rec['title'] or '(无题)')}</h1>
<div class="sub">{html.escape(rec['author'] or '未知作者')} ·
{rec['kind'] or ''} · {rec['lines'] or len(src.splitlines())} 行 ·
点赞 {rec['agree']:,} ·
<a href="{_script_url(rec)}" target="_blank" rel="noopener">TradingView 原页</a></div>
<p class="warn">本页源码来自 TradingView 公开发布的开源脚本，版权归原作者所有，
请遵循其原始许可（Pine 脚本常见 CC BY-NC-SA / MPL-2.0 / MIT）。
本项目仅用于研究检索与许可范围内的移植。</p>
<h2>Pine Script</h2>
<pre class="code"><code>{_highlight_pine(src)}</code></pre>
<p><a href="/scripts">← 返回列表</a></p>
"""
    return _page(f"{rec['title']} · Pine 源码", body)


# ==================== 任务 ====================
def _runs_page(q: dict) -> bytes:
    """状态与历史：调度状态、语料库、品种列表、结果榜、运行记录。"""
    symbol = (q.get("symbol") or [""])[0]
    strategy = (q.get("strategy") or [""])[0]
    freq = (q.get("freq") or [""])[0]
    market = (q.get("market") or [""])[0]

    store.init()
    hv = (store.get_state("harvest") or {}).get("value") or {}
    cy = (store.get_state("last_cycle") or {}).get("value") or {}
    wk = (store.get_state("worker") or {}).get("value") or {}
    ins = (store.get_state("instruments") or {}).get("value") or {}
    ts = store.task_stats()
    try:
        from .porting.triage import port_stats
        ps = port_stats()
    except Exception:
        ps = {"by_status": {}, "by_family": {}, "top": []}
    ss = store.script_stats()
    rows = store.latest_results(symbol=symbol, strategy=strategy, freq=freq,
                                market=market)
    syms = store.symbols_in_results()
    runs = store.recent_runs(20)

    def card(k, v):
        return f"<div class='card'><div class='k'>{k}</div><div class='v'>{v}</div></div>"

    cards = "".join([
        card("队列总量", f"{ts.get('total', 0):,}"),
        card("队列已完成", f"{ts.get('ok', 0):,}"),
        card("待跑", f"{ts.get('due', 0):,}"),
        card("语料库", f"{ss.get('total') or 0}"),
        card("上次采集新增", f"{hv.get('new', '—')}"),
        card("定时扫描成功", f"{cy.get('ok', '—')}"),
    ])

    total, done = ts.get("total", 0), ts.get("ok", 0)
    pct = (done / total * 100) if total else 0.0
    secs = wk.get("seconds") or 0
    rate = (wk.get("rate_per_min") or 0) / 60.0 or \
        ((wk.get("ok", 0) + wk.get("err", 0)) / secs if secs else 0)
    eta = f"{(ts.get('due', 0) / rate / 3600):.1f} 小时" if rate > 0 else "—"
    run_flag = "运行中" if wk.get("running") else ("已停止" if wk else "未启动")
    by_freq = " · ".join(f"{k} {v:,}" for k, v in (ts.get("by_freq") or {}).items())
    by_mkt = " · ".join(f"{k} {v:,}" for k, v in (ts.get("by_market") or {}).items())
    ins_line = (f"品种 {ins.get('instruments', 0):,} 个"
                f"（{by_mkt}）" if ins else "尚未同步品种表")
    queue_html = f"""
<h2>Pine 移植进度</h2>
<div class="cards">
  <div class="card"><div class="k">待移植</div><div class="v">{ps['by_status'].get('pending', 0):,}</div></div>
  <div class="card"><div class="k">已派单</div><div class="v">{ps['by_status'].get('ported', 0):,}</div></div>
  <div class="card"><div class="k">已通过校验</div><div class="v up">{ps['by_status'].get('verified', 0):,}</div></div>
  <div class="card"><div class="k">校验未过</div><div class="v down">{ps['by_status'].get('rejected', 0):,}</div></div>
</div>
<p class="note">按指标族：{' · '.join(f'{k} {v}' for k, v in list(ps['by_family'].items())[:8]) or '—'}
&nbsp;·&nbsp; 流程见 <a href="https://github.com/sencloud/alpha-radar/blob/main/docs/porting.md">docs/porting.md</a>
（分诊 → 工单 → 实现 → 机械校验 → 入队）。
<b>指标也能变策略</b>：按七个标准包装器把指标转成入场/出场规则。</p>

<h2>全市场任务队列</h2>
<div class="cards">
  <div class="card"><div class="k">总量</div><div class="v">{total:,}</div></div>
  <div class="card"><div class="k">已完成</div><div class="v up">{done:,}</div></div>
  <div class="card"><div class="k">待跑</div><div class="v">{ts.get('due', 0):,}</div></div>
  <div class="card"><div class="k">失败</div><div class="v down">{ts.get('err', 0):,}</div></div>
  <div class="card"><div class="k">完成度</div><div class="v">{pct:.1f}%</div></div>
  <div class="card"><div class="k">预计跑完</div><div class="v">{eta}</div></div>
</div>
<p class="note">{ins_line}
&nbsp;·&nbsp; 按周期：{by_freq or '—'}
&nbsp;·&nbsp; 上一轮 worker：成功 {wk.get('ok', '—')} / 失败 {wk.get('err', '—')}
{f"（{run_flag}，{secs}s，{rate * 60:.1f} 任务/分钟）" if secs else f"（{run_flag}）"}</p>
<p class="note">队列覆盖 <b>全 A 股 + 全期货品种 × 各自可用周期 × 全部策略</b>，
由常驻 worker 串行执行（速度优先让位于稳定）。成功的任务 {7} 天后自动重跑，
失败的 6 小时后重试。改范围请编辑 <code>config/universe.json</code> 的 <code>auto</code> 段。</p>
"""

    def opts(values, sel):
        o = ['<option value="">全部</option>']
        for v in values:
            o.append(f'<option value="{html.escape(str(v))}"'
                     f'{" selected" if str(v) == sel else ""}>{html.escape(str(v))}</option>')
        return "".join(o)

    all_syms = sorted({r["symbol"] for r in rows}) or sorted(
        {r["symbol"] for r in syms})
    all_strat = sorted({r["strategy"] for r in rows})
    all_freq = sorted({r["freq"] for r in rows})

    def cell(v, fmt="{}", cls_by_sign=False):
        if v is None:
            return "<td>—</td>"
        s = fmt.format(v)
        if cls_by_sign and isinstance(v, (int, float)) and v != 0:
            return f"<td class='{'up' if v > 0 else 'down'}'>{s}</td>"
        return f"<td>{s}</td>"

    body_rows = []
    name_of = {s.key: s.name for s in catalog()}
    for r in rows:
        rep = (f"<a href='/report/{html.escape(r['report'])}'>报告</a>"
               if r.get("report") else "—")
        sname = name_of.get(str(r["strategy"]), str(r["strategy"]))
        body_rows.append(
            "<tr>"
            f"<td>{html.escape(str(r['symbol']))}</td>"
            f"<td>{html.escape(str(r.get('name') or ''))}</td>"
            f"<td>{html.escape(str(r.get('market') or ''))}</td>"
            f"<td>{html.escape(sname)}</td>"
            f"<td>{html.escape(str(r['freq']))}</td>"
            + cell(r["trades"], "{:,}")
            + cell(None if r["win_rate"] is None else r["win_rate"] * 100, "{:.1f}%")
            + cell(r["pf"], "{:.2f}")
            + cell(r["avg_points"], "{:+.2f}", True)
            + cell(r["total_pnl"], "{:+,.0f}", True)
            + cell(r["max_dd"], "{:,.0f}")
            + cell(r["ret_dd"], "{:.2f}", True)
            + f"<td>{r.get('pos_years', '—')}/{r.get('years', '—')}</td>"
            f"<td class='note'>{html.escape(str(r.get('ts', ''))[5:16])}</td>"
            f"<td>{rep}</td></tr>")
    if not body_rows:
        body_rows.append("<tr><td colspan='14' class='note'>还没有结果 —— "
                         "调度器跑完第一轮后这里会出现数据。</td></tr>")

    sym_rows = "".join(
        f"<tr><td><a href='/runs?symbol={html.escape(s['symbol'])}'>"
        f"{html.escape(s['symbol'])}</a></td>"
        f"<td>{html.escape(str(s.get('name') or ''))}</td>"
        f"<td>{html.escape(str(s.get('market') or ''))}</td>"
        f"<td>{s['n']}</td>"
        f"<td class='note'>{html.escape(str(s.get('last_ts') or '')[5:16])}</td></tr>"
        for s in syms) or "<tr><td colspan='5' class='note'>暂无</td></tr>"

    run_rows = "".join(
        f"<tr><td>{r['id']}</td><td>{html.escape(str(r['kind']))}</td>"
        f"<td>{html.escape(str(r.get('started_at') or '')[5:19])}</td>"
        f"<td>{html.escape(str(r.get('finished_at') or '')[5:19] or '—')}</td>"
        f"<td>{html.escape(str(r.get('status') or ''))}</td>"
        f"<td>{r.get('n_ok', 0)}</td><td>{r.get('n_err', 0)}</td>"
        f"<td>{r.get('n_skip', 0)}</td></tr>" for r in runs
    ) or "<tr><td colspan='8' class='note'>暂无</td></tr>"

    body = f"""
<h1>状态与历史</h1>
<div class="sub">调度器在服务器上定时扫描 <code>config/universe.json</code> 里的
品种 × 周期 × 策略，结果写入 SQLite；这里读的是最新一条记录。</div>
<div class="cards">{cards}</div>
{queue_html}
<p class="note">上次采集：{html.escape(str((store.get_state('harvest') or {}).get('ts') or '—'))}
&nbsp;·&nbsp; 上次扫描：{html.escape(str((store.get_state('last_cycle') or {}).get('ts') or '—'))}
&nbsp;·&nbsp; 扫描由 systemd timer 驱动，失败会自动跳过并在运行记录里留痕</p>
<form method="post" action="/runs/trigger">
  <div><label>管理口令</label><input type="password" name="token"
   autocomplete="current-password"></div>
  <button type="submit">立即扫描一轮（限量）</button>
  <span class="note">手动触发只跑最旧的几个组合，不会打断定时任务</span>
</form>

<form class="filters" method="get" action="/runs">
  <label>品种</label><select name="symbol">{opts(all_syms, symbol)}</select>
  <label>策略</label><select name="strategy">{opts(all_strat, strategy)}</select>
  <label>周期</label><select name="freq">{opts(all_freq, freq)}</select>
  <label>市场</label><select name="market">
    <option value="">全部</option>
    <option value="futures"{" selected" if market == "futures" else ""}>期货</option>
    <option value="stocks"{" selected" if market == "stocks" else ""}>A股</option>
  </select>
  <button type="submit">筛选</button>
</form>

<h2>结果榜（按 PF 降序）</h2>
<table><thead><tr><th>品种</th><th>名称</th><th>市场</th><th>策略</th><th>周期</th>
<th>笔数</th><th>胜率</th><th>PF</th><th>均点</th><th>合计</th><th>最大回撤</th>
<th>收益回撤比</th><th>正年</th><th>时间</th><th></th></tr></thead>
<tbody>{''.join(body_rows)}</tbody></table>

<h2>品种列表</h2>
<table><thead><tr><th>品种</th><th>名称</th><th>市场</th><th>记录数</th>
<th>最近</th></tr></thead><tbody>{sym_rows}</tbody></table>

<h2>运行记录</h2>
<table><thead><tr><th>#</th><th>类型</th><th>开始</th><th>结束</th><th>状态</th>
<th>成功</th><th>失败</th><th>跳过</th></tr></thead><tbody>{run_rows}</tbody></table>

<p class="note">提醒：PF &gt; 1 不等于可交易。请同时看<b>笔数</b>（样本量）、
<b>正年数</b>（是否只靠某一年）与<b>收益回撤比</b>（是否值得做）。
详见 <a href="https://github.com/sencloud/alpha-radar/blob/main/docs/pitfalls.md">
反过拟合守则</a>。</p>
"""
    return _page("状态与历史 · alpha-radar", body)


def _run_job(job_id: str, symbol: str, strategy: str, freq: str, start: str) -> None:
    job = _jobs[job_id]
    job["state"] = "running"
    try:
        res = run_one(symbol, strategy, freq, start, verbose=False)
        _, html_path = save_result(res)
        tr = res.trades
        if tr.empty:
            summary = "无交易"
        else:
            win = tr[tr["净利"] > 0]
            gw = win["净利"].sum()
            gl = -tr[tr["净利"] <= 0]["净利"].sum()
            pf = (gw / gl) if gl > 0 else float("inf")
            summary = (f"{len(tr)} 笔 · 胜率 {len(win) / len(tr):.1%} · PF {pf:.2f}"
                       f" · 合计 {tr['净利'].sum():+,.0f} 元")
        job.update(state="done", report=html_path.name, summary=summary)
    except Exception as exc:
        job.update(state="error",
                   error=f"{type(exc).__name__}: {exc}\n\n{traceback.format_exc()[-800:]}")
    finally:
        job["finished"] = time.time()


# ==================== HTTP ====================
class Handler(BaseHTTPRequestHandler):
    server_version = f"alpha-radar/{__version__}"

    def log_message(self, fmt, *args):                 # 交给 journald
        print(f"{self.address_string()} {fmt % args}", flush=True)

    def _trigger_cycle(self) -> None:
        """手动触发一轮扫描（限量），后台执行，立即返回。"""
        limit = int(os.environ.get("ALPHARADAR_TRIGGER_LIMIT", "3"))
        try:
            from . import scheduler
        except Exception as exc:                       # 依赖缺失时不拖垮站点
            return self._send(500, _page("错误", f"<h1>调度器不可用</h1>"
                                         f"<pre class='note'>{html.escape(str(exc))}</pre>"))
        if any(j["state"] == "running" for j in _jobs.values()):
            return self._send(429, _page("忙", "<h1>已有任务在跑</h1>"
                                         "<p><a href='/runs'>← 返回</a></p>"))
        jid = uuid.uuid4().hex[:12]
        _jobs[jid] = {"state": "running", "started": time.time(),
                      "label": f"手动扫描（最多 {limit} 个组合）"}

        def worker():
            try:
                scheduler.store.init()
                scheduler.run_cycle(scheduler.load_universe(), limit=limit,
                                    out=lambda *a: print(*a, flush=True))
                _jobs[jid].update(state="done", report="", summary="扫描完成")
            except Exception as exc:
                _jobs[jid].update(state="error", error=f"{type(exc).__name__}: {exc}")

        threading.Thread(target=worker, daemon=True).start()
        self._redirect(f"/job/{jid}")

    def _trigger_harvest(self) -> None:
        """手动补采一次（与定时任务共用同一把锁，不会叠跑）。"""
        try:
            from . import scheduler
        except Exception as exc:
            return self._send(500, _page("错误", f"<h1>调度器不可用</h1>"
                                         f"<pre class='note'>{html.escape(str(exc))}</pre>"))
        if any(j["state"] == "running" for j in _jobs.values()):
            return self._send(429, _page("忙", "<h1>已有任务在跑</h1>"
                                         "<p><a href='/scripts'>← 返回</a></p>"))
        jid = uuid.uuid4().hex[:12]
        _jobs[jid] = {"state": "running", "started": time.time(),
                      "label": "采集 TradingView 开源策略"}

        def worker():
            try:
                out = scheduler.harvest_only()
                _jobs[jid].update(state="done", report="",
                                  summary=f"语料库 {out.get('total')} 个，新增 {out.get('new')}")
            except Exception as exc:
                _jobs[jid].update(state="error", error=f"{type(exc).__name__}: {exc}")

        threading.Thread(target=worker, daemon=True).start()
        self._redirect(f"/job/{jid}")

    def _send(self, code: int, body: bytes, ctype="text/html; charset=utf-8",
              headers: dict | None = None) -> None:
        self.send_response(code)
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, obj, code: int = 200) -> None:
        self._send(code, json.dumps(obj, ensure_ascii=False).encode("utf-8"),
                   "application/json; charset=utf-8")

    def _redirect(self, url: str) -> None:
        self.send_response(303)
        self.send_header("Location", url)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_GET(self) -> None:                          # noqa: N802
        u = urlparse(self.path)
        p = u.path
        if p == "/":
            return self._send(200, _index())
        if p == "/api/health":
            return self._json({"ok": True, "version": __version__,
                               "jobs": len(_jobs),
                               "last_run": _last_run or None})
        if p == "/api/strategies":
            return self._json([{"key": s.key, "name": s.name, "source": s.source,
                                "license": s.license} for s in catalog()])
        if p == "/api/falsification":
            q = parse_qs(u.query)
            inc = {x.strip() for v in q.get("include", []) for x in v.split(",")}
            lim = None
            if q.get("limit"):
                try:
                    lim = max(0, int(q["limit"][0]))
                except ValueError:
                    return self._json({"error": "limit 必须是整数"}, 400)
            try:
                got = falsification_json("insufficient" in inc, lim)
            except Exception as exc:
                return self._json({"error": f"{type(exc).__name__}: {exc}"[:300]}, 500)
            if got is None:
                body = json.dumps({"error": "not_ready",
                                   "detail": "证伪档案尚未生成，稍后再试"},
                                  ensure_ascii=False).encode("utf-8")
                return self._send(503, body, "application/json; charset=utf-8",
                                  {"Retry-After": "60", "Cache-Control": "no-store"})
            body, st = got
            etag = f'"{st.st_mtime_ns:x}-{st.st_size:x}-{lim if lim is not None else "a"}"'
            hdr = {"ETag": etag, "Last-Modified": formatdate(st.st_mtime, usegmt=True),
                   "Cache-Control": "public, max-age=60"}
            if etag in (self.headers.get("If-None-Match") or ""):
                self.send_response(304)
                for k, v in hdr.items():
                    self.send_header(k, v)
                self.end_headers()
                return None
            return self._send(200, body, "application/json; charset=utf-8", hdr)
        if p == "/runs":
            try:
                return self._send(200, _runs_page(parse_qs(u.query)))
            except Exception as exc:
                return self._send(500, _page("错误", f"<h1>读取结果库失败</h1>"
                                             f"<pre class='note'>{html.escape(str(exc))}</pre>"))
        if p == "/scripts":
            try:
                return self._send(200, _scripts_page(parse_qs(u.query)))
            except Exception as exc:
                return self._send(500, _page("错误", f"<h1>读取语料库失败</h1>"
                                             f"<pre class='note'>{html.escape(str(exc))}</pre>"))
        if p.startswith("/script/"):
            ref = p[8:]
            if not NAME_RE.match(ref):
                return self._send(400, b"bad id")
            try:
                return self._send(200, _script_page(ref))
            except Exception as exc:
                return self._send(500, _page("错误", f"<h1>读取源码失败</h1>"
                                             f"<pre class='note'>{html.escape(str(exc))}</pre>"))
        if p == "/cover.png":
            f = Path(__file__).resolve().parents[1] / "promo" / "zhihu-cover.png"
            if f.exists():
                return self._send(200, f.read_bytes(), "image/png")
            return self._send(404, b"no cover")
        if p.startswith("/job/"):
            job = _jobs.get(p[5:])
            return self._send(200, _job_page(job)) if job else self._send(404, b"no job")
        if p.startswith("/report/"):
            name = p[8:]
            if not NAME_RE.match(name) or not name.endswith(".html"):
                return self._send(400, b"bad name")
            f = (config.REPORT_DIR / name).resolve()
            if f.parent != config.REPORT_DIR.resolve() or not f.exists():
                return self._send(404, b"not found")
            return self._send(200, f.read_bytes())
        self._send(404, b"not found")

    def do_POST(self) -> None:                         # noqa: N802
        global _last_run
        p = urlparse(self.path).path
        if p not in ("/run", "/runs/trigger", "/scripts/harvest"):
            return self._send(404, b"not found")
        try:
            n = int(self.headers.get("Content-Length", 0) or 0)
        except ValueError:
            n = 0
        if n < 0 or n > MAX_FORM_BYTES:
            return self._send(413, b"payload too large")
        form = parse_qs(self.rfile.read(n).decode("utf-8", "replace")) if n else {}
        if p in ("/runs/trigger", "/scripts/harvest"):
            ok, why = check_admin(self.headers, form)
            if not ok:
                back = "/runs" if p == "/runs/trigger" else "/scripts"
                return self._send(403, _page("拒绝", f"<h1>需要管理口令</h1>"
                                             f"<p class='note'>{html.escape(why)}</p>"
                                             f"<p><a href='{back}'>← 返回</a></p>"))
            return self._trigger_cycle() if p == "/runs/trigger" else self._trigger_harvest()
        symbol = (form.get("symbol") or [""])[0]
        strategy = (form.get("strategy") or [""])[0]
        freq = (form.get("freq") or ["5min"])[0]
        start = (form.get("start") or ["20220101"])[0]

        if not ALLOW_ANY and symbol not in PRESETS:
            return self._send(400, _page("拒绝", "<h1>品种不在白名单</h1>"
                                         "<p><a href='/'>← 返回</a></p>"))
        if strategy not in {s.key for s in catalog()}:
            return self._send(400, _page("拒绝", "<h1>未知策略</h1>"
                                         "<p><a href='/'>← 返回</a></p>"))
        if freq not in FREQS or not re.match(r"^\d{8}$", start):
            return self._send(400, _page("拒绝", "<h1>参数不合法</h1>"
                                         "<p><a href='/'>← 返回</a></p>"))
        with _lock:
            if any(j["state"] == "running" for j in _jobs.values()):
                return self._send(429, _page("忙", "<h1>已有任务在跑</h1>"
                                             "<p>请稍后再试。</p><p><a href='/'>← 返回</a></p>"))
            if time.time() - _last_run < COOLDOWN:
                return self._send(429, _page("冷却", "<h1>请求过于频繁</h1>"
                                             "<p>请稍后再试。</p><p><a href='/'>← 返回</a></p>"))
            _last_run = time.time()
            jid = uuid.uuid4().hex[:12]
            _jobs[jid] = {"state": "queued", "started": time.time(),
                          "label": f"{symbol} · {strategy} · {freq} · {start}"}
        threading.Thread(target=_run_job,
                         args=(jid, symbol, strategy, freq, start),
                         daemon=True).start()
        self._redirect(f"/job/{jid}")


def main() -> None:
    for d in (config.CACHE_DIR, config.REPORT_DIR):
        d.mkdir(parents=True, exist_ok=True)
    if not admin_token():
        print(f"[warn] 未配置 {ADMIN_ENV}：/runs/trigger 与 /scripts/harvest 将拒绝所有请求",
              flush=True)
    srv = ThreadingHTTPServer((HOST, PORT), Handler)
    print(f"alpha-radar web 已启动 http://{HOST}:{PORT}  "
          f"(allow_any={ALLOW_ANY}, cooldown={COOLDOWN}s)", flush=True)
    srv.serve_forever()


if __name__ == "__main__":
    main()
