"""市场状态（regime）划分：门禁3 分状态稳健性检验用。

用指数日线（如 000001.SH，列含 trade_date/close）划分 bull / bear / range：
    - MA20 与 MA60 双上 → bull；双下 → bear；其余 → range
    - 叠加回撤过滤：距 250 日高点回撤 > 20% 强制 bear
输出与指数日线等长的 Series（index 对齐 df），供按日合并到面板。
"""

from __future__ import annotations

import numpy as np
import pandas as pd


def classify_regime(index_df: pd.DataFrame) -> pd.Series:
    """指数日线 → 每日 regime（bull/bear/range）。

    Args:
        index_df: 含 trade_date, close 的指数日线，按日期升序。
    """
    d = index_df.sort_values("trade_date").reset_index(drop=True)
    ma20 = d["close"].rolling(20, min_periods=20).mean()
    ma60 = d["close"].rolling(60, min_periods=60).mean()
    peak250 = d["close"].rolling(250, min_periods=60).max()
    dd = d["close"] / peak250 - 1.0

    regime = pd.Series("range", index=d.index, dtype="object")
    regime[(ma20 > ma60) & (d["close"] > ma20)] = "bull"
    regime[(ma20 < ma60) & (d["close"] < ma20)] = "bear"
    regime[dd < -0.20] = "bear"          # 深回撤强制熊市
    regime[dd > -0.05] = np.where(       # 浅回撤 + 均线上方 → 不判熊
        (ma20 > ma60)[dd > -0.05], "bull", regime[dd > -0.05])
    out = pd.Series(regime.values, index=d["trade_date"].values)
    return out


def regime_mask(panel: pd.DataFrame, regime: pd.Series) -> dict[str, pd.Series]:
    """把按日的 regime 映射到面板 df 的每一行，返回 {regime: bool mask}。"""
    reg_map = panel["trade_date"].map(regime)
    return {r: (reg_map == r).fillna(False) for r in ("bull", "bear", "range")}
