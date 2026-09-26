"""P4 个股级波动率目标仓位（Volatility Targeting）。

方法：目标年化波动 σ_target，个股近 n 日实现波动 σ_realized（年化），
仓位缩放系数 = min(σ_target / σ_realized, cap)，作用于 Signal.suggested_position_pct。

文献依据：波动率目标（volatility targeting）经典结论——降波动资产的组合波动
而不损失长期收益（波动高时减仓、波动低时允许满仓），对 A 股高波动个股尤其有效。
验证（docs/papers/P4_波动率目标报告.md）：以「回撤下降 + 卡玛提升 + 夏普不降」为通过标准。

用法（策略层，_evaluate 返回信号前）：
    sizer = VolTargetSizer(target_vol=0.35, window=20)
    sizer.apply(signals, df)   # 就地缩放 suggested_position_pct
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from ..types import Signal


class VolTargetSizer:
    """个股级波动率目标仓位缩放器。"""

    def __init__(self, target_vol: float = 0.35, window: int = 20,
                 min_scale: float = 0.3, cap: float = 1.0,
                 trading_days: int = 252):
        """
        Args:
            target_vol: 目标年化波动率（如 0.35 = 35%）
            window: 实现波动回看窗口（交易日）
            min_scale: 缩放下限（防波动极端时仓位归零）
            cap: 缩放上限（默认 1.0——只减不加）
        """
        self.target_vol = target_vol
        self.window = window
        self.min_scale = min_scale
        self.cap = cap
        self.trading_days = trading_days

    def realized_vol(self, close: pd.Series) -> float:
        """近 window 日年化实现波动（最后一根 bar 的口径，无前视）。"""
        if len(close) < self.window // 2:
            return np.nan
        ret = close.iloc[-self.window:].pct_change().dropna()
        if len(ret) < 5:
            return np.nan
        return float(ret.std() * np.sqrt(self.trading_days))

    def scale(self, close: pd.Series) -> float:
        """缩放系数 = clip(target/realized, min_scale, cap)；波动缺失时 1.0（不干预）。"""
        rv = self.realized_vol(close)
        if not np.isfinite(rv) or rv <= 0:
            return 1.0
        return float(np.clip(self.target_vol / rv, self.min_scale, self.cap))

    def apply(self, signals: list[Signal], df: pd.DataFrame) -> list[Signal]:
        """对信号列表就地缩放仓位（df 需含 close 列、与信号同股票升序）。"""
        if not signals:
            return signals
        s = self.scale(df["close"])
        for sig in signals:
            if sig.kind == "buy":
                sig.suggested_position_pct = round(
                    sig.suggested_position_pct * s, 4)
                sig.reason = (sig.reason or "") + f" | vol_target×{s:.2f}"
        return signals
