"""持仓状态机：现金/持仓/交易历史 + high_water_mark（移动止盈用）。

铁律内化：
- T+1：买入当日不能卖出
- 绝不向下补仓：buy 时若该票已有亏损持仓，拒绝（由 PositionLimiter 把关）
- 移动止盈：跟踪每个持仓的最高价，回撤 3% 触发
"""

from __future__ import annotations

from datetime import date
from typing import Optional

import pandas as pd
from loguru import logger

from ..types import AccountSnapshot, Position, TradeRecord
from .broker import Fill


class Portfolio:
    """账户持仓状态机。"""

    def __init__(self, init_capital: float, max_single: float = 0.25, max_sector: float = 0.40):
        self.cash = init_capital
        self.init_capital = init_capital
        self.positions: dict[str, Position] = {}
        self.high_water_mark: dict[str, float] = {}    # ts_code -> 持仓期最高价
        self.buy_dates: dict[str, date] = {}             # ts_code -> 最近买入日（T+1 用）
        self.trades: list[TradeRecord] = []
        self.halted_until: Optional[date] = None
        self.max_single = max_single
        self.max_sector = max_sector

    # ===== 持仓更新 =====

    def apply_fill(self, fill: Fill) -> None:
        """应用成交回报。"""
        code = fill.ts_code
        self.cash += fill.cash_flow

        if fill.side == "buy":
            pos = self.positions.get(code)
            if pos is None:
                pos = Position(ts_code=code, volume=0, cost=0.0)
                self.positions[code] = pos
            # 加权平均成本
            old_val = pos.cost * pos.volume
            new_val = fill.price * fill.volume
            pos.volume += fill.volume
            pos.cost = (old_val + new_val) / pos.volume if pos.volume > 0 else fill.price
            self.buy_dates[code] = fill.fill_date
            if code not in self.high_water_mark or self.high_water_mark[code] < fill.price:
                self.high_water_mark[code] = fill.price
            self.trades.append(TradeRecord(
                ts_code=code, trade_date=fill.fill_date, side="buy",
                price=fill.price, volume=fill.volume, reason=fill.reason,
                commission=fill.commission, stamp_tax=fill.stamp_tax,
            ))
        else:  # sell
            pos = self.positions.get(code)
            if pos is None or pos.volume < fill.volume:
                return
            # 盈亏 = (卖出价 - 成本) * 股数
            pnl = (fill.price - pos.cost) * fill.volume
            pos.volume -= fill.volume
            if pos.volume <= 0:
                pos.volume = 0
                pos.cost = 0.0
                self.positions.pop(code, None)
                self.high_water_mark.pop(code, None)
                self.buy_dates.pop(code, None)
            self.trades.append(TradeRecord(
                ts_code=code, trade_date=fill.fill_date, side="sell",
                price=fill.price, volume=fill.volume, pnl=pnl, reason=fill.reason,
                commission=fill.commission, stamp_tax=fill.stamp_tax,
            ))
            # 修复A(第三轮)：连亏3笔 → 停手30天（铁律F，回测侧接线）
            # 与 PaperBroker.halt_map 同语义的回测路径：Checklist._check_frequency
            # 读取 account.halted_until 拦截后续买入
            if self.consecutive_losses(code) >= 3:
                from datetime import timedelta
                self.halted_until = fill.fill_date + timedelta(days=30)
                logger.warning(f"[铁律F] {code} 连亏3笔，停手至 {self.halted_until}")

    def update_prices(self, bar_by_code: dict[str, pd.Series]) -> None:
        """用最新 bar 更新持仓现价 + high_water_mark。"""
        for code, pos in list(self.positions.items()):
            bar = bar_by_code.get(code)
            if bar is None:
                continue
            close = float(bar.get("close", pos.current_price))
            pos.current_price = close
            # 更新高水位（移动止盈用）
            if code not in self.high_water_mark or self.high_water_mark[code] < close:
                self.high_water_mark[code] = close

    # ===== 查询 =====

    def can_sell(self, ts_code: str, today: date) -> bool:
        """T+1：买入当日不能卖出。"""
        buy_date = self.buy_dates.get(ts_code)
        if buy_date is None:
            return True
        # 同一日不允许卖（简化：比较 date）
        if isinstance(buy_date, date) and isinstance(today, date):
            return today > buy_date
        return True

    @property
    def position_value(self) -> float:
        return sum(p.market_value for p in self.positions.values())

    @property
    def total_asset(self) -> float:
        return self.cash + self.position_value

    def equity_curve_step(self) -> float:
        """单步权益。"""
        return self.total_asset

    def position_pct(self, ts_code: str) -> float:
        if self.total_asset <= 0:
            return 0.0
        pos = self.positions.get(ts_code)
        return pos.market_value / self.total_asset if pos else 0.0

    def sector_pct(self, sector_by_code: dict[str, str], sector: str) -> float:
        if self.total_asset <= 0:
            return 0.0
        mv = sum(
            p.market_value
            for code, p in self.positions.items()
            if sector_by_code.get(code, "") == sector
        )
        return mv / self.total_asset

    def trade_count_this_month(self, ts_code: str, today: date) -> int:
        return sum(
            1
            for t in self.trades
            if t.ts_code == ts_code
            and isinstance(t.trade_date, date)
            and t.trade_date.year == today.year
            and t.trade_date.month == today.month
        )

    def consecutive_losses(self, ts_code: str) -> int:
        sells = sorted(
            [t for t in self.trades if t.ts_code == ts_code and t.side == "sell"],
            key=lambda t: t.trade_date if isinstance(t.trade_date, date) else 0,
            reverse=True,
        )
        n = 0
        for t in sells:
            if t.pnl < 0:
                n += 1
            else:
                break
        return n

    def to_snapshot(self, sector_by_code: dict | None = None) -> AccountSnapshot:
        """转为 AccountSnapshot（供 Checklist/风控用）。"""
        positions = list(self.positions.values())
        if sector_by_code:
            for p in positions:
                p.sector = sector_by_code.get(p.ts_code, "")
        return AccountSnapshot(
            total_asset=self.total_asset,
            cash=self.cash,
            positions=positions,
            trades=list(self.trades),
            halted_until=self.halted_until,
        )
