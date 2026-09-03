"""跨层共享的领域类型：Signal / Position / AccountSnapshot / Checklist 等。

供 strategy / risk / backtest / execution 复用，保证接口一致。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Literal, Optional

SignalKind = Literal["buy", "sell", "reduce", "hold"]


@dataclass
class Signal:
    """策略产生的交易信号。

    铁律：买入信号必须给出 stop_loss（成本-8% 或破10日线）与 take_profit（L1-L4）。
    """

    kind: SignalKind
    ts_code: str
    suggested_price: float
    stop_loss: Optional[float] = None       # 买入信号必须给出
    take_profit: list[float] = field(default_factory=list)  # L1-L4 目标价
    suggested_position_pct: float = 0.0     # 建议仓位（≤25%）
    strategy_name: str = ""                  # 信号来源策略
    reason: str = ""
    trade_date: Optional[date] = None


@dataclass
class Position:
    """单只股票持仓。"""

    ts_code: str
    volume: int = 0                  # 持仓股数
    cost: float = 0.0                # 持仓成本价
    current_price: float = 0.0       # 当前价
    sector: str = ""                  # 所属板块（用于板块集中度计算）
    stop_loss: Optional[float] = None    # 已挂止损价
    take_profit: Optional[float] = None  # 已挂止盈价

    @property
    def market_value(self) -> float:
        return self.volume * self.current_price

    @property
    def cost_value(self) -> float:
        return self.volume * self.cost

    @property
    def pnl_pct(self) -> float:
        if self.cost <= 0:
            return 0.0
        return (self.current_price / self.cost - 1.0)


@dataclass
class TradeRecord:
    """历史成交记录（频率/连亏状态机依赖）。"""

    ts_code: str
    trade_date: date
    side: Literal["buy", "sell"]
    price: float
    volume: int
    pnl: float = 0.0             # 卖出时记录本笔盈亏
    reason: str = ""             # 触发原因（止损类型/策略信号），用于统计
    commission: float = 0.0      # 佣金（修复5c：费用占比统计）
    stamp_tax: float = 0.0      # 印花税（卖出）


@dataclass
class AccountSnapshot:
    """账户快照（Checklist 与风控状态机的输入）。

    铁律：账户类数据只能来自用户/实盘查询，严禁编造。
    """

    total_asset: float = 0.0          # 总资产
    cash: float = 0.0                 # 可用资金
    positions: list[Position] = field(default_factory=list)
    trades: list[TradeRecord] = field(default_factory=list)
    halted_until: Optional[date] = None   # 连亏停手到期日
    # P0-5（第十一轮）：心理门禁信号改为三态——None=无数据来源（checklist 走
    # "未知"分支，不拦截但标注）；True=存在情绪信号（拒绝）；False=已确认正常
    psychology_alert: Optional[bool] = None

    @property
    def position_value(self) -> float:
        return sum(p.market_value for p in self.positions)

    def position_pct(self, ts_code: str) -> float:
        """单票仓位占比。"""
        if self.total_asset <= 0:
            return 0.0
        mv = next((p.market_value for p in self.positions if p.ts_code == ts_code), 0.0)
        return mv / self.total_asset

    def sector_pct(self, sector: str) -> float:
        """同板块合计占比。"""
        if self.total_asset <= 0:
            return 0.0
        mv = sum(p.market_value for p in self.positions if p.sector == sector)
        return mv / self.total_asset


@dataclass
class ChecklistItem:
    """Checklist 单项检查结果。"""

    name: str
    approved: bool
    reason: str = ""
    value: str = ""             # 检查项数值（如仓位占比、乖离率）


@dataclass
class ChecklistResult:
    """Checklist 8 项闸门结果。任一拒绝即拦截。"""

    items: list[ChecklistItem] = field(default_factory=list)

    @property
    def approved(self) -> bool:
        return all(i.approved for i in self.items) if self.items else False

    def rejected_items(self) -> list[ChecklistItem]:
        return [i for i in self.items if not i.approved]
