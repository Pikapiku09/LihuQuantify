"""市场状态分类（P2-9-4：从 run_backtest.py 迁入包内，消除 scheduler 的 sys.path hack）。"""

from __future__ import annotations

import pandas as pd


def classify_market_state(idx_df: pd.DataFrame, window: int = 20) -> dict:
    """用 000001.SH 20 日涨跌幅把交易日分三段。返回 {trade_date: state}。

    state ∈ {上涨, 下跌, 震荡, 未知}。
    """
    if idx_df is None or idx_df.empty:
        return {}
    df = idx_df.sort_values("trade_date").reset_index(drop=True).copy()
    df["ret_20d"] = df["close"].pct_change(window) * 100
    states = {}
    for _, row in df.iterrows():
        if pd.isna(row["ret_20d"]):
            states[row["trade_date"]] = "未知"
        elif row["ret_20d"] >= 3:
            states[row["trade_date"]] = "上涨"
        elif row["ret_20d"] <= -3:
            states[row["trade_date"]] = "下跌"
        else:
            states[row["trade_date"]] = "震荡"
    return states