"""回测层：事件驱动引擎 + 撮合 + 持仓状态机 + 绩效指标。"""

from .broker import SimulatedBroker, Fill, Order
from .portfolio import Portfolio
from .metrics import compute_metrics
from .engine import EventDrivenEngine, BacktestResult

__all__ = [
    "SimulatedBroker", "Fill", "Order", "Portfolio",
    "compute_metrics", "EventDrivenEngine", "BacktestResult",
]
