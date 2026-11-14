"""仓位与板块集中度限制。

铁律（来自 docs/交易铁律.md）：
    单票 ≤15%（risk.max_single_position，2026-11-14 由 25% 收紧），同板块 ≤40%
    分散，是对自己无知的承认
    绝不向下补仓
"""

from __future__ import annotations

from ..types import AccountSnapshot
from .limits import MAX_SECTOR_POSITION, MAX_SINGLE_POSITION


class PositionLimiter:
    """仓位/板块硬约束。"""

    MAX_SINGLE = MAX_SINGLE_POSITION          # P2-9-3：常量收敛 → risk/limits.py
    MAX_SECTOR = MAX_SECTOR_POSITION

    def __init__(
        self,
        max_single: float = MAX_SINGLE_POSITION,
        max_sector: float = MAX_SECTOR_POSITION,
    ):
        self.max_single = max_single
        self.max_sector = max_sector

    def can_add(
        self,
        ts_code: str,
        invest_amount: float,
        account: AccountSnapshot,
        sector: str = "",
    ) -> tuple[bool, str]:
        """检查加仓后是否违反仓位/板块限制。

        Returns:
            (是否允许, 原因)
        """
        if account.total_asset <= 0:
            return False, "账户总资产未知"

        # 单票仓位（含现有 + 本次拟投入）
        existing_mv = sum(
            p.market_value for p in account.positions if p.ts_code == ts_code
        )
        single_pct = (existing_mv + invest_amount) / account.total_asset
        # 2026-11-14：加浮点容差。仓位口径统一为 15% 后，策略侧算出的
        # 0.15 × 总资产 恰好顶格，但 15000.0 / 100000 在 IEEE-754 下是
        # 0.15000000000000002 > 0.15 → 所有"顶格"仓位被误拒（回测 0 成交）。
        # 语义是"占比 ≤ 上限"，边界值必须放行，故比较时留 1e-9 容差。
        if single_pct > self.max_single + 1e-9:
            return False, f"单票占比 {single_pct:.1%} 超过 {self.max_single:.0%}"

        # 板块合计
        if sector:
            sector_existing = sum(
                p.market_value
                for p in account.positions
                if p.sector == sector
            )
            # 排除本票已计入的部分，避免重复
            sector_existing_excl = sector_existing - existing_mv if sector else 0
            sector_pct = (sector_existing_excl + existing_mv + invest_amount) / account.total_asset
            if sector_pct > self.max_sector + 1e-9:   # 同上：边界值放行
                return False, f"板块合计 {sector_pct:.1%} 超过 {self.max_sector:.0%}"

        return True, "通过"

    def can_average_down(
        self,
        ts_code: str,
        account: AccountSnapshot,
    ) -> tuple[bool, str]:
        """铁律：绝不向下补仓。对持仓亏损的标的拒绝补仓。"""
        pos = next((p for p in account.positions if p.ts_code == ts_code), None)
        if pos is None:
            return True, "无持仓，不适用"
        if pos.pnl_pct < 0:
            return False, f"持仓亏损 {pos.pnl_pct:.1%}，铁律禁止向下补仓"
        return True, "持仓盈利，可加仓"
