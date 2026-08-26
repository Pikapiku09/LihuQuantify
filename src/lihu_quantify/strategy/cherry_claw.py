"""CherryClaw 三层过滤选股策略。

对应 dsh-invest-plugin prompts.js P_SELECT：
    三层过滤：MA5 上穿 MA10 金叉 + 量比>1.0 + 收红；实体占比≥40%；
              收盘贴近 MA5；MA20 斜率向上；金叉新鲜度≤7天
    前置硬过滤：排除 688/300/301/ST/上市不足60日/近20日日均成交额<1亿
    候选硬约束：每只候选必须给出建议仓位（单票≤25%）与止损位（成本-8%或破10日线）
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from ..indicators.standard import add_all_standard
from ..risk.stop_loss import StopLossManager
from ..types import Signal
from .base import StrategyBase


class CherryClaw(StrategyBase):
    """三层过滤选股。"""

    name = "CherryClaw"
    stateless = True   # 信号只依赖当根 bar 指标（金叉新鲜度为指标列），可预计算

    # 前置硬过滤
    EXCLUDED_PREFIX = ("688", "300", "301")
    MIN_LIST_DAYS = 60
    MIN_AVG_AMOUNT_20D = 1e8

    def __init__(
        self,
        ma_periods=(5, 10, 20, 60),
        golden_cross_max_freshness: int = 7,
        volume_ratio_threshold: float = 1.0,
        entity_ratio_threshold: float = 0.40,
        close_to_ma5_max_dev: float = 0.015,
        max_position_pct: float = 0.25,
        stop_loss_force_pct: float = -0.08,
        **kwargs,
    ):
        super().__init__(**kwargs)
        self.ma_periods = ma_periods
        self.golden_cross_max_freshness = golden_cross_max_freshness
        self.volume_ratio_threshold = volume_ratio_threshold
        self.entity_ratio_threshold = entity_ratio_threshold
        self.close_to_ma5_max_dev = close_to_ma5_max_dev
        self.max_position_pct = max_position_pct
        self.stop_loss_mgr = StopLossManager(force_pct=stop_loss_force_pct)

    def _prepare_indicators(self, df: pd.DataFrame) -> dict:
        """添加全部标准指标。"""
        df_with_ind = add_all_standard(df)
        return {"df": df_with_ind}

    def pre_filter(self, df: pd.DataFrame) -> bool:
        """前置硬过滤：返回 True 表示通过。检查数据充分性。"""
        if df is None or len(df) < 30:
            return False
        ts_code = str(df["ts_code"].iloc[0]) if "ts_code" in df.columns else ""
        # 排除科创/创业板
        if any(ts_code.startswith(p) for p in self.EXCLUDED_PREFIX):
            return False
        # 上市天数（用数据长度近似）
        if len(df) < self.MIN_LIST_DAYS:
            return False
        # 近 20 日日均成交额 ≥ 1 亿（amount 单位千元，1亿元=1e5千元）
        if len(df) >= 20:
            avg_amount_20d = df["amount"].tail(20).mean()
            if avg_amount_20d < self.MIN_AVG_AMOUNT_20D / 1e3:   # 1e8元 → 1e5千元
                return False
        return True

    def _three_layer_filter(self, row: pd.Series) -> tuple[bool, str]:
        """三层过滤单根 bar。返回 (是否通过, 原因)。"""
        # 第 1 层：MA5 上穿 MA10 金叉 + 新鲜度 ≤7 天
        freshness = row.get("ma5_x_ma10")
        if pd.isna(freshness):
            return False, "无金叉信号"
        if freshness > self.golden_cross_max_freshness:
            return False, f"金叉已过 {int(freshness)} 天，超新鲜度阈值"
        # MA5 > MA10 维持
        if row["ma5"] <= row["ma10"]:
            return False, "MA5 未站上 MA10"

        # 第 2 层：量比 >1.0 + 实体占比 ≥40% + 收红
        if row.get("vol_ratio", 0) < self.volume_ratio_threshold:
            return False, f"量比 {row.get('vol_ratio', 0):.2f} < {self.volume_ratio_threshold}"
        if row.get("body_ratio", 0) < self.entity_ratio_threshold:
            return False, f"实体占比 {row.get('body_ratio', 0):.1%} < {self.entity_ratio_threshold:.0%}"
        if not row.get("is_red", False):
            return False, "收阴"

        # 第 3 层：收盘贴近 MA5 + MA20 斜率向上
        close = row["close"]
        ma5 = row["ma5"]
        if abs(close / ma5 - 1.0) > self.close_to_ma5_max_dev:
            return False, f"收盘乖离 MA5 {abs(close/ma5-1):.1%} > {self.close_to_ma5_max_dev:.1%}"
        if row.get("ma20_slope", 0) <= 0:
            return False, "MA20 斜率未向上"

        return True, "三层过滤通过"

    def _calc_targets(self, row: pd.Series) -> list[float]:
        """计算 L1-L4 目标价（基础版，深度诊断可细化）。"""
        close = row["close"]
        ma10 = row.get("ma10", close)
        ma20 = row.get("ma20", close)
        # L1: 通道上轨（MA10 上方 5%）
        l1 = close * 1.05
        # L2: 量度目标（前高附近，简化为 +10%）
        l2 = close * 1.10
        # L3: 突破延伸（+15%）
        l3 = close * 1.15
        # L4: 周线机会（+20%）
        l4 = close * 1.20
        return [l1, l2, l3, l4]

    def _evaluate(self, df: pd.DataFrame, indicators: dict) -> list[Signal]:
        """对带指标的 DataFrame 逐 bar 评估，返回通过过滤的买入信号。"""
        df_ind = indicators.get("df", df)
        if df_ind is None or df_ind.empty:
            return []

        if not self.pre_filter(df_ind):
            return []

        ts_code = str(df_ind["ts_code"].iloc[0]) if "ts_code" in df_ind.columns else ""
        signals: list[Signal] = []

        for i in range(len(df_ind)):
            row = df_ind.iloc[i]
            # 前几根指标未就绪，跳过
            if pd.isna(row.get("ma5")) or pd.isna(row.get("ma20")) or pd.isna(row.get("vol_ratio")):
                continue
            passed, reason = self._three_layer_filter(row)
            if not passed:
                continue
            close = float(row["close"])
            ma10 = float(row.get("ma10", close))
            stop_loss = self.stop_loss_mgr.calc_stop_price(close, ma10)
            targets = self._calc_targets(row)
            trade_date = row.get("trade_date")
            signals.append(Signal(
                kind="buy",
                ts_code=ts_code,
                suggested_price=close,
                stop_loss=stop_loss,
                take_profit=targets,
                suggested_position_pct=self.max_position_pct,
                strategy_name=self.name,
                reason=f"三层过滤通过：{reason}",
                trade_date=trade_date,
            ))
        return signals

    def latest_signal(self, df: pd.DataFrame) -> Signal | None:
        """取最新一根 bar 的信号（今日选股用）。"""
        signals = self.scan(df)
        return signals[-1] if signals else None
