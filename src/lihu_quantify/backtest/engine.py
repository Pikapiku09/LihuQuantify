"""事件驱动回测引擎。

逐 bar 推送 → 策略 on_bar() → 信号 → Checklist 闸门 → 撮合（T+1）→ 持仓更新 → 止损检查
完整还原状态依赖逻辑（与 vectorbt 的核心区别）。

时序（避免未来函数）：
    日 T 开盘：撮合 T-1 产生的订单（以 T 的 open 成交）
    日 T 盘中：更新价格为 T 的 close
    日 T 盘后：止损检查 + 策略 on_bar(T) 产生新订单 → 待 T+1 撮合
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import date
from typing import Optional

import pandas as pd
from loguru import logger

from ..indicators.standard import add_all_standard
from ..risk.checklist import ChecklistGate, CheckContext
from ..risk.stop_loss import StopLossManager
from ..strategy.base import BarContext, StrategyBase
from ..types import Signal
from .broker import Order, SimulatedBroker
from .metrics import compute_metrics
from .portfolio import Portfolio


@dataclass
class BacktestResult:
    """回测结果。"""

    equity: pd.Series                       # 每日权益曲线
    trades: list                                  # TradeRecord 列表
    metrics: dict                                 # 绩效指标
    portfolio: Portfolio
    signals_generated: int = 0
    orders_rejected: int = 0


class EventDrivenEngine:
    """事件驱动回测引擎。"""

    def __init__(
        self,
        strategy: StrategyBase,
        broker: Optional[SimulatedBroker] = None,
        stop_loss_mgr: Optional[StopLossManager] = None,
        checklist_gate: Optional[ChecklistGate] = None,
        max_single: float = 0.25,
        market_states: Optional[dict] = None,
        market_filter_on: bool = True,
        market_filter_mode: str = "reduce",   # "block"=禁止开仓 | "reduce"=仓位减半
    ):
        self.strategy = strategy
        self.broker = broker or SimulatedBroker()
        self.stop_loss_mgr = stop_loss_mgr or StopLossManager()
        self.checklist_gate = checklist_gate or ChecklistGate()
        self.max_single = max_single
        # 市场状态过滤（修复A：holdout 验证"有效但脆弱"→降级为降仓信号）：
        # market_states = {trade_date: '上涨'|'震荡'|'下跌'|'未知'}（000001.SH 20日涨幅分段）
        # mode=block  ：非上涨段完全禁止开新仓（旧模式，holdout 显示脆弱）
        # mode=reduce ：非上涨段仓位减半（默认，2026-08-26 决策日志）
        # 持仓止损/止盈照常执行（风控不放松）
        self.market_states = market_states
        self.market_filter_on = market_filter_on
        self.market_filter_mode = market_filter_mode

    def _entry_position_scale(self, dt) -> float:
        """市场状态 → 新开仓仓位缩放系数。"""
        if not self.market_filter_on or not self.market_states:
            return 1.0
        state = self.market_states.get(dt)
        if state == "上涨":
            return 1.0
        if self.market_filter_mode == "block":
            return 0.0    # 完全禁止
        return 0.5        # reduce：仓位减半

    def run(
        self,
        data: dict[str, pd.DataFrame],
        init_capital: float,
        start: Optional[date] = None,
        end: Optional[date] = None,
        sector_by_code: Optional[dict] = None,
    ) -> BacktestResult:
        """运行回测。

        Args:
            data: {ts_code: 日线 DataFrame}（需含 trade_date 列）
            init_capital: 初始资金
            start/end: 回测区间（可选）
            sector_by_code: {ts_code: sector}（板块集中度用）
        """
        if not data:
            return BacktestResult(equity=pd.Series(dtype=float), trades=[], metrics={}, portfolio=Portfolio(init_capital))

        # 1. 预计算指标
        prepared: dict[str, pd.DataFrame] = {}
        for code, df in data.items():
            df = df.sort_values("trade_date").reset_index(drop=True)
            prepared[code] = add_all_standard(df)

        # 2. 性能优化：预建日期位置映射（O(1) 定位代替全表扫描）
        date_pos: dict[str, dict] = {
            code: {d: i for i, d in enumerate(df["trade_date"].tolist())}
            for code, df in prepared.items()
        }

        # 3. 无状态策略：预计算信号表（网格搜索必需，信号只依赖当根 bar 指标）
        signal_table: dict[str, dict] = {}   # {code: {trade_date: Signal}}
        if getattr(self.strategy, "stateless", False):
            for code, df in prepared.items():
                try:
                    sigs = self.strategy.scan(df)
                except Exception as e:
                    logger.warning(f"策略 scan 异常 {code}: {e}")
                    sigs = []
                signal_table[code] = {s.trade_date: s for s in sigs if s.trade_date is not None}
            logger.debug(f"无状态策略信号预计算完成: {sum(len(v) for v in signal_table.values())} 个信号")

        # 4. 对齐交易日
        all_dates = sorted(set().union(*[set(df["trade_date"]) for df in prepared.values()]))
        if start is not None:
            all_dates = [d for d in all_dates if d >= start]
        if end is not None:
            all_dates = [d for d in all_dates if d <= end]

        portfolio = Portfolio(init_capital, max_single=self.max_single)
        pending_orders: list[Order] = []
        equity_dict: dict = {}
        signals_generated = 0
        orders_rejected = 0

        for dt in all_dates:
            # 1. 撮合 T-1 的订单（以 T 的 open 成交）
            if pending_orders:
                for order in pending_orders:
                    pos_map = date_pos.get(order.ts_code, {})
                    i = pos_map.get(dt)
                    if i is None:
                        continue
                    next_bar = prepared[order.ts_code].iloc[i]
                    fill = self.broker.fill(order, next_bar)
                    if fill is not None:
                        # P1-4（第十一轮）：撮合时二次确认现金（双保险）——多单同日撮合时，
                        # 订单生成期的估算可能彼此重叠，"下单前估算"只防单笔，这里按实际成交
                        # 价对【当前剩余现金】缩量整手，任何路径都不透支。
                        if order.side == "buy":
                            per_lot = fill.price * 100 * (1 + self.broker.commission_rate)
                            max_lots = int(portfolio.cash // per_lot) if per_lot > 0 else 0
                            vol2 = min(order.volume, max_lots * 100)
                            if vol2 < 100:
                                continue
                            if vol2 != order.volume:
                                order = replace(order, volume=vol2)
                                fill = self.broker.fill(order, next_bar)
                                if fill is None:
                                    continue
                        portfolio.apply_fill(fill)
                pending_orders = []

            # 2. 更新价格为 T 的 close（O(1) 定位）
            bars_today: dict[str, pd.Series] = {}
            for code, df in prepared.items():
                i = date_pos[code].get(dt)
                if i is not None:
                    bars_today[code] = df.iloc[i]
            portfolio.update_prices(bars_today)

            # 3. 止损检查（持仓）
            for code in list(portfolio.positions.keys()):
                if not portfolio.can_sell(code, dt):
                    continue
                pos = portfolio.positions[code]
                bar = bars_today.get(code)
                if bar is None:
                    continue
                ma_vals = {
                    "ma10": float(bar.get("ma10", 0)) if pd.notna(bar.get("ma10")) else 0.0,
                    "ma20": float(bar.get("ma20", 0)) if pd.notna(bar.get("ma20")) else 0.0,
                }
                action = self.stop_loss_mgr.evaluate(
                    pos, bar, ma_vals,
                    high_water_mark=portfolio.high_water_mark.get(code, 0.0),
                )
                if action.kind in ("force_stop", "ma_break", "execute", "trailing_stop"):
                    vol = (pos.volume // 100) * 100
                    if vol > 0:
                        pending_orders.append(Order(
                            ts_code=code, side="sell", volume=vol,
                            order_type="market", trade_date=dt,
                            reason=action.reason,
                        ))

            # 4. 策略信号 → 买入（无状态策略查预计算表；有状态策略逐 bar 推送）
            #    市场状态（修复A降级为降仓信号）：非上涨段 scale<1（减半/禁止）；
            #    持仓止损在步骤3照常执行
            entry_scale = self._entry_position_scale(dt)
            for code, df in prepared.items():
                if len(df) < 30:
                    continue
                # 已持仓则不再加仓（铁律：不向下补仓）
                if code in portfolio.positions:
                    continue
                i = date_pos[code].get(dt)
                if i is None or i < 30:
                    continue
                last_bar = df.iloc[i]

                if signal_table:
                    # 无状态策略：查表（O(1)）
                    signal = signal_table.get(code, {}).get(dt)
                else:
                    # 有状态策略：逐 bar 推送（原始路径）
                    history = df.iloc[: i + 1]
                    ctx = BarContext(
                        bar=last_bar, history=history, indicators={"df": history},
                        position=portfolio.positions.get(code),
                        account=portfolio.to_snapshot(sector_by_code),
                        trade_date=dt,
                    )
                    try:
                        signal = self.strategy.on_bar(ctx)
                    except Exception as e:
                        logger.warning(f"策略 on_bar 异常 {code} {dt}: {e}")
                        continue

                if signal is None or signal.kind != "buy":
                    continue
                signals_generated += 1
                # Checklist 闸门
                account = portfolio.to_snapshot(sector_by_code)
                check_ctx = CheckContext(
                    current_price=float(last_bar.get("close", 0)),
                    ma10=float(last_bar.get("ma10", 0)) if pd.notna(last_bar.get("ma10")) else 0.0,
                    sector=(sector_by_code or {}).get(code, ""),
                    invest_amount=signal.suggested_position_pct * portfolio.total_asset,
                    fundamentals={"logic": signal.reason},
                )
                result = self.checklist_gate.check(signal, account, check_ctx)
                if not result.approved:
                    orders_rejected += 1
                    continue
                # 计算下单股数（100 的倍数；修复A：非上涨段按 scale 缩仓）
                price = signal.suggested_price or float(last_bar["close"])
                target_value = signal.suggested_position_pct * portfolio.total_asset * entry_scale
                volume = int(target_value / price / 100) * 100
                if volume < 100:
                    continue
                # P1-4（第十一轮）：买入现金充足性校验（防 T+1 跳空高开透支）。
                # 以下一根 T+1 开盘价（买价 = open - 滑点）估算单股成本，现金不足原量
                # → 按 cash // (估单手成本) 向下取整缩量；缩到不足 1 手 → 拒单。
                est_price = price
                ni = i + 1
                if ni < len(df):
                    nb = df.iloc[ni]
                    nb_open = float(nb.get("open", 0))
                    if nb_open > 0 and not pd.isna(nb_open):
                        est_price = nb_open * (1 - self.broker.slippage)
                per_lot = est_price * 100 * (1 + self.broker.commission_rate)
                max_lots = int(portfolio.cash // per_lot) if per_lot > 0 else 0
                volume = min(volume, max_lots * 100)
                if volume < 100:
                    continue
                pending_orders.append(Order(
                    ts_code=code, side="buy", volume=volume,
                    order_type="market", trade_date=dt,
                    reason=signal.reason,
                ))

            # 5. 记录权益
            equity_dict[dt] = portfolio.total_asset

        equity = pd.Series(equity_dict, name="equity")
        equity.index.name = "trade_date"
        metrics = compute_metrics(equity, portfolio.trades)
        return BacktestResult(
            equity=equity,
            trades=portfolio.trades,
            metrics=metrics,
            portfolio=portfolio,
            signals_generated=signals_generated,
            orders_rejected=orders_rejected,
        )
