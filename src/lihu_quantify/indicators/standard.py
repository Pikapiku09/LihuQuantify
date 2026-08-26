"""标准技术指标（基于 pandas-ta）。

覆盖 CherryClaw 选股与六维诊断所需：
- MA5/10/20/60（三层过滤 + 均线系统）
- MACD DIF/DEA/HIST（背离检测依赖）
- BOLL 上中下轨（支撑/阻力）
- RSI14（超买超卖）
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pandas_ta as ta


def add_ma(df: pd.DataFrame, periods=(5, 10, 20, 60)) -> pd.DataFrame:
    """添加 MA 均线 + MA20 斜率（向上判定）。

    三层过滤需要 MA5/10 金叉、MA20 斜率向上；
    均线系统诊断需要 MA5/10/20/60 多头排列。
    """
    df = df.copy()
    for p in periods:
        df[f"ma{p}"] = ta.sma(df["close"], length=p)
    # MA20 斜率（5 日差分），>0 视为向上
    df["ma20_slope"] = df["ma20"].diff(5) / 5.0 if "ma20" in df.columns else np.nan
    # MA5/MA10 金叉新鲜度（金叉后经过的 bar 数，None 表示无金叉）
    df["ma5_x_ma10"] = _golden_cross_freshness(df["ma5"], df["ma10"])
    return df


def _golden_cross_freshness(ma_fast: pd.Series, ma_slow: pd.Series) -> pd.Series:
    """计算金叉新鲜度：金叉后经过的 bar 数。无金叉信号时为 NaN。

    金叉定义：ma_fast 上穿 ma_slow（前一根 <=，当前 >）。
    """
    diff = ma_fast - ma_slow
    cross = (diff > 0) & (diff.shift(1) <= 0)
    freshness = pd.Series(np.nan, index=ma_fast.index, dtype="float64")
    last_cross_idx = -1
    for i in range(len(diff)):
        if i == 0:
            continue
        if cross.iloc[i]:
            last_cross_idx = i
            freshness.iloc[i] = 0
        elif last_cross_idx >= 0 and diff.iloc[i] > 0:
            freshness.iloc[i] = i - last_cross_idx
        elif diff.iloc[i] <= 0:
            last_cross_idx = -1
    return freshness


def add_macd(df: pd.DataFrame, fast=12, slow=26, signal=9) -> pd.DataFrame:
    """添加 MACD：DIF / DEA / HIST。

    背离检测需要 DIF 峰谷；六维诊断需要 MACD 零轴上下 + 拐头。
    """
    df = df.copy()
    macd_df = ta.macd(df["close"], fast=fast, slow=slow, signal=signal)
    if macd_df is None or macd_df.empty:
        return df
    # pandas-ta 列名：MACD_12_26_9 / MACDh_12_26_9 / MACDs_12_26_9
    df["dif"] = macd_df.iloc[:, 0]
    df["dea"] = macd_df.iloc[:, 2]
    df["macd_hist"] = macd_df.iloc[:, 1]
    return df


def add_boll_rsi(df: pd.DataFrame, length=20, std=2.0, rsi_length=14) -> pd.DataFrame:
    """添加 BOLL 上下轨 + RSI14。

    BOLL 中轨作为支撑/阻力参考；RSI 判超买(>70)/超卖(<30)。
    """
    df = df.copy()
    boll = ta.bbands(df["close"], length=length, std=std)
    if boll is not None and not boll.empty:
        df["boll_low"] = boll.iloc[:, 0]
        df["boll_mid"] = boll.iloc[:, 1]
        df["boll_up"] = boll.iloc[:, 2]
    df["rsi14"] = ta.rsi(df["close"], length=rsi_length)
    return df


def add_all_standard(df: pd.DataFrame) -> pd.DataFrame:
    """一次性添加全部标准指标。要求 df 至少有 close 列。"""
    df = add_ma(df)
    df = add_macd(df)
    df = add_boll_rsi(df)
    # 实体占比（CherryClaw 三层过滤第 2 层依赖）
    df["body"] = (df["close"] - df["open"]).abs()
    df["body_ratio"] = df["body"] / (df["high"] - df["low"]).replace(0, np.nan)
    # 收红（close > open）
    df["is_red"] = df["close"] > df["open"]
    # 量比（当根 vol / 5 日均 vol）
    df["vol_ma5"] = df["vol"].rolling(5).mean()
    df["vol_ratio"] = df["vol"] / df["vol_ma5"].replace(0, np.nan)
    return df
