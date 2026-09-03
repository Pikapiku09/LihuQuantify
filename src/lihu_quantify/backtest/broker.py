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
        self.limit_pct = 0.10   # 主板 ±10%（P1-2；ST/创业板不细分，代码留接口）

    @staticmethod
    def _pre_close_of(next_bar: pd.Series) -> float | None:
        """取前收盘价（涨跌停建模用）。缺失则回退前一根 close 不可得 → None。"""
        pre = next_bar.get("pre_close", None)
        if pre is None or (isinstance(pre, float) and pd.isna(pre)):
            # 无 pre_close 字段：用 open 无法判定涨跌停，返回 None（不做停板检查）
            return None
        f = float(pre)
        return f if f > 0 else None

    def fill(self, order: Order, next_bar: pd.Series) -> Fill | None:
        """在 T+1 日（next_bar）撮合订单。

        市价单：以 next_bar.open ± slippage 成交
        限价单：触及 limit_price 才成交（成交价与 open 比较，见 P1-3）
        成交量必须为 100 的整数倍。

        P1-2（第十一轮）：涨跌停/停牌无法成交建模——
            - 买单：next_bar.low >= 涨停价（一字板）→ 拒单
            - 卖单：next_bar.high <= 跌停价（一字跌停）→ 拒单
            - 停牌：volume==0 或 open 缺失/0 → 拒单（当日单当日废，不顺延）
            主板涨跌停 ±10%（round(pre_close × 1.1, 2)）；ST/创业板不细分，
            limit_pct 参数预留（简化假设，代码注释注明）。
        """
        if order.volume <= 0 or order.volume % self.LOT_SIZE != 0:
            return None

        open_price = float(next_bar.get("open", 0))
        high = float(next_bar.get("high", open_price))
        low = float(next_bar.get("low", open_price))
        fill_date = next_bar.get("trade_date")

        # —— 停牌：成交量显式为 0 或 open 缺失/0 → 无法成交，当日单当日废 ——
        # （无 vol 字段的 bar 视为有成交量，保持既有合成测试行为不变）
        vol_today = next_bar.get("volume", next_bar.get("vol"))
        if vol_today is not None and float(vol_today) <= 0:
            return None
        if pd.isna(open_price) or open_price <= 0:
            return None

        # —— 涨跌停：一字板无法成交 ——
        pre_close = self._pre_close_of(next_bar)
        if pre_close is not None:
            limit_up = round(pre_close * (1 + self.limit_pct), 2)
            limit_down = round(pre_close * (1 - self.limit_pct), 2)
            # P0 之外：涨停一字板（low>=涨停价）买单无法成交；跌停一字板（high<=跌停价）卖单无法成交
            if order.side == "buy" and low >= limit_up:
                return None
            if order.side == "sell" and high <= limit_down:
                return None

        if order.order_type == "limit":
            # 限价买：limit_price >= low 才能成交，成交价 = min(limit, open)（P1-3）
            if order.side == "buy":
                if order.limit_price < low:
                    return None
                price = min(order.limit_price, open_price)
            else:  # 卖
                if order.limit_price > high:
                    return None
                price = max(order.limit_price, open_price)
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
