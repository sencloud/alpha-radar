"""命令行入口：alpharadar <子命令>。

  harvest   采集/更新 TradingView 开源策略语料库
  corpus    列出语料库里点赞最高的开源脚本
  list      列出内置策略
  run       单品种 × 单策略回测（并输出 HTML 报告）
  matrix    多品种 × 多策略 × 多周期排行榜
  falsify-export  按五道闸门判定结果库 + 精选档案，导出对外证伪档案 JSON
"""

from __future__ import annotations

import argparse
import sys

import pandas as pd

from . import config
from .harvest import DEFAULT_TERMS, harvest, top_open
from .metrics import render_text
from .pipeline import catalog, run_matrix, run_one, save_result


def _kv(pairs: list[str]) -> dict:
    out = {}
    for kv in pairs or []:
        if "=" not in kv:
            raise SystemExit(f"参数要写成 key=value：{kv}")
        k, v = kv.split("=", 1)
        out[k.strip()] = _coerce(v.strip())
    return out


def _coerce(v: str):
    if v.lower() in ("true", "false"):
        return v.lower() == "true"
    if "," in v:
        return tuple(float(x) for x in v.split(",") if x.strip())
    try:
        f = float(v)
        return int(f) if f.is_integer() and "." not in v else f
    except ValueError:
        return v


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="alpharadar",
                                 description="持续采集 TradingView 策略并在 A股/期货上自动回测")
    sub = ap.add_subparsers(dest="cmd", required=True)

    h = sub.add_parser("harvest", help="采集 TradingView 开源策略")
    h.add_argument("--terms", default="", help="逗号分隔的搜索词（默认内置 40 个）")
    h.add_argument("--per-term", type=int, default=200)
    h.add_argument("--max-fetch", type=int, default=300)

    c = sub.add_parser("corpus", help="列出语料库")
    c.add_argument("-n", type=int, default=30)

    sub.add_parser("list", help="列出内置策略")

    r = sub.add_parser("run", help="单品种单策略回测")
    r.add_argument("--symbol", required=True, help="如 P.DCE / 600519.SH")
    r.add_argument("--strategy", required=True, help="策略 key，见 alpharadar list")
    r.add_argument("--freq", default="5min", help="1min/5min/15min/30min/60min/1d")
    r.add_argument("--start", default="20220101")
    r.add_argument("--end", default=None)
    r.add_argument("--capital", type=float, default=100_000.0)
    r.add_argument("--set", action="append", default=[], metavar="KEY=VALUE")
    r.add_argument("--no-report", action="store_true", help="不写 HTML 报告")

    m = sub.add_parser("matrix", help="多品种多策略排行榜")
    m.add_argument("--symbols", required=True, help="逗号分隔")
    m.add_argument("--strategies", default="supertrend,utbot,chandelier,orb,vreversal")
    m.add_argument("--freqs", default="5min")
    m.add_argument("--start", default="20220101")
    m.add_argument("--end", default=None)
    m.add_argument("--set", action="append", default=[], metavar="KEY=VALUE")
    m.add_argument("--out", default=None, help="结果 CSV 路径")

    fx = sub.add_parser("falsify-export",
                        help="导出证伪档案 JSON（契约见 docs/falsification-export.md）")
    fx.add_argument("--out", required=True, help="输出文件路径；写 - 输出到 stdout")
    fx.add_argument("--include-insufficient", action="store_true",
                    help="同时导出样本不足（insufficient）的条目")
    fx.add_argument("--limit", type=int, default=None,
                    help="自动判定条目的上限（精选档案不受限）")
    fx.add_argument("--db", default=None, help="结果库路径（默认 data/alpharadar.db）")

    args = ap.parse_args(argv)

    if args.cmd == "falsify-export":
        import json
        from pathlib import Path
        from .falsify import build_export, write_export
        kw = {"include_insufficient": args.include_insufficient, "limit": args.limit,
              "db_path": Path(args.db) if args.db else None}
        if args.out == "-":
            print(json.dumps(build_export(**kw), ensure_ascii=False, indent=2,
                             allow_nan=False))
            return 0
        payload = write_export(Path(args.out), **kw)
        s = payload["summary"]
        print(f"[ok] {args.out}  阈值 {payload['threshold_version']}  "
              f"共 {s['archive_total']} 条（精选 {s['curated']} / 自动 {s['auto']}）"
              f"  结论分布 {s['by_verdict']}  许可排除 {s['excluded_license']}"
              f"  隐藏样本不足 {s['insufficient_hidden']}", file=sys.stderr)
        return 0

    if args.cmd == "harvest":
        terms = tuple(t.strip() for t in args.terms.split(",") if t.strip()) or DEFAULT_TERMS
        harvest(terms=terms, per_term=args.per_term, max_fetch=args.max_fetch)
        return 0

    if args.cmd == "corpus":
        rows = [{"点赞": s.agree, "标题": s.title, "作者": s.author,
                 "类型": s.kind, "行数": s.lines, "文件": s.file}
                for s in top_open(args.n)]
        print(pd.DataFrame(rows).to_string(index=False) if rows
              else "语料库为空，先运行：alpharadar harvest")
        return 0

    if args.cmd == "list":
        rows = [{"key": s.key, "名称": s.name, "出处": s.source, "许可": s.license,
                 "说明": s.notes} for s in catalog()]
        print(pd.DataFrame(rows).to_string(index=False))
        return 0

    if args.cmd == "run":
        res = run_one(args.symbol, args.strategy, args.freq, args.start, args.end,
                      _kv(args.set), args.capital)
        print(render_text(res))
        if not args.no_report:
            csv, html = save_result(res)
            print(f"\n逐笔明细：{csv}\nHTML 报告：{html}")
        return 0

    if args.cmd == "matrix":
        df = run_matrix([s.strip() for s in args.symbols.split(",") if s.strip()],
                        [s.strip() for s in args.strategies.split(",") if s.strip()],
                        tuple(f.strip() for f in args.freqs.split(",") if f.strip()),
                        args.start, args.end, _kv(args.set))
        pd.set_option("display.width", 200)
        print(df.to_string(index=False))
        out = args.out or (config.REPORT_DIR / "matrix.csv")
        df.to_csv(out, index=False, encoding="utf-8-sig")
        print(f"\n已写入 {out}")
        return 0

    return 1


if __name__ == "__main__":
    sys.exit(main())
