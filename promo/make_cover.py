"""生成知乎封面（1200×675，含 2x 版本）。

设计原则：知乎封面在信息流里显示得很小，只保留三层信息 ——
  1) 一句钩子：不找圣杯，只做证伪
  2) 一张真实资金曲线：冲高之后连亏（图本身就是论点）
  3) 一行出处：策略来源 / 数据源 / agent 底座 / 仓库地址

**封面上的每个数字都是从回测结果里算出来的**，不允许手写，
避免素材与代码结论不一致（这是本项目最容易翻车的地方）。

数据来源优先级：
  promo/equity.csv + promo/cover_meta.json（存在则离线重制）
  → 现跑一次真实回测并落盘
  → 兜底示意曲线（终端会明确提示）

用法：
    python promo/make_cover.py
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt                       # noqa: E402
import numpy as np                                    # noqa: E402
import pandas as pd                                   # noqa: E402
from matplotlib.patches import FancyBboxPatch, Rectangle   # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

HERE = Path(__file__).resolve().parent
EQUITY_CSV = HERE / "equity.csv"
META_JSON = HERE / "cover_meta.json"

plt.rcParams["font.sans-serif"] = ["Microsoft YaHei", "SimHei", "DejaVu Sans"]
plt.rcParams["axes.unicode_minus"] = False

# 与 HTML 报告一致的配色
BG = "#0f1115"
PANEL = "#171a21"
TEXT = "#e6e6e6"
MUTED = "#8b93a1"
GREEN = "#31c48d"
RED = "#f05252"
ACCENT = "#f0b429"
GRID = "#232833"

W, H = 1200, 675
SCALE = 2


def _meta_from_result(res) -> dict:
    tr = res.trades
    win = tr[tr["净利"] > 0]
    loss = tr[tr["净利"] <= 0]
    gl = -loss["净利"].sum()
    yearly = (tr.assign(年=tr["日期"].str[:4]).groupby("年")["净利"].sum()
              .round(0).astype(int).to_dict())
    return {"笔数": int(len(tr)),
            "PF": round(float(win["净利"].sum() / gl), 2) if gl > 0 else None,
            "分年": {str(k): int(v) for k, v in yearly.items()}}


def load_data() -> tuple[pd.DataFrame, dict, str]:
    if EQUITY_CSV.exists() and META_JSON.exists():
        eq = pd.read_csv(EQUITY_CSV)
        return eq, json.loads(META_JSON.read_text(encoding="utf-8")), "本地缓存"
    try:
        from alpharadar.pipeline import run_one
        res = run_one("P.DCE", "utbot", "5min", "20220101", verbose=False)
        eq = res.equity.rename(columns={"equity": "净值"}).copy()
        eq.to_csv(EQUITY_CSV, index=False, encoding="utf-8-sig")
        meta = _meta_from_result(res)
        META_JSON.write_text(json.dumps(meta, ensure_ascii=False, indent=1),
                             encoding="utf-8")
        return eq, meta, "真实回测（现取）"
    except Exception as exc:
        print(f"  回测不可用（{type(exc).__name__}: {exc}），改用示意曲线")
        x = np.arange(1150)
        y = (100000 + 45000 * np.exp(-((x - 300) ** 2) / 40000)
             - 60 * np.maximum(0, x - 300)
             + np.random.default_rng(3).normal(0, 900, len(x)))
        return (pd.DataFrame({"date": x, "净值": y}),
                {"笔数": None, "PF": None, "分年": {}}, "示意曲线（未取到数据）")


def _yearly_line(meta: dict, max_years: int = 5) -> str:
    items = list(meta.get("分年", {}).items())[-max_years:]
    if not items:
        return "分年数据不可用（示意曲线）"
    span = f"{items[0][0]}→{items[-1][0]}"
    vals = "  ".join(f"{v / 1000:+.1f}k" for _, v in items)
    return f"分年 {span}    {vals}"


def draw(scale: int, out: Path, eq: pd.DataFrame, meta: dict, src: str) -> None:
    fig = plt.figure(figsize=(W / 100, H / 100), dpi=100 * scale)
    ax = fig.add_axes([0, 0, 1, 1])
    ax.set_xlim(0, W)
    ax.set_ylim(0, H)
    ax.axis("off")
    ax.add_patch(Rectangle((0, 0), W, H, color=BG, zorder=0))
    for gx in np.arange(0, W, 60):
        ax.plot([gx, gx], [0, H], color=GRID, lw=0.5, alpha=0.5, zorder=1)
    for gy in np.arange(0, H, 60):
        ax.plot([0, W], [gy, gy], color=GRID, lw=0.5, alpha=0.5, zorder=1)

    # ---------- 右上：真实资金曲线 ----------
    y = eq["净值"].to_numpy(float)
    dates = eq["date"].astype(str).to_numpy() if "date" in eq.columns else None
    left, right, base_y, top_y = 600, 1180, 250, 560
    x = np.linspace(left + 16, right - 16, len(y))
    lo, hi = float(np.min(y)), float(np.max(y))
    rng = (hi - lo) or 1.0
    py = base_y + (y - lo) / rng * (top_y - base_y)

    ax.add_patch(FancyBboxPatch((left, 235), right - left, 350,
                                boxstyle="round,pad=6,rounding_size=14",
                                fc=PANEL, ec=GRID, lw=1.2, zorder=2))
    by = base_y + (y[0] - lo) / rng * (top_y - base_y)
    ax.plot([left + 16, right - 16], [by, by], color=MUTED, lw=1.0,
            ls=(0, (5, 5)), alpha=0.8, zorder=3)

    peak = int(np.argmax(y))
    ax.plot(x[:peak + 1], py[:peak + 1], color=GREEN, lw=3.2, zorder=4)
    ax.plot(x[peak:], py[peak:], color=RED, lw=3.2, zorder=5)
    ax.scatter([x[peak]], [py[peak]], s=90, color=ACCENT, zorder=6,
               edgecolor=BG, linewidth=1.5)
    label = "历史最高"
    if dates is not None and peak < len(dates):
        d = str(dates[peak]).replace("-", "")[:6]
        if len(d) == 6 and d.isdigit():
            label = f"历史最高 {d[:4]}-{d[4:6]}"
    ax.text(x[peak], py[peak] + 34, label, color=ACCENT, fontsize=17,
            ha="center", va="bottom", zorder=6)

    n, pf = meta.get("笔数") or len(y), meta.get("PF")
    head = "UT Bot @ 棕榈油 5min"
    if n:
        head += f"    {n} 笔"
    if pf:
        head += f"    PF {pf}"
    ax.text(left + 16, 208, head, color=TEXT, fontsize=18, va="bottom", zorder=6)
    ax.text(left + 16, 172, _yearly_line(meta), color=RED, fontsize=14,
            va="bottom", zorder=6)

    # ---------- 左侧：标题 ----------
    ax.text(64, 596, "alpha-radar · 策略雷达", color=ACCENT, fontsize=20,
            va="center")
    ax.plot([64, 176], [566, 566], color=ACCENT, lw=3)
    ax.text(60, 470, "不找圣杯", color=TEXT, fontsize=72, va="center",
            weight="bold")
    ax.text(60, 352, "只做证伪", color=TEXT, fontsize=72, va="center",
            weight="bold")
    ax.text(64, 252, "从 TradingView 批量采集开源策略", color=MUTED,
            fontsize=23, va="center")
    ax.text(64, 210, "在 A 股 / 期货上做含成本回测", color=MUTED,
            fontsize=23, va="center")

    # ---------- 底部信息条 ----------
    ax.add_patch(Rectangle((0, 0), W, 118, color="#0b0d11", zorder=3))
    ax.plot([0, W], [118, 118], color=GRID, lw=1.2, zorder=4)
    ax.text(64, 78, "Tushare 数据源     Python 内核     Codex harness + deepseek-flash",
            color=MUTED, fontsize=19, va="center", zorder=5)
    ax.text(64, 38, "github.com/sencloud/alpha-radar", color=ACCENT,
            fontsize=21, va="center", zorder=5, weight="bold")
    ax.text(W - 60, 58, "90% 的量化工作\n是把策略证伪", color=MUTED,
            fontsize=15, va="center", ha="right", zorder=5, alpha=0.75)

    fig.savefig(out, facecolor=BG)
    plt.close(fig)
    print(f"  已输出 {out.name}  ({src})")


def main() -> None:
    eq, meta, src = load_data()
    draw(1, HERE / "zhihu-cover.png", eq, meta, src)
    draw(SCALE, HERE / "zhihu-cover@2x.png", eq, meta, src)


if __name__ == "__main__":
    main()
