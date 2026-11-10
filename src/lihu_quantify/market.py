"""市场状态分类（P2-9-4：从 run_backtest.py 迁入包内，消除 scheduler 的 sys.path hack）。

2026-11-06 修复：单一20日累计涨幅判断在趋势反转初期严重滞后，导致 block 模式
持续"关门"错过上涨行情。新增趋势反转确认维度，使动量策略能在反弹初期即开仓。
"""

from __future__ import annotations

import pandas as pd


def classify_market_state(idx_df: pd.DataFrame, window: int = 20) -> dict:
    """用 000001.SH 趋势判断把交易日分四段。返回 {trade_date: state}。

    state ∈ {上涨, 下跌, 震荡, 未知}。

    判断逻辑（双维度，避免单一20日涨幅滞后错过趋势反转）：

    - **主维度**：20日累计涨跌幅
        - ≥ +3% → 上涨
        - ≤ -3% → 下跌（深度下跌段）
        - 其余 → 震荡区，交给辅维度裁决
    - **辅维度（趋势反转确认）**：当 20日涨幅落在 (-3%, +3%) 震荡区时，
      若短期动量出现明确多头信号，也判为"上涨"，捕捉反弹初期的开仓窗口：
        · MA5 > MA20 且 MA5 上行（均线多头排列初成），**或**
        · 近5日涨幅 ≥ +2%（V型快速反弹）
    - **深度下跌保护**：20日涨幅 ≤ -3% 的下跌段，短期反弹需近5日涨幅 ≥ +5%
      才翻多为"上涨"，避免下跌中继的假反弹骗开仓。
    """
    if idx_df is None or idx_df.empty:
        return {}
    df = idx_df.sort_values("trade_date").reset_index(drop=True).copy()
    df["ret_20d"] = df["close"].pct_change(window) * 100
    df["ma5"] = df["close"].rolling(5).mean()
    df["ma20"] = df["close"].rolling(window).mean()
    df["ret_5d"] = df["close"].pct_change(5) * 100
    df["ma5_prev"] = df["ma5"].shift(1)

    states = {}
    for _, row in df.iterrows():
        if pd.isna(row["ret_20d"]):
            states[row["trade_date"]] = "未知"
            continue

        r20 = row["ret_20d"]
        r5 = row["ret_5d"] if pd.notna(row["ret_5d"]) else 0.0
        ma5_up = (
            pd.notna(row["ma5"]) and pd.notna(row["ma5_prev"])
            and row["ma5"] > row["ma5_prev"]
        )
        ma5_above = (
            pd.notna(row["ma5"]) and pd.notna(row["ma20"])
            and row["ma5"] > row["ma20"]
        )

        if r20 >= 3:
            states[row["trade_date"]] = "上涨"
        elif r20 <= -3:
            # 深度下跌段：仅强反转（近5日 ≥ +5%）才翻多，否则保持下跌
            states[row["trade_date"]] = "上涨" if r5 >= 5 else "下跌"
        else:
            # 震荡区：趋势反转确认
            if (ma5_above and ma5_up) or r5 >= 2:
                states[row["trade_date"]] = "上涨"
            else:
                states[row["trade_date"]] = "震荡"
    return states
