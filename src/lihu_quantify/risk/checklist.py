"""开仓强制 Checklist 8 项闸门。

铁则（来自 docs/开仓前强制Checklist.md）：
    任一项目为"拒绝" → 最终结论必须为"拒绝买入"，当天不许以任何理由下单。
    账户类数据只能来自用户提供，严禁编造；未提供则标注"未知"并提示补充。

8 项：
    1. 仓位预算 ≤25%
    2. 板块集中 ≤40%
    3. 止损预设（必须给出）
    4. 止盈预设（必须给出）
    5. 交易频率（月内≤3 + 连亏3停手）
    6. 追高检查（乖离10日线≤8%）
    7. 基本面核验
    8. 心理门禁
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from ..types import AccountSnapshot, ChecklistItem, ChecklistResult, Signal


@dataclass
class CheckContext:
    """Checklist 检查所需的行情/板块/基本面上下文。"""

    current_price: float = 0.0
    ma10: float = 0.0                 # 10 日均线（追高乖离 + 止损参考）
    sector: str = ""                  # 所属板块（板块集中度）
    fundamentals: Optional[dict] = None   # 估值位置/催化剂/风险点
    invest_amount: float = 0.0        # 本票拟投入金额（未知则 0）


class ChecklistGate:
    """8 项强制闸门。"""

    MAX_SINGLE_PCT = 0.25
    MAX_SECTOR_PCT = 0.40

    def __init__(self, chasing_high_threshold: float = 0.08):
        """追高阈值可配置（修复4：网格搜索需要）。

        铁律数值（仓位/板块/止损档位）不可配置，只有追高阈值参与网格。
        """
        self.CHASING_HIGH_THRESHOLD = chasing_high_threshold

    def check(
        self,
        signal: Signal,
        account: AccountSnapshot,
        ctx: CheckContext,
    ) -> ChecklistResult:
        """对买入信号执行 8 项检查。任一拒绝即整体拒绝。"""
        items: list[ChecklistItem] = [
            self._check_position(signal, account, ctx),
            self._check_sector(signal, account, ctx),
            self._check_stop_loss(signal),
            self._check_take_profit(signal),
            self._check_frequency(signal, account),
            self._check_chasing_high(signal, ctx),
            self._check_fundamentals(signal, ctx),
            self._check_psychology(account),
        ]
        return ChecklistResult(items=items)

    # ===== 8 项检查 =====

    def _check_position(
        self, signal: Signal, account: AccountSnapshot, ctx: CheckContext
    ) -> ChecklistItem:
        """1. 仓位预算 ≤25%。"""
        if account.total_asset <= 0 or ctx.invest_amount <= 0:
            return ChecklistItem(
                name="仓位预算",
                approved=False,
                value="未知",
                reason="账户总资产/拟投入金额未提供，无法计算占比",
            )
        pct = ctx.invest_amount / account.total_asset
        approved = pct <= self.MAX_SINGLE_PCT
        return ChecklistItem(
            name="仓位预算",
            approved=approved,
            value=f"{pct:.1%}（上限25%）",
            reason="通过" if approved else f"占比 {pct:.1%} 超过 25%",
        )

    def _check_sector(
        self, signal: Signal, account: AccountSnapshot, ctx: CheckContext
    ) -> ChecklistItem:
        """2. 板块集中 ≤40%。未知板块不拦截（标注未知，实盘需用户补充）。"""
        if not ctx.sector:
            return ChecklistItem(
                name="板块集中",
                approved=True,
                value="未知",
                reason="板块信息未提供，无法校验（实盘需补充）",
            )
        existing = account.sector_pct(ctx.sector)
        if account.total_asset <= 0:
            return ChecklistItem(name="板块集中", approved=True, value="未知", reason="账户总资产未知")
        new_pct = ctx.invest_amount / account.total_asset if ctx.invest_amount > 0 else 0.0
        total = existing + new_pct
        approved = total <= self.MAX_SECTOR_PCT
        return ChecklistItem(
            name="板块集中",
            approved=approved,
            value=f"现有 {existing:.1%} + 本票 {new_pct:.1%} = {total:.1%}（上限40%）",
            reason="通过" if approved else f"板块合计 {total:.1%} 超过 40%",
        )

    def _check_stop_loss(self, signal: Signal) -> ChecklistItem:
        """3. 止损预设（必须给出）。"""
        if signal.stop_loss and signal.stop_loss > 0:
            return ChecklistItem(
                name="止损预设",
                approved=True,
                value=f"止损价 {signal.stop_loss:.2f}",
                reason="通过",
            )
        return ChecklistItem(
            name="止损预设",
            approved=False,
            value="未给出",
            reason="买入信号未给出止损价（成本-8% 或破10日线）",
        )

    def _check_take_profit(self, signal: Signal) -> ChecklistItem:
        """4. 止盈预设（必须给出）。"""
        if signal.take_profit:
            tp = ", ".join(f"{p:.2f}" for p in signal.take_profit)
            return ChecklistItem(
                name="止盈预设",
                approved=True,
                value=f"目标价 {tp}",
                reason="通过",
            )
        return ChecklistItem(
            name="止盈预设",
            approved=False,
            value="未给出",
            reason="买入信号未给出止盈目标（L1-L4）",
        )

    def _check_frequency(self, signal: Signal, account: AccountSnapshot) -> ChecklistItem:
        """5. 交易频率：月内≤3 + 连亏3停手。"""
        # 连亏停手检查
        if account.halted_until is not None:
            today = signal.trade_date
            if today is not None and today < account.halted_until:
                return ChecklistItem(
                    name="交易频率",
                    approved=False,
                    value=f"停手至 {account.halted_until}",
                    reason="连亏 3 笔，停手一个月未到期",
                )
        # 月内交易次数
        if signal.trade_date is None:
            return ChecklistItem(name="交易频率", approved=True, value="未知", reason="无日期无法判断")
        month_trades = sum(
            1
            for t in account.trades
            if t.ts_code == signal.ts_code
            and t.trade_date.year == signal.trade_date.year
            and t.trade_date.month == signal.trade_date.month
        )
        approved = month_trades < 3
        return ChecklistItem(
            name="交易频率",
            approved=approved,
            value=f"本月已交易 {month_trades} 次（上限3）",
            reason="通过" if approved else f"本月交易 {month_trades} 次已达上限",
        )

    def _check_chasing_high(self, signal: Signal, ctx: CheckContext) -> ChecklistItem:
        """6. 追高检查：乖离 10 日线 ≤8%。"""
        if ctx.ma10 <= 0 or ctx.current_price <= 0:
            return ChecklistItem(
                name="追高检查",
                approved=False,
                value="未知",
                reason="现价或 MA10 未提供，无法计算乖离",
            )
        deviation = (ctx.current_price / ctx.ma10 - 1.0)
        approved = deviation <= self.CHASING_HIGH_THRESHOLD
        return ChecklistItem(
            name="追高检查",
            approved=approved,
            value=f"乖离 10 日线 {deviation:.1%}（阈值8%）",
            reason="通过" if approved else f"乖离 {deviation:.1%} 超过 8%，视为追高",
        )

    def _check_fundamentals(self, signal: Signal, ctx: CheckContext) -> ChecklistItem:
        """7. 基本面核验。"""
        f = ctx.fundamentals or {}
        # 至少要有买入逻辑
        if not signal.reason and not f:
            return ChecklistItem(
                name="基本面核验",
                approved=False,
                value="未提供",
                reason="买入逻辑与基本面均未提供",
            )
        return ChecklistItem(
            name="基本面核验",
            approved=True,
            value=f"逻辑：{signal.reason[:30]}" if signal.reason else "已提供",
            reason="通过",
        )

    def _check_psychology(self, account: AccountSnapshot) -> ChecklistItem:
        """8. 心理门禁。"""
        if account.psychology_alert:
            return ChecklistItem(
                name="心理门禁",
                approved=False,
                value="存在必须赚/报复/焦虑信号",
                reason="用户透露情绪信号，建议冷静，拒绝买入",
            )
        return ChecklistItem(
            name="心理门禁",
            approved=True,
            value="未透露异常情绪",
            reason="通过",
        )
