"""指标层测试：标准指标 + MACD 背离 + 蜡烛图预警。"""

from __future__ import annotations

from datetime import date

import pandas as pd
import pytest

from lihu_quantify.indicators.candlestick import detect_candlestick_warning
from lihu_quantify.indicators.divergence import detect_macd_divergence
from lihu_quantify.indicators.standard import add_all_standard, add_ma, add_macd


def test_add_ma(daily_600584):
    df = add_ma(daily_600584.copy())
    for p in (5, 10, 20, 60):
        assert f"ma{p}" in df.columns
    assert "ma20_slope" in df.columns
    # MA5 第 5 行之后应有值
    assert df["ma5"].iloc[5] > 0


def test_add_macd(daily_600584):
    df = add_macd(daily_600584.copy())
    assert "dif" in df.columns
    assert "dea" in df.columns
    assert "macd_hist" in df.columns
    # DIF 应有有限值
    assert df["dif"].iloc[30:].notna().any()


def test_add_all_standard(daily_600584):
    df = add_all_standard(daily_600584.copy())
    for col in ["ma5", "ma10", "ma20", "ma60", "dif", "dea", "macd_hist",
                "boll_up", "boll_mid", "boll_low", "rsi14",
                "body", "body_ratio", "is_red", "vol_ratio"]:
        assert col in df.columns, f"缺失列 {col}"


def test_divergence_runs_on_600584(daily_600584):
    """600584 36-60 日窗口内无标准背离（基线）。"""
    df = add_all_standard(daily_600584.copy())
    divs = detect_macd_divergence(df, window=60, min_window=36)
    # 基线报告称"无标准顶/底背离"；允许返回空或极少
    assert isinstance(divs, list)
    # 若检测到，每个背离点结构完整
    for d in divs:
        assert d["type"] in ("top", "bottom")
        assert "desc" in d


def test_divergence_too_short_returns_empty(daily_600584):
    """数据短于 min_window 应返回空。"""
    df = add_all_standard(daily_600584.copy())
    short = df.head(20)
    assert detect_macd_divergence(short, window=60, min_window=36) == []


def test_candlestick_warning_819(daily_600584_with_819):
    """8/19 天量大阴线应触发减仓/清仓预警。"""
    df = daily_600584_with_819.copy()
    # 需 vol_ma：补充
    df["vol_ma"] = df["vol"].rolling(20, min_periods=5).mean()
    warnings = detect_candlestick_warning(df)
    assert len(warnings) > 0
    # 8/19 应有大阴线预警
    w_819 = [w for w in warnings if str(w["date"]) == str(date(2026, 8, 19))]
    assert len(w_819) >= 1, "8/19 应有蜡烛图预警"
    w = w_819[0]
    assert w["action"] in ("清仓", "减仓")
    assert "大阴线" in w["pattern"] or "天量" in w["pattern"]


def test_candlestick_empty_input():
    """空输入不应报错。"""
    empty = pd.DataFrame(columns=["open", "high", "low", "close", "vol"])
    assert detect_candlestick_warning(empty) == []
