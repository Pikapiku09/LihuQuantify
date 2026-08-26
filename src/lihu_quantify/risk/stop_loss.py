"""三档止损 + 移动止盈管理。

铁律（来自 docs/交易铁律.md）：
    1. 先写止损，再点买入（买入单+止损条件单同时挂）
    2. 成本 -8% 或跌破 10 日线，无条件离场
    3. 让盈利奔跑，让亏损快走
       - 盈利单挂移动止盈（回撤到 +3% 或破 10 日线离场）
       - 亏损单砍快

三档：
    -3% 预警（贴近 MA20）→ -5% 执行 → -8% 强制清仓
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, Optional

import pandas as pd

from ..types import Position


@dataclass
class StopAction:
    """止损/止盈触发动作。"""

    kind: Literal["hold", "warn", "execute", "force_stop", "trailing_stop", "ma_break"]
    reason: str = ""
    suggested_price: float = 0.0
    volume: int = 0     # 建议卖出股数（0=全部）


class StopLossManager:
    """止损/止盈评估。"""

    def __init__(
        self,
        warn_pct: float = -0.03,
        exec_pct: float = -0.05,
        force_pct: float = -0.08,
        trailing_pullback: float = 0.03,
        trailing_break_ma: int = 10,
    ):
        self.warn_pct = warn_pct
        self.exec_pct = exec_pct
        self.force_pct = force_pct
        self.trailing_pullback = trailing_pullback     # 盈利回撤 3% 离场
        self.trailing_break_ma = trailing_break_ma     # 或破 10 日线离场

    def evaluate(
        self,
        position: Position,
        bar: pd.Series,
        ma_vals: dict,
        high_water_mark: float = 0.0,
    ) -> StopAction:
        """评估单根 bar 是否触发止损/止盈。

        Args:
            position: 当前持仓
            bar: 当根 OHLCV（含 close/low）
            ma_vals: {"ma10": float, "ma20": float} 均线值
            high_water_mark: 持仓期最高价（移动止盈用，由 portfolio 维护）

        判定顺序（铁律优先级）：
            1. force_stop -8%：low 或 close 触及（铁律无条件，保留盘中 low）
            2. ma_break：收盘价跌破 10 日线（修复2：low→close，避免盘中洗盘）
            3. execute -5%：close 触及
            4. warn -3%：close 触及
            5. trailing_stop：浮盈后从高水位回撤 3%（修复1：移动止盈接入）
        """
        if position.volume <= 0 or position.cost <= 0:
            return StopAction(kind="hold")

        close = float(bar.get("close", 0))
        low = float(bar.get("low", close))
        ma10 = ma_vals.get("ma10", 0.0)
        ma20 = ma_vals.get("ma20", 0.0)

        # 1. 强制止损：成本 -8%（铁律无条件，保留盘中 low 判定）
        force_price = position.cost * (1 + self.force_pct)   # -8%
        if low <= force_price or close <= force_price:
            return StopAction(
                kind="force_stop",
                reason=f"成本-8%触发：成本{position.cost:.2f} → 强止损线{force_price:.2f}",
                suggested_price=force_price,
            )
        # 2. 跌破 10 日线：收盘价判定（修复2：原 low<ma10 盘中洗盘过紧）
        if ma10 > 0 and close < ma10:
            return StopAction(
                kind="ma_break",
                reason=f"收盘跌破 10 日线：MA10={ma10:.2f}，收盘{close:.2f}",
                suggested_price=ma10,
            )

        # 3. 执行止损：-5%
        exec_price = position.cost * (1 + self.exec_pct)
        if close <= exec_price:
            return StopAction(
                kind="execute",
                reason=f"成本-5%触发：{exec_price:.2f}",
                suggested_price=exec_price,
            )

        # 4. 预警：-3%
        warn_price = position.cost * (1 + self.warn_pct)
        if close <= warn_price:
            return StopAction(
                kind="warn",
                reason=f"成本-3%预警：{warn_price:.2f}",
                suggested_price=warn_price,
            )

        # 5. 移动止盈（修复1）：浮盈后从高水位回撤 3% 离场
        if high_water_mark > position.cost:
            trail_price = high_water_mark * (1 + self.trailing_pullback)  # 高水位×0.97
            if close <= trail_price:
                return StopAction(
                    kind="trailing_stop",
                    reason=f"移动止盈触发：高水位{high_water_mark:.2f} 回撤3% → 离场价{trail_price:.2f}",
                    suggested_price=trail_price,
                )

        return StopAction(kind="hold")

    def calc_stop_price(self, cost: float, ma10: float = 0.0) -> float:
        """计算建议止损价：max(成本-8%, MA10) 取先到先走 → 用 min。"""
        force_price = cost * (1 + self.force_pct)
        if ma10 > 0:
            return min(force_price, ma10)
        return force_price
