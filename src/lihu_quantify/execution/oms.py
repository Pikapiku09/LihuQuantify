"""OMS 订单管理系统 —— 铁律的实盘守护者。

铁律（docs/交易铁律.md + 开仓前强制Checklist.md）：
    1. 先写止损，再点买入 —— 买入单 + 止损单必须同时挂出（两步合一，不许分开下单）
    2. 绝不向下补仓 —— 对亏损持仓的加仓请求直接拒绝
    3. 成本 -8% 或跌破 10 日线，无条件离场 —— 止损监控盘中轮询触发

修复B（第二轮）：stop_registry 持久化到 data/stop_registry.json，
    rebuild 优先从文件恢复（保留原始止损价），文件缺失才回退"成本-8%"。
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Optional

from loguru import logger

from ..types import AccountSnapshot, Signal
from .base import BrokerBase, OrderResult


def _default_registry_file() -> str:
    # src/lihu_quantify/execution/oms.py → 项目根/data/stop_registry.json
    return str(Path(__file__).resolve().parents[3] / "data" / "stop_registry.json")


@dataclass
class StopOrder:
    """已登记的止损单（本地程序化条件单）。"""

    ts_code: str
    volume: int                 # 监控股数
    stop_price: float           # 止损触发价
    buy_order_id: str = ""      # 对应买入单（审计用）
    triggered: bool = False     # 是否已触发
    triggered_at: Optional[date] = None
    reason: str = ""


class OrderManagementSystem:
    """订单管理：铁律执行。"""

    def __init__(
        self,
        broker: BrokerBase,
        poll_interval: float = 5.0,
        registry_file: Optional[str] = None,
        persist: bool = True,
    ):
        """
        Args:
            broker: 券商接口（实盘 MiniQMTClient / 模拟盘 PaperBroker）
            poll_interval: 止损监控轮询间隔（秒）
            registry_file: 止损登记持久化文件（默认 data/stop_registry.json）
            persist: 是否持久化（测试可关）
        """
        self.broker = broker
        self.poll_interval = poll_interval
        self.stop_registry: dict[str, StopOrder] = {}
        self.registry_file = registry_file or _default_registry_file()
        self.persist = persist
        if self.persist:
            self._load_registry()

    # ===== 持久化（修复B.2） =====

    def _save_registry(self) -> None:
        if not self.persist:
            return
        data = {
            code: {
                "ts_code": s.ts_code, "volume": s.volume,
                "stop_price": s.stop_price, "buy_order_id": s.buy_order_id,
                "triggered": s.triggered,
                "triggered_at": str(s.triggered_at) if s.triggered_at else None,
                "reason": s.reason,
            }
            for code, s in self.stop_registry.items()
        }
        try:
            path = Path(self.registry_file)
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(".tmp")
            tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
            import os
            os.replace(tmp, path)
            logger.debug(f"[OMS] 止损登记已持久化: {len(data)} 条")
        except OSError as e:
            logger.warning(f"[OMS] 止损登记写入失败: {e}")

    def _load_registry(self) -> None:
        path = Path(self.registry_file)
        if not path.exists():
            return
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            for code, s in data.items():
                ta = s.get("triggered_at")
                self.stop_registry[code] = StopOrder(
                    ts_code=s.get("ts_code", code),
                    volume=s.get("volume", 0),
                    stop_price=s.get("stop_price", 0.0),
                    buy_order_id=s.get("buy_order_id", ""),
                    triggered=s.get("triggered", False),
                    triggered_at=date.fromisoformat(ta) if ta else None,
                    reason=s.get("reason", ""),
                )
            if self.stop_registry:
                logger.info(f"[OMS] 止损登记恢复：{len(self.stop_registry)} 条（含原始止损价）")
        except (json.JSONDecodeError, ValueError, OSError) as e:
            logger.warning(f"[OMS] 止损登记文件损坏，忽略: {e}")

    # ===== 铁律 1：买入单 + 止损单同时挂 =====

    def place_buy_with_stop(
        self,
        signal: Signal,
        volume: int,
        buy_price: float,
    ) -> tuple[OrderResult, Optional[StopOrder]]:
        """原子化买入：止损价非法 → 拒绝买入；买入失败 → 不登记止损。

        Returns:
            (买入结果, 止损单（成功时）)
        """
        # 前置检查：止损价必须给出且低于买入价（铁律 1）
        if not signal.stop_loss or signal.stop_loss <= 0:
            return OrderResult(
                success=False, msg="铁律1：买入信号未给出止损价，拒绝下单"
            ), None
        if signal.stop_loss >= buy_price:
            return OrderResult(
                success=False,
                msg=f"铁律1：止损价 {signal.stop_loss:.2f} 不低于买入价 {buy_price:.2f}，拒绝下单",
            ), None
        if volume <= 0 or volume % 100 != 0:
            return OrderResult(
                success=False, msg=f"股数 {volume} 必须为 100 的正倍数"
            ), None

        # 铁律 2：绝不向下补仓（已有该票持仓且亏损 → 拒绝）
        positions = self.broker.query_positions()
        existing = next((p for p in positions if p.ts_code == signal.ts_code), None)
        if existing is not None and existing.cost > 0:
            cur = self.broker.get_price(signal.ts_code) or 0
            if cur > 0 and cur < existing.cost:
                return OrderResult(
                    success=False,
                    msg=f"铁律2：{signal.ts_code} 现持仓亏损（{cur:.2f} < 成本 {existing.cost:.2f}），禁止向下补仓",
                ), None

        # 下买入单
        buy_result = self.broker.buy(signal.ts_code, buy_price, volume)
        if not buy_result.success:
            logger.error(f"买入失败，不登记止损：{buy_result.msg}")
            return buy_result, None

        # 登记止损（程序化条件单）
        stop = StopOrder(
            ts_code=signal.ts_code,
            volume=volume,
            stop_price=signal.stop_loss,
            buy_order_id=buy_result.order_id,
            reason=f"买入@{buy_price:.2f} 止损@{signal.stop_loss:.2f}",
        )
        self.stop_registry[signal.ts_code] = stop
        self._save_registry()   # 修复B.2：持久化
        logger.info(
            f"[OMS] 买入+止损同时挂出：{signal.ts_code} {volume}股 "
            f"@{buy_price:.2f}，止损线 {stop.stop_price:.2f} "
            f"（-{(1 - signal.stop_loss / buy_price):.1%}）"
        )
        return buy_result, stop

    # ===== 止损监控（程序化条件单） =====

    def check_stops_once(self) -> list[OrderResult]:
        """单次检查所有止损单。触发则市价卖出。返回触发结果列表。"""
        triggered_results: list[OrderResult] = []
        for code, stop in list(self.stop_registry.items()):
            if stop.triggered:
                continue
            price = self.broker.get_price(code)
            if price <= 0:
                continue
            if price <= stop.stop_price:
                logger.warning(
                    f"[OMS] 止损触发：{code} 现价 {price:.2f} ≤ 止损线 {stop.stop_price:.2f}"
                )
                # 卖出（用当前价，实际可挂跌一分钱保证成交）
                sell_price = round(price - 0.01, 2)
                result = self.broker.sell(code, sell_price, stop.volume)
                if result.success:
                    stop.triggered = True
                    stop.triggered_at = date.today()
                    triggered_results.append(result)
                    self._save_registry()   # 修复B.2：触发状态持久化
                    logger.warning(f"[OMS] 止损卖出已挂：{code} {stop.volume}股 @ {sell_price:.2f}")
                else:
                    logger.error(f"[OMS] 止损卖出失败：{result.msg}，下一轮重试")
        return triggered_results

    def monitor_loop(self, max_seconds: Optional[float] = None) -> None:
        """盘中止损监控循环（阻塞）。

        Args:
            max_seconds: 最大运行时长（None=一直运行；测试用有限值）
        """
        logger.info(f"[OMS] 止损监控启动（每 {self.poll_interval}s 轮询），监控 {len(self.stop_registry)} 个持仓")
        t0 = time.time()
        while True:
            self.check_stops_once()
            if max_seconds is not None and time.time() - t0 >= max_seconds:
                break
            # 全部触发完则退出
            if self.stop_registry and all(s.triggered for s in self.stop_registry.values()):
                break
            time.sleep(self.poll_interval)
        logger.info("[OMS] 止损监控结束")

    # ===== 工具 =====

    def rebuild_stops_from_positions(
        self,
        stop_price_fn=None,
    ) -> int:
        """从当前持仓重建止损登记（程序崩溃恢复用）。

        Args:
            stop_price_fn: fn(ts_code, cost) -> stop_price；默认成本-8%

        注意：用总持仓量（崩溃恢复时跨日，冻结已过期）。
        """
        positions = self.broker.query_positions()
        count = 0
        for p in positions:
            if p.ts_code in self.stop_registry:
                continue
            cost = p.cost if p.cost > 0 else 0
            if stop_price_fn is not None:
                sp = stop_price_fn(p.ts_code, cost)
            else:
                sp = round(cost * 0.92, 2)   # 成本 -8%
            # query_positions 返回的 volume 是可用量；崩溃恢复场景下
            # 挂单冻结的量也应监控，这里用可用量 + 冻结量
            total_vol = p.volume + p.frozen
            if total_vol <= 0:
                continue
            self.stop_registry[p.ts_code] = StopOrder(
                ts_code=p.ts_code, volume=total_vol, stop_price=sp,
                reason=f"重建（成本{cost:.2f}）",
            )
            count += 1
        if count:
            logger.info(f"[OMS] 从持仓重建 {count} 个止损登记")
            self._save_registry()   # 修复B.2：重建后持久化
        return count

    def pending_stops(self) -> list[StopOrder]:
        """未触发的止损单列表。"""
        return [s for s in self.stop_registry.values() if not s.triggered]
