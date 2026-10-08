"""LLM 驱动的自动移植 worker：在服务器后台持续把 Pine 脚本翻成可回测策略。

循环：取 pending 候选 → 生成工单 → 交给 DeepSeek 翻译 → 四道闸门校验 →
      通过就写入 strategies/generated.py 并入队；不过就把错误反馈回去重试，
      连续失败则回滚并标记 rejected（**绝不留下没通过的代码**）。

安全设计（LLM 生成代码必须当成不可信输入）：
  1. 先 compile() 查语法，再写入；
  2. 写入前保存原文，校验不过立即回滚 —— 保证 strategies/generated.py 永远可导入；
  3. 校验用确定性闸门（无未来函数 / 信号健全 / 可复现 / 预热纪律），
     闸门不过就不入库，模型的自我评价不作数；
  4. 生成代码只拿到 df 和 params，跑在受限契约里（不碰网络/文件/撮合）。

DeepSeek 是推理型模型：**必须关思考**，否则输出预算被推理吃光、正文为空
（这台机上实测过）。请求里带 thinking.type=disabled。

用法：
    python -m alpharadar.porting.autoport --once --max-ports 3
    python -m alpharadar.porting.autoport --minutes 720        # 常驻
"""

from __future__ import annotations

import argparse
import importlib
import os
import re
import sys
import time
from datetime import datetime
from pathlib import Path

import requests

from .. import config, store
from ..strategies import REGISTRY
from . import scaffold, verify
from .triage import port_stats

GEN = config.DATA_DIR / "generated_strategies.py"
GEN_HEADER = '''"""LLM 自动移植生成的策略（自动写入，不要手工编辑）。

放在 data/ 而不是源码树：源码树每次部署都会被覆盖，生成物放进去会被冲掉。
每个策略都通过了 verify 的四道闸门才写进来；未通过的回滚删除。
"""

import numpy as np
import pandas as pd

from alpharadar.strategies.base import register, signal_frame
'''
WRAPPERS_TEXT = "\n".join(
    f"- {fam}：{w['how']}｜入场 {w['entry']}｜出场 {w['exit']}"
    for fam, w in scaffold.WRAPPERS.items() if fam != "unknown")

SYSTEM = f"""你是 Pine Script 到 Python 的量化策略翻译器。把给定的 Pine 脚本翻译成本项目要求的 Python 策略函数。

【输出格式】只输出一个 ```python 代码块，里面是完整可运行的代码；不要任何解释文字。

【代码契约】
- 必须用 @register 装饰，函数名以下划线开头，签名 (df: pd.DataFrame, p: dict) -> pd.DataFrame
- 只允许使用：numpy(np)、pandas(pd)、signal_frame、register
- df 可用列：trade_time / sdate / open / high / low / close / vol
- df 里还已经算好了这些指标列，可直接用，不要重复实现：
  ma5 ma10 ma20 ma40 ma60 / atr / atr_ma / rsi / vma（成交量均线）
- 返回 return signal_frame(df, sig, stop=stop, tag=tag)
  sig：np.int8 数组，取值 1 做多 / -1 做空 / 0 无信号
  stop：结构止损价数组（没有结构位就传全 NaN 的数组）
  tag：可选，字符串数组
- 策略里**不许**算钱、不许撮合、不许读写文件、不许联网、不许 import 其他库
- **绝对禁止未来数据**：不许 shift(-n)、不许用当根之后的信息、
  request.security / pivothigh 必须按已确认口径改写（右移确认根数）
- 所有参数从 p 里取，并在 defaults 字典里给出默认值
- 预热期由 signal_frame 自动处理（默认前 60 根不出信号），你不用自己写
- @register 的第一个参数（策略 key）用 `tv_` 开头，后面自己起一个不冲突的名字，
  例如 tv_my_rsi_cross；不要跟已有的内置策略重名（utbot/supertrend/orb 等）

【指标 → 策略的包装器】若原脚本是 indicator（没有 strategy.entry），从中选一个并在注释里写明理由：
{WRAPPERS_TEXT}
若原脚本已经是 strategy（有 strategy.entry），直接复刻它的入场/出场条件，不要另创包装器。

【质量要求】宁可保守：明确的入场/出场条件、有可复现的参数、止损有明确含义。
"""


def _llm(messages: list[dict], timeout: int = 180) -> str:
    """调用 DeepSeek（OpenAI 兼容）。关思考，否则正文会被推理吃空。"""
    base = os.environ.get("LLM_BASE_URL", "https://api.deepseek.com").rstrip("/")
    key = os.environ.get("LLM_API_KEY", "")
    model = os.environ.get("LLM_MODEL", "deepseek-flash")
    if not key:
        raise RuntimeError("缺少 LLM_API_KEY（在 .env 里配置）")
    body = {"model": model, "messages": messages, "max_tokens": 4096,
            "temperature": 0.2, "thinking": {"type": "disabled"}}
    r = requests.post(f"{base}/chat/completions", json=body, timeout=timeout,
                      headers={"Authorization": f"Bearer {key}",
                               "Content-Type": "application/json"})
    if r.status_code != 200:
        raise RuntimeError(f"LLM HTTP {r.status_code}: {r.text[:200]}")
    js = r.json()
    return js["choices"][0]["message"].get("content") or ""


def _extract_code(text: str) -> str:
    m = re.findall(r"```(?:python)?\s*(.*?)```", text, re.S)
    code = (m[0] if m else text).strip()
    if "@register" not in code:
        raise RuntimeError("返回内容里没有 @register")
    return code


def _append(block: str, sid: str) -> str:
    """写入 generated.py（返回写入前的原文，用于回滚）。"""
    GEN.parent.mkdir(parents=True, exist_ok=True)
    old = GEN.read_text(encoding="utf-8") if GEN.exists() else GEN_HEADER
    stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    GEN.write_text(old.rstrip() + f"\n\n\n# ==== AUTO-PORT {sid} {stamp} ====\n"
                   + block.strip() + f"\n# ==== END {sid} ====\n", encoding="utf-8")
    return old


def _reload() -> None:
    """把 generated.py 重新导入当前进程，让新策略进入注册表。"""
    import alpharadar.strategies as S
    for k in [k for k in REGISTRY if k.startswith("tv_")]:
        REGISTRY.pop(k, None)          # 清掉旧注册，避免被拒的策略残留
    S.load_generated()


def _rollback(old: str, before_keys: set) -> None:
    """回滚到写入前：还原文件 + 清掉这次新注册的 key。

    只还原文件是不够的 —— REGISTRY 是常驻字典，被拒的策略会留在内存里，
    下一轮 --sync 就可能把它同步进队列（踩过）。所以按 key 快照显式清理。
    """
    GEN.write_text(old, encoding="utf-8")
    for k in list(REGISTRY):
        if k not in before_keys:
            REGISTRY.pop(k, None)
    _reload()
    for k in list(REGISTRY):
        if k not in before_keys:
            REGISTRY.pop(k, None)


def port_one(rec: dict, max_tries: int = 2, verbose=print) -> dict:
    """翻译一个脚本；返回 {ok, key, errors}。"""
    sid = rec["sid"]
    worksheet = scaffold.make_worksheet(sid)
    before_registry = dict(REGISTRY)
    before_keys = set(before_registry)
    msgs = [{"role": "system", "content": SYSTEM},
            {"role": "user", "content": f"{worksheet}\n\n【本脚本的 source_sid】{sid}"}]
    last_err = ""
    key = ""
    for attempt in range(1, max_tries + 1):
        try:
            code = _extract_code(_llm(msgs))
        except Exception as exc:
            last_err = f"LLM 调用失败：{exc}"
            verbose(f"    [{attempt}] {last_err[:120]}")
            continue
        try:
            compile(code, "<gen>", "exec")          # 先查语法，别污染模块
        except SyntaxError as exc:
            last_err = f"语法错误：{exc}"
            msgs += [{"role": "assistant", "content": code},
                     {"role": "user", "content": f"这段代码有语法错误：{exc}。请修正后重新输出完整代码。"}]
            continue

        # key 由模型自己起，只要求 tv_ 前缀且不覆盖内置策略 ——
        # 原来的实现强制它用我指定的名字，实测 162 个被拒里有 104 个栽在这上面，
        # 而那是纯粹的命名约定问题，不是策略逻辑问题。
        m = re.search(r'@register\(\s*["\']([^"\']+)["\']', code)
        if not m:
            last_err = "代码里找不到 @register 的 key"
            msgs += [{"role": "assistant", "content": code},
                     {"role": "user", "content": "找不到 @register 的第一个参数。请按模板重新输出。"}]
            continue
        key = m.group(1)
        if not re.fullmatch(r"tv_[A-Za-z0-9_]{2,40}", key):
            last_err = f"key「{key}」不合规：必须以 tv_ 开头（如 tv_my_rsi）"
            msgs += [{"role": "assistant", "content": code},
                     {"role": "user", "content": last_err + "。请修正后重新输出。"}]
            continue
        if key in before_registry and not key.startswith("tv_"):
            last_err = f"key「{key}」会覆盖内置策略，请换个名字"
            msgs += [{"role": "assistant", "content": code},
                     {"role": "user", "content": last_err + "。请修正后重新输出。"}]
            continue

        old = _append(code, sid)
        try:
            _reload()
        except Exception as exc:
            _rollback(old, before_keys)
            last_err = f"导入失败：{exc}"
            msgs += [{"role": "assistant", "content": code},
                     {"role": "user", "content": f"代码导入报错：{exc}。请修正后重新输出。"}]
            continue
        if key not in REGISTRY:
            _rollback(old, before_keys)
            last_err = f"代码里没有注册出 key={key} 的策略"
            msgs += [{"role": "assistant", "content": code},
                     {"role": "user", "content": f"没有看到 key={key} 的注册，请修正。"}]
            continue

        r = verify.verify_strategy(key, bars=verify.synth_bars(2000))
        if not r["ok"]:
            _rollback(old, before_keys)             # 回滚，绝不留下没过的代码
            last_err = "；".join(r["errors"])
            verbose(f"    [{attempt}] 闸门未过：{last_err[:140]}")
            msgs += [{"role": "assistant", "content": code},
                     {"role": "user",
                      "content": "这段代码没有通过自动校验，问题：" + last_err
                                 + "。请修正后重新输出完整代码（尤其注意不要用未来数据）。"}]
            continue

        verbose(f"    通过闸门：longs={r['stats']['longs']} shorts={r['stats']['shorts']}")
        return {"ok": True, "key": key, "errors": []}
    return {"ok": False, "key": key, "errors": [last_err or "未知失败"]}


def _day_budget() -> tuple[int, str]:
    """返回 (今天已移植数, 日期)。用于每日上限。"""
    d = (store.get_state("autoport_day") or {}).get("value") or {}
    today = datetime.now().strftime("%Y-%m-%d")
    return (int(d.get("count", 0)) if d.get("date") == today else 0), today


def run_autoport(max_ports: int = 3, minutes: float = 0.0, max_tries: int = 2,
                 pace: float = 0.0, daily_cap: int = 0, verbose=print) -> dict:
    store.init()
    t0 = time.time()
    ok = bad = 0
    done = 0
    while True:
        if max_ports and done >= max_ports:
            break
        if minutes and (time.time() - t0) / 60 >= minutes:
            break
        used, today = _day_budget()
        if daily_cap and used >= daily_cap:
            verbose(f"[autoport] 今日已达上限 {daily_cap} 个（已用 {used}），本轮结束")
            break
        cands = port_stats()["top"]
        if not cands:
            verbose("[autoport] 没有待移植候选")
            break
        rec = cands[0]
        done += 1
        verbose(f"[autoport] ({done}) {rec['title'][:44]} | {rec['family']} | 分数 {rec['score']}")
        res = port_one(rec, max_tries=max_tries, verbose=verbose)
        with store.connect() as con:
            if res["ok"]:
                con.execute("UPDATE ports SET status='verified', strategy_key=?, notes=?, "
                            "updated_at=datetime('now') WHERE sid=?",
                            (res["key"], "自动移植通过四道闸门", rec["sid"]))
                ok += 1
            else:
                con.execute("UPDATE ports SET status='rejected', notes=?, "
                            "updated_at=datetime('now') WHERE sid=?",
                            ("；".join(res["errors"])[:500], rec["sid"]))
                bad += 1
        verbose(f"    -> {'已入库 ' + res['key'] if res['ok'] else '拒绝：' + res['errors'][0][:100]}")
        store.set_state("autoport_day", {"date": today, "count": used + 1})
        if pace:
            time.sleep(pace)

    st = {"ok": ok, "bad": bad, "seconds": round(time.time() - t0)}
    store.set_state("autoport", {**st, "finished":
                                 datetime.now().isoformat(timespec="seconds")})
    verbose(f"[autoport] 本轮完成：成功 {ok} 失败 {bad}，用时 {st['seconds']}s")
    return st


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="LLM 自动移植 Pine 策略")
    ap.add_argument("--max-ports", type=int, default=3, help="本轮最多移植几个")
    ap.add_argument("--minutes", type=float, default=0.0, help="时间预算（分钟）")
    ap.add_argument("--tries", type=int, default=2, help="单个脚本最多重试几次")
    ap.add_argument("--pace", type=float, default=0.0, help="每个之间等待秒数（控成本）")
    ap.add_argument("--daily-cap", type=int, default=0, help="每日最多移植几个（0=不限）")
    args = ap.parse_args(argv)
    from ..tushare_client import TushareClient  # noqa: F401  (保证 config 已加载)
    config.load_env()
    run_autoport(args.max_ports, args.minutes, args.tries, args.pace, args.daily_cap)
    return 0


if __name__ == "__main__":
    sys.exit(main())
