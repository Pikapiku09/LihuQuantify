"""执行层：MiniQMT 实盘 + 模拟盘 + OMS（铁律内化）。

设计：
- BrokerBase 统一接口：实盘（xtquant）与模拟盘（回测撮合）可互换
- OMS 铁律：买入单 + 止损单必须同时挂出（两步合一，不许分开下单）
- 止损监控：盘中轮询价格，触发止损价自动卖出（程序化条件单，比 QMT 客户端条件单更可控）
"""

from .base import BrokerBase, OrderResult, PositionInfo
from .xtquant_client import MiniQMTClient
from .paper_trade import PaperBroker
from .oms import OrderManagementSystem

__all__ = [
    "BrokerBase", "OrderResult", "PositionInfo",
    "MiniQMTClient", "PaperBroker", "OrderManagementSystem",
]
