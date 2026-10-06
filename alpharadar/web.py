"""alpha-radar 轻量 Web 控制台。

只用标准库（http.server），不引入 Flask/FastAPI —— 服务器上少一个依赖就少一类故障。

路由：
    GET  /              落地页（项目介绍 + 封面 + 策略表 + 回测表单 + 最近报告）
    POST /run           提交回测任务（后台线程执行），跳转到任务页
    GET  /job/<id>      任务进度页（运行中自动刷新，完成后跳报告）
    GET  /report/<名称> 查看已生成的 HTML 报告
    GET  /api/health    健康检查（供 Caddy / 监控用）
    GET  /api/strategies 策略清单 JSON
    GET  /cover.png     推广封面

安全与配额（公开站点必须考虑）：
    - 品种默认只允许 universe.PRESETS 里的白名单；设 ALPHARADAR_ALLOW_ANY=1 才放开
    - 策略只允许注册表里的 key
    - 全局同一时刻只跑一个任务，且两次任务之间有冷却
    - 报告文件名做白名单校验，禁止路径穿越
"""

from __future__ import annotations

import html
import json
import os
import re
import threading
import time
import traceback
import uuid
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
"""


def _page(title: str, body: str, refresh: str = "") -> bytes:
    meta = f'<meta http-equiv="refresh" content="{refresh}">' if refresh else ""
    nav = ('<div class="nav"><a href="/">首页</a>'
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
&nbsp;·&nbsp; <a href="/api/strategies">/api/strategies</a></p>
"""
    return _page("alpha-radar · 策略雷达", body)


def _job_page(job: dict) -> bytes:
    """任务进度页。"""
    if job["state"] == "done":
        return _page("完成", f"""<h1>回测完成</h1>
<p><a href="/report/{html.escape(job['report'])}">查看报告 →</a></p>
<p class="note">{html.escape(job.get('summary', ''))}</p>
<p><a href="/">← 返回首页</a></p>""")
    if job["state"] == "error":
        return _page("失败", f"""<h1>回测失败</h1>
<pre class="note">{html.escape(job.get('error', ''))}</pre>
<p><a href="/">← 返回首页</a></p>""")
    return _page("运行中", f"""<h1>回测运行中…</h1>
<p class="note">{html.escape(job['label'])}</p>
<p class="note">首次拉取数据较慢（期货 1 分钟线可能要数分钟），请稍候。</p>
<p><a href="/">← 返回首页</a></p>""", refresh="4")


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
    ss = store.script_stats()
    rows = store.latest_results(symbol=symbol, strategy=strategy, freq=freq,
                                market=market)
    syms = store.symbols_in_results()
    runs = store.recent_runs(20)

    def card(k, v):
        return f"<div class='card'><div class='k'>{k}</div><div class='v'>{v}</div></div>"

    cards = "".join([
        card("语料库", f"{ss.get('total') or 0}"),
        card("其中开源", f"{ss.get('open') or 0}"),
        card("strategy 类型", f"{ss.get('strat') or 0}"),
        card("上次采集新增", f"{hv.get('new', '—')}"),
        card("上次扫描成功", f"{cy.get('ok', '—')}"),
        card("上次扫描失败", f"{cy.get('err', '—')}"),
        card("组合总数", f"{cy.get('cells', '—')}"),
    ])

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
<p class="note">上次采集：{html.escape(str((store.get_state('harvest') or {}).get('ts') or '—'))}
&nbsp;·&nbsp; 上次扫描：{html.escape(str((store.get_state('last_cycle') or {}).get('ts') or '—'))}
&nbsp;·&nbsp; 扫描由 systemd timer 驱动，失败会自动跳过并在运行记录里留痕</p>
<form method="post" action="/runs/trigger">
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

    def _send(self, code: int, body: bytes, ctype="text/html; charset=utf-8") -> None:
        self.send_response(code)
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
        if p == "/runs":
            try:
                return self._send(200, _runs_page(parse_qs(u.query)))
            except Exception as exc:
                return self._send(500, _page("错误", f"<h1>读取结果库失败</h1>"
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
        if p == "/runs/trigger":
            return self._trigger_cycle()
        if p != "/run":
            return self._send(404, b"not found")
        n = int(self.headers.get("Content-Length", 0))
        form = parse_qs(self.rfile.read(n).decode("utf-8", "replace"))
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
    srv = ThreadingHTTPServer((HOST, PORT), Handler)
    print(f"alpha-radar web 已启动 http://{HOST}:{PORT}  "
          f"(allow_any={ALLOW_ANY}, cooldown={COOLDOWN}s)", flush=True)
    srv.serve_forever()


if __name__ == "__main__":
    main()
