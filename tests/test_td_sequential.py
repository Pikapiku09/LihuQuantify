"""九转序列回归测试。

基线（samples/reports/长电科技_深度分析_含Checklist闸门.md §10）：
    600584.SH 在 2026-08 上旬到中旬完成"卖出序列 1→9 完整周期"，
    8/19 收盘跌破 4 日前收盘（77.74 < 77.82）确认序列终结，转为买入序列第 1 根。

算法说明：
    标准 DeMark TD Setup：close[i] vs close[i-4]（lookback=4，"4 根前收盘价"）。
    报告中"8/6-8/18 1→9"为 LLM 摘要；按标准定义，序列起点为 8/5（68.97>7/30 的 64.51），
    衰竭点在 8/17（85.85>8/11 的 77.67）。本测试验证标准定义下的序列正确性，
    并验证 8/19 反转启动买入序列（与报告 §10 "买入序列第 1 根" 一致）。
"""

from __future__ import annotations

from datetime import date

import pandas as pd

from lihu_quantify.indicators.td_sequential import (
    describe_td_state,
    latest_td_state,
    td_sequential,
)


def _get_row(df_result: pd.DataFrame, daily: pd.DataFrame, target_date: date) -> pd.Series:
    """从结果中取指定日期的行。"""
    daily_indexed = daily.set_index("trade_date")
    res = df_result.set_index(daily_indexed.index)
    return res.loc[target_date]


def test_td_sequence_sell_series_600584(daily_600584_with_819):
    """验证 600584 卖出序列 1→9 + 8/19 反转。"""
    daily = daily_600584_with_819
    res = td_sequential(daily["close"], lookback=4, exhaustion=9)

    # --- 卖出序列起点 8/5（68.97 > 4日前 7/30 的 64.51）---
    row_85 = _get_row(res, daily, date(2026, 8, 5))
    assert row_85["td_dir"] == "sell", f"8/5 应为卖出序列起点，实际 {row_85['td_dir']}"
    assert row_85["td_count"] == 1, f"8/5 计数应为 1，实际 {row_85['td_count']}"

    # --- 衰竭点 8/17（85.85 > 4日前 8/11 的 77.67），count=9 ---
    row_817 = _get_row(res, daily, date(2026, 8, 17))
    assert row_817["td_dir"] == "sell", f"8/17 应为卖出序列，实际 {row_817['td_dir']}"
    assert row_817["td_count"] == 9, f"8/17 应到第 9 根衰竭，实际 {row_817['td_count']}"
    assert bool(row_817["td_exhausted"]) is True, "8/17 应标记为衰竭"

    # --- 8/18 继续同向（85.42 > 4日前 8/12 的 78.18），count=10 ---
    row_818 = _get_row(res, daily, date(2026, 8, 18))
    assert row_818["td_dir"] == "sell"
    assert row_818["td_count"] == 10, f"8/18 应为第 10 根，实际 {row_818['td_count']}"
    assert bool(row_818["td_exhausted"]) is True

    # --- 8/19 反转（77.74 < 4日前 8/13 的 77.82），买入序列第 1 根 ---
    row_819 = _get_row(res, daily, date(2026, 8, 19))
    assert row_819["td_dir"] == "buy", f"8/19 应反转买入，实际 {row_819['td_dir']}"
    assert row_819["td_count"] == 1, f"8/19 应为买入第 1 根，实际 {row_819['td_count']}"
    assert bool(row_819["td_exhausted"]) is False


def test_td_sequence_full_progression(daily_600584_with_819):
    """验证 8/5→8/18 卖出序列连续递增 1→10。"""
    daily = daily_600584_with_819
    res = td_sequential(daily["close"], lookback=4, exhaustion=9)
    daily_indexed = daily.set_index("trade_date")
    res = res.set_index(daily_indexed.index)

    expected = [
        (date(2026, 8, 5), 1),
        (date(2026, 8, 6), 2),
        (date(2026, 8, 7), 3),
        (date(2026, 8, 10), 4),
        (date(2026, 8, 11), 5),
        (date(2026, 8, 12), 6),
        (date(2026, 8, 13), 7),
        (date(2026, 8, 14), 8),
        (date(2026, 8, 17), 9),
        (date(2026, 8, 18), 10),
    ]
    for d, expected_count in expected:
        row = res.loc[d]
        assert row["td_count"] == expected_count, (
            f"{d}: 期望计数 {expected_count}，实际 {row['td_count']}"
        )
        assert row["td_dir"] == "sell"


def test_td_describe(daily_600584_with_819):
    """验证 describe_td_state 文本输出。"""
    daily = daily_600584_with_819
    # 取到 8/17 的数据
    daily_to_817 = daily[daily["trade_date"] <= date(2026, 8, 17)].reset_index(drop=True)
    state = latest_td_state(daily_to_817["close"], lookback=4, exhaustion=9)
    desc = describe_td_state(state)
    assert "卖出" in desc
    assert "9" in desc or "衰竭" in desc

    # 8/19 反转后
    daily_to_819 = daily[daily["trade_date"] <= date(2026, 8, 19)].reset_index(drop=True)
    state_819 = latest_td_state(daily_to_819["close"], lookback=4, exhaustion=9)
    desc_819 = describe_td_state(state_819)
    assert "买入" in desc_819
    assert "1" in desc_819


def test_td_sequence_empty_and_short():
    """空序列和短序列不应报错。"""
    empty = pd.Series([], dtype=float)
    res = td_sequential(empty)
    assert len(res) == 0

    short = pd.Series([1.0, 2.0, 3.0])  # 不足 lookback
    res = td_sequential(short, lookback=4)
    assert (res["td_count"] == 0).all()


def test_td_sequence_buy_series():
    """构造一个 9 根买入序列验证。"""
    # 价格持续下跌，且每根 < 4 日前
    closes = [10, 9, 8, 7, 6, 5, 4, 3, 2, 1, 0.5, 0.1, 0.01]
    s = pd.Series(closes)
    res = td_sequential(s, lookback=4, exhaustion=9)
    # 第 5 根（index 4，值 6）开始 < 4 日前（10），买入 #1
    assert res.iloc[4]["td_dir"] == "buy"
    assert res.iloc[4]["td_count"] == 1
    # index 12（值 0.01）应为买入 #9 衰竭
    assert res.iloc[12]["td_count"] == 9
    assert bool(res.iloc[12]["td_exhausted"]) is True
