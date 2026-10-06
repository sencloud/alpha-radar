"""全局配置：路径、环境变量、默认回测参数。"""

from __future__ import annotations

import os
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _p(env: str, default: str) -> Path:
    return Path(os.environ.get(env, ROOT / default)).expanduser().resolve()


CACHE_DIR = _p("ALPHARADAR_CACHE", "data_cache")      # 行情缓存
DATA_DIR = _p("ALPHARADAR_DATA", "data")              # 结果数据库
CORPUS_DIR = _p("ALPHARADAR_CORPUS", "corpus")        # TradingView 语料库
REPORT_DIR = _p("ALPHARADAR_REPORTS", "reports")      # 回测报告

for _d in (CACHE_DIR, DATA_DIR, CORPUS_DIR, REPORT_DIR):
    _d.mkdir(parents=True, exist_ok=True)

DB_PATH = DATA_DIR / "alpharadar.db"


def load_env(path: Path | None = None) -> None:
    """把 .env 读入 os.environ（已存在的变量不覆盖）。"""
    env_path = path or (ROOT / ".env")
    if not env_path.is_file():
        return
    for line in env_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        v = v.strip()
        if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'":
            v = v[1:-1]
        os.environ.setdefault(k.strip(), v)


# ==================== 回测默认参数 ====================
# 这些是所有策略共用的「引擎层」参数；策略自身的参数放在 strategies/library.py。
DEFAULTS: dict = {
    # 指标
    "atr_n": 14,
    "atr_period": 10,         # 趋势族 ATR 周期
    "atr_key": 3.0,           # 趋势族 ATR 倍数（收紧会显著变差，见 docs/pitfalls.md）
    "rsi_n": 14,
    "vol_n": 20,
    "atr_ma_n": 50,
    # 体制闸门
    "use_regime": 0,
    "er_n": 48,
    "er_min": 0.0,
    "atr_ratio_n": 480,
    "atr_ratio_min": 0.0,
    "adx_min": 0.0,
    "regime_confirm": 1,
    # 成本（由 Instrument 覆盖）
    "slippage_ticks": 1,      # 每边滑点跳数
    # 出场
    "stop_mode": "struct",    # struct=信号给出的结构止损；atr=入场价 ± stop_atr×ATR
    "stop_atr": 2.0,
    "stop_buf_atr": 0.5,
    "stop_min_points": 0.0,
    "stop_max_atr": 6.0,
    "partial_tps": (),        # 分批止盈（×ATR），如 (1.0, 2.0, 3.0)
    "tgt_atr": 2.5,           # 单目标止盈（×ATR）
    "use_target": 1,          # 0=不设固定止盈（跟踪止损类策略）
    "trail_stop": 0,          # 1=用信号给出的逐根跟踪止损
    "breakeven_points": 0.0,  # 浮盈达此点数后止损推到成本（0=关闭）
    "max_hold_bars": 60,
    # 时段与风控
    "no_entry_before": "0910",   # 开盘消化期不开仓（"0910" 形式，""=不限制）
    "no_entry_after": "1430",    # 午后衰竭期不开仓
    "allow_night": 0,            # 0=只做日盘（夜盘流动性/跳空另论）
    "cooldown_bars": 0,
    "max_entries_per_day": 1,
    "allow_overnight": 0,     # 0=收盘强平（日内）
    # 资金
    "lots": 1,                # 期货：手数；股票：手数（1 手 = 100 股）
}
