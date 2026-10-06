"""报告输出：单文件 HTML（无外部依赖，可直接发到 GitHub Pages / 微信）。"""

from __future__ import annotations

import html
from pathlib import Path

import numpy as np
import pandas as pd

from .engine import BacktestResult
from .metrics import group_table, summarize, yearly_table

CSS = """
body{font-family:-apple-system,'Segoe UI','Microsoft YaHei',sans-serif;margin:0;
background:#0f1115;color:#e6e6e6}
.wrap{max-width:960px;margin:0 auto;padding:28px 20px 60px}
h1{font-size:22px;margin:0 0 4px}
.sub{color:#8b93a1;font-size:13px;margin-bottom:22px}
.cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:10px;
margin-bottom:22px}
.card{background:#171a21;border:1px solid #232833;border-radius:10px;padding:12px 14px}
.card .k{color:#8b93a1;font-size:12px}
.card .v{font-size:20px;font-weight:600;margin-top:4px}
.pos{color:#31c48d}.neg{color:#f05252}
table{border-collapse:collapse;width:100%;font-size:13px;margin:8px 0 22px}
th,td{padding:7px 10px;border-bottom:1px solid #232833;text-align:right}
th:first-child,td:first-child{text-align:left}
th{color:#8b93a1;font-weight:500;background:#14171d}
h2{font-size:15px;margin:24px 0 6px;color:#c9d1d9}
.note{color:#8b93a1;font-size:12px;line-height:1.7}
"""


def _sparkline(eq: pd.DataFrame, w: int = 900, h: int = 160) -> str:
    if eq.empty or len(eq) < 2:
        return ""
    y = eq["equity"].astype(float).to_numpy()
    x = np.linspace(0, w, len(y))
    lo, hi = float(np.min(y)), float(np.max(y))
    rng = (hi - lo) or 1.0
    py = h - (y - lo) / rng * (h - 20) - 10
    base = h - (eq["equity"].iloc[0] - lo) / rng * (h - 20) - 10
    pts = " ".join(f"{a:.1f},{b:.1f}" for a, b in zip(x, py))
    color = "#31c48d" if y[-1] >= y[0] else "#f05252"
    return (f'<svg viewBox="0 0 {w} {h}" width="100%" height="{h}">'
            f'<line x1="0" y1="{base:.1f}" x2="{w}" y2="{base:.1f}" '
            f'stroke="#3a3f4b" stroke-dasharray="4 4"/>'
            f'<polyline points="{pts}" fill="none" stroke="{color}" stroke-width="1.6"/></svg>')


def _table(df: pd.DataFrame, cls_col: str | None = None) -> str:
    if df.empty:
        return "<p class='note'>无数据</p>"
    head = "".join(f"<th>{html.escape(str(c))}</th>" for c in df.columns)
    body = []
    for _, r in df.iterrows():
        tds = []
        for c in df.columns:
            v = r[c]
            txt = f"{v:,.2f}" if isinstance(v, (int, float, np.floating)) else html.escape(str(v))
            cls = ""
            if cls_col and c == cls_col and isinstance(v, (int, float, np.floating)) and v != 0:
                cls = " class='pos'" if v > 0 else " class='neg'"
            tds.append(f"<td{cls}>{txt}</td>")
        body.append("<tr>" + "".join(tds) + "</tr>")
    return f"<table><thead><tr>{head}</tr></thead><tbody>{''.join(body)}</tbody></table>"


def render_html(res: BacktestResult, title: str | None = None,
                extra_note: str = "") -> str:
    s = summarize(res)
    name = title or f"{s['name']} · {res.params.get('strategy', '')}"
    cards = [
        ("笔数", f"{s['笔数']}"), ("胜率", f"{s['胜率']:.1%}" if s["笔数"] else "-"),
        ("PF", f"{s['PF']:.2f}" if s["笔数"] else "-"),
        ("每手均点", f"{s['均点']:+.3f}" if s["笔数"] else "-"),
        ("合计盈亏", f"{s['合计元']:+,.0f}"),
        ("最大回撤", f"{s['最大回撤']:,.0f}"),
        ("收益回撤比", f"{s['收益回撤比']:.2f}" if s["笔数"] else "-"),
        ("正年数", f"{s['正年数']}/{s['年数']}" if s["笔数"] else "-"),
    ]
    card_html = "".join(
        f"<div class='card'><div class='k'>{k}</div>"
        f"<div class='v'>{html.escape(v)}</div></div>" for k, v in cards)
    note = ("本报告由 alpha-radar 自动生成。回测含双边成本与滑点，"
            "但不含冲击成本、涨跌停无法成交、盘中流动性枯竭等实盘约束。"
            "历史表现不代表未来收益。")
    return f"""<!doctype html><html lang="zh"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{html.escape(name)}</title><style>{CSS}</style></head><body><div class="wrap">
<h1>{html.escape(name)}</h1>
<div class="sub">{html.escape(s['symbol'])} · {s['market']} ·
{res.params.get('freq','')} · {html.escape(str(res.params.get('strategy_display','')))}</div>
<div class="cards">{card_html}</div>
<h2>资金曲线（每交易日结算）</h2>{_sparkline(res.equity)}
<h2>分年</h2>{_table(yearly_table(res), cls_col='盈亏')}
<h2>离场原因</h2>{_table(group_table(res, '原因'), cls_col='盈亏')}
<h2>方向</h2>{_table(group_table(res, '方向'), cls_col='盈亏')}
<h2>说明</h2><p class="note">{html.escape(note)}</p>
{extra_note}
</div></body></html>"""


def write_html(res: BacktestResult, path: Path, **kw) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(render_html(res, **kw), encoding="utf-8")
    return path
