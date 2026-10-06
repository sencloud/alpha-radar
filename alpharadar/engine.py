"""事件驱动回测引擎（A 股 / 期货通用）。

撮合口径（偏保守，宁可低估收益）：
- 信号当根收盘确认，按收盘价 ± slippage_ticks 跳成交；
- 止损优先于止盈：同一根内两者都触及时按止损成交；
- 止盈按触发价成交（限价单，不加滑点）；开盘已越过止盈档则按开盘价（更优）；
- 止损、时间止损、收盘平仓均吃 1 次滑点；
- 成本：期货按手手续费（可叠加成交额比例），A 股按成交额佣金 + 卖出印花税 + 最低收费；
- A 股 T+1：当日买入当日不可卖出（引擎强制执行，含止损），因此 A 股必须过夜。

支持：定手数、分批止盈、结构止损 / 固定 ATR 止损 / 逐根跟踪止损、日内强平。
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from .universe import Instrument


@dataclass
class Position:
    direction: int
    entry: float
    stop: float
    lots: int
    entry_time: str
    entry_sdate: str
    tag: str = ""
    tps: list = field(default_factory=list)
    bars: int = 0
    risk_points: float = 0.0
    tgt_points: float = 0.0
    last_close: float = 0.0
    mfe: float = 0.0
    mae: float = 0.0
    filled: int = 0
    realized: float = 0.0          # Σ 已平仓（点数 × 手数）
    exit_turnover: float = 0.0     # Σ 已平仓（手数 × 成交价）
    reasons: list = field(default_factory=list)

    @property
    def remaining(self) -> int:
        return self.lots - self.filled


@dataclass
class BacktestResult:
    trades: pd.DataFrame
    equity: pd.DataFrame
    instrument: Instrument
    params: dict
    capital: float


class Backtester:
    """bar 级事件循环。bars 需含：trade_time/sdate/open/high/low/close/atr/sig。"""

    def __init__(self, bars: pd.DataFrame, instrument: Instrument,
                 params: dict, capital: float = 100_000.0):
        self.inst = instrument
        self.p = dict(params)
        self.capital = capital
        b = bars.reset_index(drop=True).copy()
        sd = b["sdate"].astype(str).to_numpy()
        b["x_last"] = np.concatenate([sd[1:] != sd[:-1], [True]])
        b["x_hm"] = b["trade_time"].dt.strftime("%H%M")
        if "st_stop" not in b.columns:
            b["st_stop"] = np.nan
        if "sig_tag" not in b.columns:
            b["sig_tag"] = ""
        # 体制闸门统一在引擎层执行：策略只产出原始信号，避免各策略口径不一
        if self.p.get("use_regime") and "regime_ok" in b.columns:
            from .indicators import apply_regime_gate
            b["sig"] = apply_regime_gate(b["sig"].to_numpy(np.int8), b, self.p)
        self.bars = b
        self.slip = params.get("slippage_ticks", 1) * instrument.tick
        self.unit = instrument.mult * instrument.lot    # 每手的名义单位数

    # ---------- 时段 ----------
    def _in_session(self, hm: str) -> bool:
        for s, e in self.inst.sessions:
            if not self.p.get("allow_night", 0) and (s >= "1800" or e <= "0600"):
                continue                                # 默认只做日盘
            if s <= e:
                if s <= hm <= e:
                    return True
            elif hm >= s or hm <= e:                    # 跨零点的夜盘
                return True
        return False

    def _entry_time_ok(self, hm: str) -> bool:
        if self.p.get("_daily"):
            return True                                 # 日线：不做盘中时段过滤
        if not self._in_session(hm):
            return False
        before, after = self.p.get("no_entry_before"), self.p.get("no_entry_after")
        day = "0900" <= hm <= "1500"
        if before and day and hm < before:
            return False
        if after and day and hm > after:
            return False
        return True

    # ---------- 成本 ----------
    def _commission(self, entry_turnover: float, exit_turnover: float, lots: int) -> float:
        i = self.inst
        if i.market == "futures":
            return (i.fee_per_lot * lots * 2
                    + i.fee_rate * (entry_turnover + exit_turnover))
        buy = max(i.min_fee, entry_turnover * i.fee_rate)
        sell = max(i.min_fee, exit_turnover * (i.fee_rate + i.fee_rate_sell))
        return buy + sell

    # ---------- 分批止盈档 ----------
    def _build_tps(self, entry: float, atr: float, sig: int):
        p = self.p
        if not p.get("use_target", 1):
            return [], max(1, int(p["lots"]))
        levels = tuple(p.get("partial_tps") or ()) or (float(p["tgt_atr"]),)
        per = max(1, int(p["lots"]))
        single = len(levels) == 1 and not tuple(p.get("partial_tps") or ())
        tps = [{"name": "止盈" if single else f"TP{i}",
                "px": entry + sig * float(k) * atr, "pts": float(k) * atr,
                "lots": per, "hit": False}
               for i, k in enumerate(levels, 1)]
        return tps, per * len(levels)

    def _fill(self, pos: Position, px: float, lots: int, reason: str) -> None:
        lots = min(int(lots), pos.remaining)
        if lots <= 0:
            return
        pos.realized += (px - pos.entry) * pos.direction * lots
        pos.exit_turnover += lots * px
        pos.filled += lots
        pos.reasons.append(reason)

    def _settle(self, pos: Position, sdate: str, out: list) -> float:
        i = self.inst
        L = pos.lots
        entry_turnover = pos.entry * self.unit * L
        exit_turnover = pos.exit_turnover * self.unit
        fee = self._commission(entry_turnover, exit_turnover, L)
        pnl = pos.realized * self.unit - fee
        hits = [r for r in pos.reasons if r.startswith("TP") or r == "止盈"]
        tail = [r for r in pos.reasons if r not in hits]
        reason = (("+".join(hits) + "→" if hits else "") + tail[-1]) if tail else \
            ("止盈" if hits == ["止盈"] else "+".join(hits) + "全平")
        out.append({
            "日期": sdate, "方向": "多" if pos.direction == 1 else "空", "手数": L,
            "开仓时间": pos.entry_time, "开仓": round(pos.entry, 3),
            "平仓": round(pos.exit_turnover / L if L else pos.entry, 3),
            "点数": round(pos.realized / L if L else 0.0, 3),
            "净利": round(pnl, 2), "费用": round(fee, 2), "原因": reason,
            "持有根数": pos.bars, "MFE": round(pos.mfe, 2), "MAE": round(pos.mae, 2),
            "止损点": round(pos.risk_points, 2), "止盈点": round(pos.tgt_points, 2),
            "形态": pos.tag,
        })
        return pnl

    # ---------- 单根 K 的持仓处理 ----------
    def _manage(self, pos: Position, o, h, l, c, is_last: bool, sdate: str) -> None:
        p, i = self.p, self.inst
        pos.bars += 1
        pos.mfe = max(pos.mfe, (h - pos.entry) * pos.direction)
        pos.mae = min(pos.mae, (l - pos.entry) * pos.direction)
        d = pos.direction

        if i.t_plus_1 and sdate == pos.entry_sdate:
            return                                       # A 股当日不可卖

        if (d == 1 and o <= pos.stop) or (d == -1 and o >= pos.stop):
            self._fill(pos, o - d * self.slip, pos.remaining, "止损")
            return
        for tp in pos.tps:
            if tp["hit"] or pos.remaining <= 0:
                continue
            if (d == 1 and o >= tp["px"]) or (d == -1 and o <= tp["px"]):
                self._fill(pos, o, min(tp["lots"], pos.remaining),
                           tp["name"] + ("(开高)" if d == 1 else "(开低)"))
                tp["hit"] = True
        if pos.remaining <= 0:
            return

        if (d == 1 and l <= pos.stop) or (d == -1 and h >= pos.stop):
            self._fill(pos, pos.stop - d * self.slip, pos.remaining, "止损")
            return
        for tp in pos.tps:
            if tp["hit"] or pos.remaining <= 0:
                continue
            if (d == 1 and h >= tp["px"]) or (d == -1 and l <= tp["px"]):
                self._fill(pos, tp["px"], tp["lots"], tp["name"])
                tp["hit"] = True
        if pos.remaining <= 0:
            return

        if pos.bars >= int(p.get("max_hold_bars", 10 ** 9)):
            self._fill(pos, c - d * self.slip, pos.remaining, "时间止损")
            return
        if not p.get("allow_overnight", 0) and is_last and not i.t_plus_1:
            self._fill(pos, c - d * self.slip, pos.remaining, "收盘平仓")

    # ---------- 主循环 ----------
    def run(self) -> BacktestResult:
        p, i = self.p, self.inst
        cash = self.capital
        pos: Position | None = None
        trades: list[dict] = []
        equity: list[tuple[str, float]] = []
        cooldown = {1: 0, -1: 0}
        day_count: dict[tuple[str, int], int] = {}

        cols = ["trade_time", "sdate", "x_hm", "x_last", "open", "high", "low",
                "close", "atr", "stop_px", "st_stop", "sig", "sig_tag"]
        for row in self.bars[cols].itertuples(index=False):
            sdate = str(row.sdate)
            for k in cooldown:
                cooldown[k] = max(0, cooldown[k] - 1)

            if pos:
                pos.last_close = float(row.close)
                self._manage(pos, float(row.open), float(row.high), float(row.low),
                             float(row.close), bool(row.x_last), sdate)
                if pos.remaining > 0 and p.get("trail_stop") and np.isfinite(row.st_stop):
                    lvl = float(row.st_stop)
                    pos.stop = (max(pos.stop, lvl) if pos.direction == 1
                                else min(pos.stop, lvl))
                if pos.remaining <= 0:
                    pnl = self._settle(pos, sdate, trades)
                    if pnl <= 0:
                        cooldown[pos.direction] = int(p.get("cooldown_bars", 0))
                    cash += pnl
                    pos = None
                elif p.get("breakeven_points") and \
                        (float(row.close) - pos.entry) * pos.direction >= p["breakeven_points"]:
                    pos.stop = (max(pos.stop, pos.entry) if pos.direction == 1
                                else min(pos.stop, pos.entry))

            if pos is None and int(row.sig) != 0 and self._entry_time_ok(str(row.x_hm)):
                sig = int(row.sig)
                key = (sdate, sig)
                if (cooldown[sig] == 0
                        and day_count.get(key, 0) < int(p.get("max_entries_per_day", 1))):
                    atr = float(row.atr)
                    entry = float(row.close) + sig * self.slip
                    if np.isfinite(atr) and atr > 0:
                        if p.get("stop_mode") == "atr":
                            stop = entry - sig * float(p["stop_atr"]) * atr
                            dist = float(p["stop_atr"]) * atr
                        else:
                            stop = float(row.stop_px)
                            dist = (entry - stop) * sig
                        if p.get("trail_stop"):
                            ok = np.isfinite(stop) and dist > 0
                        else:
                            ok = (np.isfinite(stop)
                                  and float(p.get("stop_min_points", 0)) <= dist
                                  <= float(p.get("stop_max_atr", 6)) * atr)
                        if ok:
                            tps, lots = self._build_tps(entry, atr, sig)
                            tgt = (sum(x["pts"] * x["lots"] for x in tps) / lots
                                   if tps else 0.0)
                            pos = Position(sig, entry, stop, lots, str(row.trade_time),
                                           sdate, str(row.sig_tag), tps,
                                           risk_points=dist, tgt_points=tgt,
                                           last_close=float(row.close))
                            day_count[key] = day_count.get(key, 0) + 1

            if bool(row.x_last):
                mtm = cash
                if pos:
                    mtm += (pos.realized * self.unit
                            + (pos.last_close - pos.entry) * pos.direction
                            * self.unit * pos.remaining)
                equity.append((sdate, mtm))

        if pos:
            self._fill(pos, pos.last_close or pos.entry, pos.remaining, "区间结束")
            last_date = equity[-1][0] if equity else str(self.bars["sdate"].iloc[-1])
            cash += self._settle(pos, last_date, trades)
            if equity:
                equity[-1] = (equity[-1][0], cash)

        return BacktestResult(trades=pd.DataFrame(trades),
                              equity=pd.DataFrame(equity, columns=["date", "equity"]),
                              instrument=i, params=p, capital=self.capital)


def run_strategy(bars: pd.DataFrame, instrument: Instrument, params: dict,
                 capital: float = 100_000.0) -> BacktestResult:
    """便捷入口：跑一次回测。"""
    return Backtester(bars, instrument, params, capital).run()
