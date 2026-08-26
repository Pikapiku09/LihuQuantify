"""模拟撮合：A 股规则（T+1、100 股起买、佣金/印花税/滑点）。

费率（来自 config/backtest）：
    佣金 万 2.5（最低 5 元）
    印花税 卖出千 1
    滑点 0.1%

T+1：买入当日不能卖出（铁律"不向下补仓"也在此体现）。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

import pandas as pd


@dataclass
class Order:
    """订单。"""

    ts_code: str
    side: Literal["buy", "sell"]
    volume: int                       # 股数（必须 100 的倍数）
    order_type: Literal["market", "limit"] = "market"
    limit_price: float = 0.0
    trade_date: object = None         # 信号产生日（T 日）
    reason: str = ""


@dataclass
class Fill:
    """成交回报。"""

    ts_code: str
    side: str
    price: float
    volume: int
    fill_date: object
    commission: float = 0.0
    stamp_tax: float = 0.0
    slippage_cost: float = 0.0
    cash_flow: float = 0.0    # 正=卖出回笼资金，负=买入支出
    reason: str = ""           # 触发原因（止损类型/策略信号），用于统计


class SimulatedBroker:
    """A 股模拟撮合。"""

    MIN_COMMISSION = 5.0           # 单笔佣金最低 5 元
    LOT_SIZE = 100                 # A 股 1 手 = 100 股

    def __init__(
        self,
        commission_rate: float = 0.00025,   # 万 2.5
        stamp_tax_rate: float = 0.0005,     # 卖出印花税 万 5（2023.8 起减半）
        slippage: float = 0.001,            # 0.1%
    ):
        self.commission_rate = commission_rate
        self.stamp_tax_rate = stamp_tax_rate
        self.slippage = slippage

    def fill(self, order: Order, next_bar: pd.Series) -> Fill | None:
        """在 T+1 日（next_bar）撮合订单。

        市价单：以 next_bar.open ± slippage 成交
        限价单：触及 limit_price 才成交
        成交量必须为 100 的整数倍。
        """
        if order.volume <= 0 or order.volume % self.LOT_SIZE != 0:
            return None

        open_price = float(next_bar.get("open", 0))
        high = float(next_bar.get("high", open_price))
        low = float(next_bar.get("low", open_price))
        fill_date = next_bar.get("trade_date")

        if order.order_type == "limit":
            # 限价买：limit_price >= low 才能成交，成交价 = min(limit, high)
            if order.side == "buy":
                if order.limit_price < low:
                    return None
                price = min(order.limit_price, high)
            else:  # 卖
                if order.limit_price > high:
                    return None
                price = max(order.limit_price, low)
        else:
            # 市价单：开盘价 ± 滑点
            slip = open_price * self.slippage
            price = open_price - slip if order.side == "buy" else open_price + slip

        if price <= 0:
            return None

        # 费用计算
        turnover = price * order.volume
        commission = max(turnover * self.commission_rate, self.MIN_COMMISSION)
        stamp_tax = turnover * self.stamp_tax_rate if order.side == "sell" else 0.0

        if order.side == "buy":
            cash_flow = -(turnover + commission)   # 买入支出
        else:
            cash_flow = turnover - commission - stamp_tax   # 卖出回笼

        return Fill(
            ts_code=order.ts_code,
            side=order.side,
            price=price,
            volume=order.volume,
            fill_date=fill_date,
            commission=commission,
            stamp_tax=stamp_tax,
            cash_flow=cash_flow,
            reason=order.reason,
        )
