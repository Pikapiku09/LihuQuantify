"""行为审计：把一次性诊断脚本产品化为可周期性复用的模块。

回答的问题不是「策略好不好」，而是「交易者本人是否在重复同样的行为偏差」：

- ``loss_add_ratio``   浮亏加仓占买入额比 —— 本项目实证的最大亏损来源（27.4%）
- ``peak_capture``     峰值捕获率 —— 中位 26.5%，96.2% 卖在峰值以下
- ``pgr_plr``          盈利/亏损单持有期差（处置效应的经典测度）
- ``post_sell_upside`` 卖出后是否还在涨 —— 94.4% 的盈利单卖后继续上涨
- ``turnover``         年化换手与摩擦成本 —— 6.5 → 29.6 倍，成本差 ≈ 6.93pp
- ``position_sizing``  单笔仓位分布与盈亏单规模比 —— 1.40x
"""

from __future__ import annotations

from .ingest import clean_cell, load_fills, to_float, to_trade_records, to_ts_code

__all__ = [
    "clean_cell",
    "load_fills",
    "to_float",
    "to_trade_records",
    "to_ts_code",
]
