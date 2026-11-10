"""策略基类：回测与实盘共用 on_bar 接口，保证策略逻辑不重写。

设计原则：
- scan(df) -> list[Signal]：批量扫描模式（选股/诊断），DataFrame 进、信号列表出
- on_bar(ctx) -> Signal | None：事件驱动模式（回测/实盘逐 bar 推送）
- 两种模式共享策略核心逻辑（_evaluate），只是数据切片方式不同
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Optional

import pandas as pd

from ..types import AccountSnapshot, Position, Signal


@dataclass
class BarContext:
    """on_bar 推送的上下文。"""

    bar: pd.Series                   # 当根 OHLCV
    history: pd.DataFrame            # 历史窗口（含当根，升序）
    indicators: dict                 # 已计算的指标列
    position: Optional[Position]     # 当前持仓（无则 None）
    account: Optional[AccountSnapshot]  # 账户快照
    trade_date: object = None        # 当根日期


class StrategyBase(ABC):
    """策略基类。子类实现 _evaluate(df, indicators) -> list[Signal]。

    stateless=True 的策略（信号只依赖当根 bar 指标，无跨日状态）
    允许引擎预计算信号表加速回测（网格搜索必需）。
    """

    name: str = "base"
    stateless: bool = False

    def __init__(self, **params):
        self.params = params

    @abstractmethod
    def _evaluate(self, df: pd.DataFrame, indicators: dict) -> list[Signal]:
        """核心策略逻辑：在带指标的 DataFrame 上评估，返回信号列表。"""

    def latest_signal(self, df: pd.DataFrame) -> Signal | None:
        """取【最新 bar】的信号（每日扫描用）。当日 bar 无信号 → None。

        2026-11-10 修复（P0-A）：原实现 ``return signals[-1]`` 返回的是「窗口内
        最后一根**合格** bar」的信号，不校验它是否属于最新 bar。对带限频的策略
        （IntradayReversal 距上一信号至少 10 个交易日）该信号可滞后数十个交易日：

          - 实测 605178.SH 于 2026-10-23 取到 2026-07-16 的信号（滞后 65 个交易日），
            旧止损 90.62 对今日执行价 64.15 → 触发「铁律1：止损价不低于买入价」；
          - 池内 200 只几乎「每天都有信号」（每日 196–200 个），本质是僵尸信号堆积，
            不是当日交易机会。

        口径与事件驱动路径 ``on_bar``（修复5b）对齐：只认最新 bar 当日的信号。
        """
        if df is None or df.empty:
            return None
        signals = self.scan(df)
        if not signals:
            return None
        last_date = df.sort_values("trade_date")["trade_date"].iloc[-1]
        for s in reversed(signals):
            if self._same_date(s.trade_date, last_date):
                return s
        return None

    @staticmethod
    def _same_date(a: object, b: object) -> bool:
        """宽松日期比较：date / Timestamp / 'YYYYMMDD' / 'YYYY-MM-DD' 混用时不误判。"""
        if a is None or b is None:
            return False
        try:
            return pd.Timestamp(a).normalize() == pd.Timestamp(b).normalize()
        except (ValueError, TypeError):
            return a == b

    def scan(self, df: pd.DataFrame) -> list[Signal]:
        """批量扫描模式：输入完整日线 DataFrame，返回信号列表。"""
        if df is None or df.empty:
            return []
        df = df.sort_values("trade_date").reset_index(drop=True)
        indicators = self._prepare_indicators(df)
        return self._evaluate(df, indicators)

    def on_bar(self, ctx: BarContext) -> Optional[Signal]:
        """事件驱动模式：逐 bar 推送，返回当根信号或 None。

        修复5b：当日 bar 无信号必须返回 None，删除 signals[-1] 兜底
        （否则被拒信号会在之后每天重复提交）。
        """
        signals = self._evaluate(ctx.history, ctx.indicators)
        if not signals:
            return None
        last_date = ctx.bar.get("trade_date")
        for s in reversed(signals):
            if self._same_date(s.trade_date, last_date):
                return s
        return None

    def _prepare_indicators(self, df: pd.DataFrame) -> dict:
        """子类可覆盖：在 scan 前准备指标。默认空。"""
        return {}
