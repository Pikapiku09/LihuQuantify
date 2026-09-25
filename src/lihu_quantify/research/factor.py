"""因子定义：面板 df 进、因子值出（stateless 向量化）。

注册模式：实现 f(df: pd.DataFrame) -> pd.Series 后用 @add_factor 注册，
即可被 ic.py / acceptance.py / 后续网格搜索按名字调用。
起步内置三个示范因子（对应论文引入方案研究项目 #1）：
    momentum       —— n 日动量（T+1 环境下 A 股常用 5-20 日短反转/中期动量混合区）
    reversal       —— n 日反转（负过去收益）
    turnover_premium —— 换手率因子（低换手溢价 / 高换手风险）
"""

from __future__ import annotations

from typing import Callable

import numpy as np
import pandas as pd

# 因子注册表：名字 -> 函数。因子函数输入面板 df，输出与 df 等长的 Series。
FACTOR_REGISTRY: dict[str, Callable[[pd.DataFrame], pd.Series]] = {}


def add_factor(name: str):
    """装饰器：注册因子到 FACTOR_REGISTRY。"""
    def deco(fn: Callable[[pd.DataFrame], pd.Series]) -> Callable[[pd.DataFrame], pd.Series]:
        FACTOR_REGISTRY[name] = fn
        fn.__name__ = name
        return fn
    return deco


def _grouped(df: pd.DataFrame) -> pd.DataFrame:
    """保证按 (ts_code, trade_date) 排序的副本。"""
    return df.sort_values(["ts_code", "trade_date"]).reset_index(drop=True)


@add_factor("momentum_20")
def momentum(df: pd.DataFrame, n: int = 20) -> pd.Series:
    """n 日动量：close / close.shift(n) - 1。值越大越看多。"""
    d = _grouped(df)
    ret = d.groupby("ts_code")["close"].transform(lambda s: s / s.shift(n) - 1.0)
    return ret


@add_factor("reversal_5")
def reversal(df: pd.DataFrame, n: int = 5) -> pd.Series:
    """n 日反转：-过去 n 日收益。值越大（过去跌越多）越看多。"""
    d = _grouped(df)
    ret = -d.groupby("ts_code")["close"].transform(lambda s: s / s.shift(n) - 1.0)
    return ret


@add_factor("turnover_20")
def turnover_premium(df: pd.DataFrame, n: int = 20) -> pd.Series:
    """换手率因子（取负 → 低换手看多）。amount/close 近似成交额换手需要流通股本，
    无股本数据时用 amount 的 n 日均值 z-score 的负值近似（高流动性股票折价）。
    """
    d = _grouped(df)
    amt = d["amount"].astype(float)
    mean = amt.groupby(d["ts_code"]).transform(lambda s: s.rolling(n, min_periods=n // 2).mean())
    std = amt.groupby(d["ts_code"]).transform(lambda s: s.rolling(n, min_periods=n // 2).std())
    z = (amt - mean) / std.replace(0, np.nan)
    return -z


@add_factor("residual_vol")
def residual_vol(df: pd.DataFrame, halflife: int = 42) -> pd.Series:
    """低残差波动因子（Barra ResVol/DASTD 简化版）。值越大（波动越低）越看多。

    验收（P1 门禁2 R4，2026-09-25，2019-2024 × 196 只分层池，horizon=20，n_trials=15）：
        RankIC=+0.0833  ICIR=0.40  t=14.8  多空年化+8.94%(成本后)  单调ρ=0.90
        icDSR=0.951  regime一致=1.00  与现有因子相关≤0.12（独立维度）
    构造来源：中泰证券 Barra(CNE6) 上篇（十年 IC=-0.050 外部佐证）。
    简化说明：市场模型残差以去均值收益近似；正式 Barra 口径需对市场收益回归取残差。
    """
    ret = df.groupby("ts_code")["close"].transform(lambda s: s.pct_change())
    ew_var = (ret ** 2).groupby(df["ts_code"], observed=True).transform(
        lambda x: x.ewm(halflife=halflife, min_periods=60).mean())
    return -np.sqrt(ew_var)


@add_factor("intraday_rev_20")
def intraday_rev_20(df: pd.DataFrame, n: int = 20) -> pd.Series:
    """20 日累计日内收益反转（剔除隔夜）。值越大（日内涨越少）越看多。

    验收（P1 门禁2 R4，同上口径）：
        RankIC=+0.0690  ICIR=0.47  t=17.6  多空年化+15.62%  单调ρ=1.00
        icDSR=0.976  regime一致=1.00
    定位：反转族替换实现（与 rev_mom_20 截面相关 0.83，按 <0.65 入库纪律不独立入库，
    作为反转族代表使用——ICIR/多空均优于 rev_mom_20 的 0.36/+13.4%）。
    构造来源：国泰海通「20 日日内收益」因子口径。
    """
    intraday = np.log(df["close"] / df["open"])
    return -intraday.groupby(df["ts_code"], observed=True).transform(
        lambda x: x.rolling(n, min_periods=n // 2).sum())


def compute_factor(df: pd.DataFrame, name: str, **params) -> pd.Series:
    """按注册名计算因子。"""
    if name not in FACTOR_REGISTRY:
        raise KeyError(f"未注册因子: {name}（已注册: {list(FACTOR_REGISTRY)}）")
    return FACTOR_REGISTRY[name](df, **params)
