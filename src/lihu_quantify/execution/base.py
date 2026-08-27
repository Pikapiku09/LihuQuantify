"""执行层抽象接口：实盘/模拟盘互换。"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Optional


@dataclass
class OrderResult:
    """下单结果。"""

    success: bool
    order_id: str = ""
    msg: str = ""
    filled_volume: int = 0
    filled_price: float = 0.0


@dataclass
class PositionInfo:
    """持仓信息（从券商/模拟盘查询）。"""

    ts_code: str
    volume: int = 0            # 可用持仓
    frozen: int = 0            # 冻结（挂单中）
    cost: float = 0.0          # 成本价
    market_value: float = 0.0


class BrokerBase(ABC):
    """券商接口抽象：实盘（MiniQMT）与模拟盘共用。"""

    @abstractmethod
    def connect(self) -> bool:
        """连接/初始化。"""

    @abstractmethod
    def buy(self, ts_code: str, price: float, volume: int, reason: str = "") -> OrderResult:
        """买入（volume 必须 100 倍数）。reason 为交易原因（记录用）。"""

    @abstractmethod
    def sell(self, ts_code: str, price: float, volume: int, reason: str = "") -> OrderResult:
        """卖出。reason 为离场原因（止损类型等，记录用）。"""

    @abstractmethod
    def cancel(self, order_id: str) -> OrderResult:
        """撤单。"""

    @abstractmethod
    def query_positions(self) -> list[PositionInfo]:
        """查询持仓。"""

    @abstractmethod
    def query_asset(self) -> dict:
        """查询账户资产。返回 {cash, total_asset, market_value}。"""

    @abstractmethod
    def get_price(self, ts_code: str) -> float:
        """查最新价（止损监控用）。"""

    def close(self) -> None:
        """断开（可选实现）。"""
