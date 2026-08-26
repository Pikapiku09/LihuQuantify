"""九转序列（TD Sequential）—— 自研实现。

定义（来自 dsh-invest-plugin prompts.js P_DEEP §7）：
    用收盘价与 4 根前收盘价比较逐日计数
    - 连续 9 根收盘 > 4 根前收盘 → 卖出序列（计数 1-9，9 为衰竭点）
    - 连续 9 根收盘 < 4 根前收盘 → 买入序列

回归基线（samples/reports/长电科技_深度分析_含Checklist闸门.md §10）：
    600584.SH 在 2026-08-06 ~ 2026-08-18 输出"卖出序列 1→9 完整周期"，
    8/18 第 9 根衰竭点（收 85.42），
    8/19 收盘跌破 4 日前收盘（77.74 < 77.82）确认序列终结，转为买入序列第 1 根。

验证：
    8/6  close=75.87  vs 4日前 7/31 close=65.71  → 75.87>65.71  卖出#1 ✓
    8/18 close=85.42  vs 4日前 8/12 close=78.18  → 85.42>78.18  卖出#9 ✓ 衰竭
    8/19 close=77.74  vs 4日前 8/13 close=77.82  → 77.74<77.82  买入#1 ✓
"""

from __future__ import annotations

import numpy as np
import pandas as pd


def td_sequential(
    close: pd.Series,
    lookback: int = 4,
    exhaustion: int = 9,
) -> pd.DataFrame:
    """计算九转序列。

    Args:
        close: 收盘价序列（要求按 trade_date 升序）
        lookback: 与几根前的收盘价比较，默认 4
        exhaustion: 衰竭点计数，默认 9

    Returns:
        DataFrame，列：
            td_count     int     当前序列计数（1..N，0 表示无序列）
            td_dir       str     'buy' | 'sell' | None
            td_exhausted bool    是否到达衰竭点（count >= exhaustion）
    """
    n = len(close)
    td_count = np.zeros(n, dtype=int)
    td_dir: list[str | None] = [None] * n
    td_exhausted = np.zeros(n, dtype=bool)

    cur_dir: str | None = None  # 'buy' | 'sell' | None
    cur_count = 0

    for i in range(n):
        if i < lookback:
            td_dir[i] = None
            continue
        prev = close.iloc[i - lookback]
        cur = close.iloc[i]
        if pd.isna(prev) or pd.isna(cur):
            td_dir[i] = cur_dir
            td_count[i] = cur_count
            td_exhausted[i] = cur_count >= exhaustion
            continue

        if cur > prev:
            bar_dir = "sell"  # 收盘高于 4 日前 → 卖出序列
        elif cur < prev:
            bar_dir = "buy"
        else:
            bar_dir = None

        if bar_dir is None:
            # 中性，保持当前状态但计数清零
            cur_count = 0
            cur_dir = None
        elif bar_dir == cur_dir:
            cur_count += 1
        else:
            # 方向反转，开启新序列
            cur_dir = bar_dir
            cur_count = 1

        td_count[i] = cur_count
        td_dir[i] = cur_dir if cur_count > 0 else None
        td_exhausted[i] = cur_count >= exhaustion

    return pd.DataFrame(
        {
            "td_count": td_count,
            "td_dir": td_dir,
            "td_exhausted": td_exhausted,
        },
        index=close.index,
    )


def latest_td_state(close: pd.DataFrame, **kwargs) -> dict:
    """取最近一根 bar 的九转状态，便于报告输出。"""
    res = td_sequential(close, **kwargs)
    if res.empty:
        return {"count": 0, "dir": None, "exhausted": False}
    last = res.iloc[-1]
    return {
        "count": int(last["td_count"]),
        "dir": last["td_dir"],
        "exhausted": bool(last["td_exhausted"]),
    }


def describe_td_state(state: dict) -> str:
    """生成报告文本，如"卖出序列第 7 根，距离 9 衰竭还有 2 根"。"""
    if state["dir"] is None or state["count"] == 0:
        return "当前无九转序列"
    label = "卖出" if state["dir"] == "sell" else "买入"
    if state["exhausted"]:
        return f"{label}序列已到第 {state['count']} 根衰竭点"
    remain = 9 - state["count"]
    return f"{label}序列第 {state['count']} 根，距离 9 衰竭还有 {remain} 根"
