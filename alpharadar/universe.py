"""品种表：A 股与期货的合约规则、成本模型、交易时段。

新增品种只要在 PRESETS 里加一条，或写自己的 Instrument。
成本口径偏保守（宁可高估）：
  期货 —— 按手手续费 + 每边 1 跳滑点；
  A 股 —— 佣金按成交额（双边、设最低 5 元）+ 印花税（卖出单边 0.05%）。
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Instrument:
    ts_code: str                     # Tushare 代码：RB.SHF / 600519.SH
    name: str
    market: str                      # "futures" | "stock"
    mult: float                      # 期货：每手乘数；股票：1（按股计价）
    tick: float                      # 最小变动价位
    lot: int = 1                     # 最小交易单位：期货 1 手，股票 100 股
    fee_per_lot: float = 0.0         # 期货：单边手续费（元/手）
    fee_rate: float = 0.0            # 按成交额：买方佣金率（股票）
    fee_rate_sell: float = 0.0       # 按成交额：卖方额外（股票印花税）
    min_fee: float = 0.0             # 单笔最低手续费（股票 5 元）
    margin: float = 0.0              # 保证金比例（仅备查）
    t_plus_1: bool = False           # A 股当日买入不可卖出
    sessions: tuple = (("0900", "1500"),)   # 可交易时段（HHMM）

    @property
    def day_sessions(self) -> tuple:
        return self.sessions


# 期货：日内为主，含夜盘的品种把夜盘时段也列进去
PRESETS: dict[str, Instrument] = {
    # ---------- 期货 ----------
    "P.DCE":  Instrument("P.DCE", "棕榈油", "futures", 10, 2.0, 1, fee_per_lot=2.5,
                         margin=0.10, sessions=(("0900", "1015"), ("1030", "1130"),
                                                ("1330", "1500"), ("2100", "2300"))),
    "Y.DCE":  Instrument("Y.DCE", "豆油", "futures", 10, 2.0, 1, fee_per_lot=2.5,
                         margin=0.10, sessions=(("0900", "1015"), ("1030", "1130"),
                                                ("1330", "1500"), ("2100", "2300"))),
    "OI.ZCE": Instrument("OI.ZCE", "菜油", "futures", 10, 1.0, 1, fee_per_lot=2.0,
                         margin=0.10, sessions=(("0900", "1015"), ("1030", "1130"),
                                                ("1330", "1500"), ("2100", "2300"))),
    "M.DCE":  Instrument("M.DCE", "豆粕", "futures", 10, 1.0, 1, fee_per_lot=1.5,
                         margin=0.08, sessions=(("0900", "1015"), ("1030", "1130"),
                                                ("1330", "1500"), ("2100", "2300"))),
    "RB.SHF": Instrument("RB.SHF", "螺纹钢", "futures", 10, 1.0, 1, fee_per_lot=3.5,
                         margin=0.10, sessions=(("0900", "1015"), ("1030", "1130"),
                                                ("1330", "1500"), ("2100", "2300"))),
    "AG.SHF": Instrument("AG.SHF", "沪银", "futures", 15, 1.0, 1, fee_rate=1e-5,
                         margin=0.12, sessions=(("0900", "1015"), ("1030", "1130"),
                                                ("1330", "1500"), ("2100", "0230"))),
    "C.DCE":  Instrument("C.DCE", "玉米", "futures", 10, 1.0, 1, fee_per_lot=1.2,
                         margin=0.08, sessions=(("0900", "1015"), ("1030", "1130"),
                                                ("1330", "1500"), ("2100", "2300"))),
    # ---------- A 股（示例；可自行替换成任意 6 位代码 + .SH/.SZ/.BJ） ----------
    "600519.SH": Instrument("600519.SH", "贵州茅台", "stock", 1, 0.01, 100,
                            fee_rate=2.5e-4, fee_rate_sell=5e-4, min_fee=5.0,
                            t_plus_1=True,
                            sessions=(("0930", "1130"), ("1300", "1500"))),
    "000001.SZ": Instrument("000001.SZ", "平安银行", "stock", 1, 0.01, 100,
                            fee_rate=2.5e-4, fee_rate_sell=5e-4, min_fee=5.0,
                            t_plus_1=True,
                            sessions=(("0930", "1130"), ("1300", "1500"))),
    "510300.SH": Instrument("510300.SH", "沪深300ETF", "stock", 1, 0.001, 100,
                            fee_rate=2.5e-4, fee_rate_sell=5e-4, min_fee=5.0,
                            t_plus_1=True,
                            sessions=(("0930", "1130"), ("1300", "1500"))),
}


def resolve(symbol: str) -> Instrument:
    """按代码取品种规则；不在表里则按代码后缀猜一个合理默认。"""
    if symbol in PRESETS:
        return PRESETS[symbol]
    up = symbol.upper()
    if up.endswith((".SH", ".SZ", ".BJ")):
        return Instrument(symbol, symbol, "stock", 1, 0.01, 100,
                          fee_rate=2.5e-4, fee_rate_sell=5e-4, min_fee=5.0,
                          t_plus_1=True,
                          sessions=(("0930", "1130"), ("1300", "1500")))
    return Instrument(symbol, symbol, "futures", 10, 1.0, 1, fee_per_lot=3.0,
                      margin=0.10, sessions=(("0900", "1500"),))


def is_fund(symbol: str) -> bool:
    """ETF / LOF 判断：这类代码要用 tushare 的 fund_daily，而不是 daily。

    踩过的坑：把 510300.SH 当普通股票取数，daily 返回空 -> 「未取到日线」。
    规则：沪市 5 开头（50/51/52/56/58…）、深市 15/16 开头是基金。
    """
    code = symbol.split(".")[0]
    return code.startswith(("5", "15", "16")) and len(code) == 6


def list_presets(market: str | None = None) -> list[Instrument]:
    items = list(PRESETS.values())
    return [i for i in items if market is None or i.market == market]
