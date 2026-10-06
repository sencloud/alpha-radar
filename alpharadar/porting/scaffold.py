"""生成「移植工单」：把一个 Pine 脚本打包成 agent 可以直接开工的任务包。

工单包含：元信息 + 原始 Pine 源码 + 家族判断与建议包装器 + 风险清单 +
预填好的 Python 模板 + 验收清单。agent 拿着它写策略，写完跑 verify 过关即入库。

用法：
    python -m alpharadar.porting.scaffold --list 20          # 列出候选
    python -m alpharadar.porting.scaffold --sid <sid>        # 生成工单
    python -m alpharadar.porting.scaffold --next             # 自动挑下一个最高分
"""

from __future__ import annotations

import argparse
from pathlib import Path

from .. import store
from ..config import CORPUS_DIR
from ..strategies import REGISTRY
from .triage import port_stats

# 指标族 -> 标准包装器（把「指标」变成「策略」的做法）。详见 docs/porting.md
WRAPPERS: dict[str, dict] = {
    "oscillator": {
        "how": "阈值穿越：上穿超卖线做多、下穿超买线做空；或零轴穿越",
        "entry": "sig=1 当 osc 上穿 lower；sig=-1 当 osc 下穿 upper",
        "exit": "反向穿越，或 ATR 跟踪止损",
        "params": "length, lower, upper（默认 14/30/70）",
    },
    "bands": {
        "how": "二选一：(a) 触带反转（触下轨做多）(b) 带外突破（收盘破上轨做多）",
        "entry": "sig=1 当 close 上穿 lower，或 close > upper（按所选口径）",
        "exit": "回到中轨 / 对侧带 / ATR 跟踪",
        "params": "length, mult（默认 20/2.0）",
    },
    "trend": {
        "how": "方向翻转：状态由空转多时做多，反之做空",
        "entry": "sig = trend - trend.shift(1)（只在 ±1 时）",
        "exit": "再次翻转 / ATR 跟踪止损",
        "params": "length, mult（按原脚本默认）",
    },
    "level": {
        "how": "穿越水平位：收盘上穿关键位做多",
        "entry": "sig=1 当 close 上穿 level；sig=-1 当 close 下穿 level",
        "exit": "回到该位下方 / ATR 跟踪",
        "params": "pivot 周期、位选择",
    },
    "volatility": {
        "how": "作为过滤器叠加：波动扩张（ATR 高于均值 / 带宽放大）才允许交易；"
               "或挤压后突破",
        "entry": "被过滤的主信号（通常配 trend/bands 家族）",
        "exit": "同被包装的主策略",
        "params": "atr_n, atr_ratio_min（参考 use_atr_filter）",
    },
    "volume": {
        "how": "作为确认过滤器：放量 / 增仓才入场",
        "entry": "主信号 AND (vol > k*均量) AND/OR (oi 变化 > 0)",
        "exit": "同主策略",
        "params": "vol_n, vol_mult",
    },
    "pattern": {
        "how": "形态触发：形态在当根确认，当根收盘（或下一根开盘）入场",
        "entry": "按形态定义给出布尔序列，转成 sig",
        "exit": "形态失效点（如吞没的低点）/ ATR",
        "params": "pattern 相关参数",
    },
    "unknown": {
        "how": "家族未识别：先读源码，判断它到底在算什么，再决定包装方式",
        "entry": "—", "exit": "—", "params": "—",
    },
}

TEMPLATE = '''
@register("{key}", "{name}", source="{source}", license="{license}",
          source_sid="{sid}", freqs=("1min", "5min", "15min", "30min", "60min"),
          defaults={{ }},
          notes="{notes}")
def _{key}(df: pd.DataFrame, p: dict) -> pd.DataFrame:
    # 移植自 {title}（{author}）
    # 包装器：{wrapper}
    # 改写说明：逐条写清对原逻辑做了什么改动，尤其是重绘/未来函数部分
    h = df["high"].to_numpy(float)
    l = df["low"].to_numpy(float)
    c = df["close"].to_numpy(float)
    v = df["vol"].to_numpy(float)
    # TODO 1) 复刻原指标（只用 <= t 的数据）
    # TODO 2) 把指标转成 sig（见上面的包装器）
    # TODO 3) 给出结构止损 stop；没有就留 NaN 并改用 stop_mode="atr"
    sig = np.zeros(len(df), dtype=np.int8)
    stop = np.full(len(df), np.nan)
    return signal_frame(df, sig, stop=stop)
'''


def list_candidates(limit: int = 20) -> list[dict]:
    st = port_stats()
    done = {s.source_sid for s in REGISTRY.values() if s.source_sid}
    return [r for r in st["top"] if r["sid"] not in done][:limit]


def make_worksheet(sid: str) -> str:
    rec = store.get_script(sid)
    if not rec:
        raise SystemExit(f"语料库没有这个脚本：{sid}")
    src = (CORPUS_DIR / "sources" / (rec["file"] or "")).read_text(
        encoding="utf-8", errors="replace")
    with store.connect() as con:
        p = con.execute("SELECT * FROM ports WHERE sid=? OR sid LIKE ?",
                        (sid, f"%;{sid}")).fetchone()
    fam = (p["family"] if p else "unknown") or "unknown"
    risk = (p["risk"] if p else "") or ""
    w = WRAPPERS.get(fam, WRAPPERS["unknown"])
    key = f"tv_{sid.split(';')[-1][:10]}"
    body = [
        f"# 移植工单：{rec['title']}",
        "",
        f"- 脚本 ID：`{sid}`",
        f"- 作者：{rec['author']}　点赞：{rec['agree']:,}　类型：{rec['kind']}　"
        f"行数：{rec['lines']}",
        f"- 原页：https://www.tradingview.com/script/{rec['slug']}/",
        f"- 本地源码：`corpus/sources/{rec['file']}`",
        f"- 判定家族：**{fam}**",
        f"- 重绘/未来函数风险：{risk or '未检出'}",
        "",
        "## 建议包装器（指标 -> 策略）",
        f"- 做法：{w['how']}",
        f"- 入场：{w['entry']}",
        f"- 出场：{w['exit']}",
        f"- 参数：{w['params']}",
        "",
        "## 硬要求",
        "1. 只能使用 <= t 的数据；security / pivothigh 必须按**已确认口径**改写",
        "2. 只输出信号列（sig / stop_px / sig_tag），不要在策略里算钱或撮合",
        "3. defaults 里必须列全该策略读取的所有参数",
        "4. 文件头保留原作者与许可（写进 source= / license=）",
        "5. 写完必须跑：python -m alpharadar.porting.verify --strategy <key> "
        "--real P.DCE:5min",
        "",
        "## Python 模板（可直接开工）",
        "```python",
        TEMPLATE.format(key=key, name=str(rec["title"]).replace('"', ""),
                        source=f"TradingView @{rec['author']}",
                        license="<按原脚本填写>", sid=sid,
                        notes=f"移植自 {rec['title']}；包装器：{fam}",
                        title=rec["title"], author=rec["author"],
                        wrapper=f"{fam} - {w['how']}").strip(),
        "```",
        "",
        "## 原始 Pine 源码",
        "```pine",
        src.rstrip(),
        "```",
        "",
    ]
    return "\n".join(body)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="生成 Pine 移植工单")
    ap.add_argument("--sid", help="脚本 id（可只给哈希部分）")
    ap.add_argument("--next", action="store_true", help="自动挑下一个最高分候选")
    ap.add_argument("--list", type=int, metavar="N", help="列出 N 个候选")
    ap.add_argument("--out", default="work", help="工单输出目录（默认 work/）")
    args = ap.parse_args(argv)

    if args.list:
        for i, r in enumerate(list_candidates(args.list), 1):
            flag = "!" if r["risk"] else " "
            print(f"{i:>3}. [{r['score']:>5}] {flag} {r['kind']:<9} {r['family']:<11} "
                  f"{(r['title'] or '')[:42]:<44} @{r['author']}")
        return 0

    sid = args.sid
    if args.next or not sid:
        cands = list_candidates(1)
        if not cands:
            print("没有待移植候选（先跑 python -m alpharadar.porting.triage）")
            return 1
        sid = cands[0]["sid"]
    text = make_worksheet(sid)
    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"port-{sid.split(';')[-1][:10]}.md"
    path.write_text(text, encoding="utf-8")
    with store.connect() as con:
        con.execute("UPDATE ports SET status='ported', updated_at=datetime('now') "
                    "WHERE sid=? OR sid LIKE ?", (sid, f"%;{sid}"))
    print(f"工单已生成：{path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
