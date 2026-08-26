"""交易频率控制状态机。

铁律（来自 docs/交易铁律.md）：
    同一只票一个月 ≤3 次；连亏 3 笔，停手一个月
    机会永远有，本金不常有
"""

from __future__ import annotations

from datetime import date, timedelta

from ..types import AccountSnapshot, TradeRecord


class FrequencyGuard:
    """月内交易次数 + 连亏停手状态机。"""

    MAX_TRADES_PER_TICKER_MONTH = 3
    HALT_AFTER_CONSEC_LOSSES = 3
    HALT_DAYS = 30

    def __init__(
        self,
        max_per_ticker_month: int = 3,
        halt_after_losses: int = 3,
        halt_days: int = 30,
    ):
        self.max_per_ticker_month = max_per_ticker_month
        self.halt_after_losses = halt_after_losses
        self.halt_days = halt_days

    def can_trade(
        self,
        ts_code: str,
        account: AccountSnapshot,
        today: date,
    ) -> tuple[bool, str]:
        """检查是否允许交易。"""
        # 1. 连亏停手检查
        if account.halted_until is not None and today < account.halted_until:
            return False, f"连亏停手至 {account.halted_until}，剩余 {(account.halted_until - today).days} 天"

        # 2. 同票月内交易次数
        month_trades = sum(
            1
            for t in account.trades
            if t.ts_code == ts_code
            and t.trade_date.year == today.year
            and t.trade_date.month == today.month
        )
        if month_trades >= self.max_per_ticker_month:
            return False, f"本月 {ts_code} 已交易 {month_trades} 次，达上限 {self.max_per_ticker_month}"

        return True, "通过"

    def should_halt_after_loss(self, ts_code: str, account: AccountSnapshot) -> tuple[bool, date | None]:
        """检查是否触发连亏 3 笔停手。返回 (是否停手, 停手到期日)。"""
        # 取该票最近的卖出成交，按日期降序
        sells = sorted(
            [t for t in account.trades if t.ts_code == ts_code and t.side == "sell"],
            key=lambda t: t.trade_date,
            reverse=True,
        )
        consec_losses = 0
        for t in sells:
            if t.pnl < 0:
                consec_losses += 1
            else:
                break
            if consec_losses >= self.halt_after_losses:
                # 从最近一笔亏损日算起停手一个月
                halt_from = sells[0].trade_date
                return True, halt_from + timedelta(days=self.halt_days)
        return False, None

    @staticmethod
    def record_trade(
        trades: list[TradeRecord],
        ts_code: str,
        trade_date: date,
        side: str,
        price: float,
        volume: int,
        pnl: float = 0.0,
    ) -> list[TradeRecord]:
        """记录一笔成交到交易历史。"""
        trades.append(
            TradeRecord(
                ts_code=ts_code,
                trade_date=trade_date,
                side=side,
                price=price,
                volume=volume,
                pnl=pnl,
            )
        )
        return trades
