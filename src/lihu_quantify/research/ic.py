"""IC / 分组回测 / Deflated Sharpe（门禁2 核心计算）。

所有计算基于前瞻收益 fwd_ret（调用方用 close.shift(-h) 构造，避免本模块偷看未来）。
"""

from __future__ import annotations

import numpy as np
import pandas as pd
from scipy import stats


def forward_return(df: pd.DataFrame, horizon: int = 5) -> pd.Series:
    """未来 h 日收益（按 ts_code 分组 shift(-h)）。"""
    d = df.sort_values(["ts_code", "trade_date"])
    return d.groupby("ts_code")["close"].transform(lambda s: s.shift(-horizon) / s - 1.0)


def calc_ic(df: pd.DataFrame, factor: pd.Series, horizon: int = 5) -> pd.DataFrame:
    """逐日截面 IC。

    返回 DataFrame(index=trade_date, columns=[ic, rank_ic, n])：
        ic      —— Pearson 截面相关
        rank_ic —— Spearman 秩相关（主指标，对异常值稳健）
        n       —— 当日截面样本数
    """
    d = df.copy()
    # 对齐修复（评审 P2，2026-09-29）：原 .values 按位置贴——df 未预排序时
    # forward_return 内部 sort 后的位置与 d 错位 → IC 整列错配。改为按 index 对齐
    # （df 已排序时行为与旧口径完全一致，历史门禁数字不受影响）。
    if isinstance(factor, pd.Series):
        d["_factor"] = factor
    else:
        d["_factor"] = pd.Series(factor, index=d.index)
    d["_fwd"] = forward_return(d, horizon)
    d = d.dropna(subset=["_factor", "_fwd"])

    def _day(g: pd.DataFrame):
        if len(g) < 3:
            return pd.Series({"ic": np.nan, "rank_ic": np.nan, "n": len(g)})
        ic = g["_factor"].corr(g["_fwd"], method="pearson")
        ric = g["_factor"].corr(g["_fwd"], method="spearman")
        return pd.Series({"ic": ic, "rank_ic": ric, "n": len(g)})

    return d.groupby("trade_date").apply(_day, include_groups=False).dropna()


def quantile_backtest(
    df: pd.DataFrame,
    factor: pd.Series,
    n_groups: int = 5,
    horizon: int = 5,
    cost_bps: float = 15.0,
) -> dict:
    """逐日截面分组回测（等权、h 日持有、含双边成本）。

    返回 dict：
        group_returns  —— DataFrame(index=trade_date, columns=G1..Gn)，各组 h 日等权收益
        long_short     —— G_top - G1 序列（扣成本后）
        turnover       —— 多头组日度换手率序列（近似：持仓集合日变化比例）
        cost_bps       —— 单边成本（基点）
    """
    d = df.copy()
    d["_factor"] = factor.values
    d["_fwd"] = forward_return(d, horizon).values
    d = d.dropna(subset=["_factor", "_fwd"])

    rows = {}
    holdings_prev: set[str] | None = None
    turnovers = []
    for td, g in d.groupby("trade_date"):
        if len(g) < n_groups:
            continue
        try:
            labels = pd.qcut(g["_factor"].rank(method="first"), n_groups, labels=False)
        except ValueError:
            continue
        g = g.assign(_g=labels)
        ret = g.groupby("_g")["_fwd"].mean()
        rows[td] = {f"G{i+1}": ret.get(i, np.nan) for i in range(n_groups)}
        # 多头组（最高分位）换手
        cur = set(g.loc[g["_g"] == n_groups - 1, "ts_code"])
        if holdings_prev is not None and (holdings_prev or cur):
            turnovers.append(1.0 - len(cur & holdings_prev) / max(len(holdings_prev | cur), 1))
        holdings_prev = cur

    group_returns = pd.DataFrame(rows).T.sort_index()
    ls_gross = group_returns[f"G{n_groups}"] - group_returns["G1"]
    cost = cost_bps * 2 / horizon / 10_000.0  # 双边成本摊到每日（h 日调仓一次）
    long_short = ls_gross - cost
    return {
        "group_returns": group_returns,
        "long_short": long_short,
        "turnover": pd.Series(turnovers, dtype="float64"),
        "cost_bps": cost_bps,
    }


def deflated_sharpe(
    sr: float,
    sr_trials_std: float,
    n_obs: int,
    n_trials: int,
    skew: float = 0.0,
    kurtosis: float = 3.0,
) -> float:
    """Deflated Sharpe Ratio（Bailey & López de Prado 2014）。

    返回 DSR ∈ [0,1]：观测到的 SR 在校正"试了 n_trials 次中的最优"之后仍 > 0 的概率。
    经验判据：DSR >= 0.95 才认为该策略/因子不是数据挖掘的幸存者。

    Args:
        sr:            观测 Sharpe（日频，非年化；与 sr_trials_std 同口径）
        sr_trials_std: 全部试验 SR 的截面标准差（无记录时可用 0.5*|sr| 保守估计）
        n_obs:         收益样本数（天数）
        n_trials:      试验过的策略/因子/参数组合总数
        skew, kurtosis: 收益分布三/四阶矩（kurtosis 为非超额口径，正态=3）
    """
    if n_trials <= 1:
        sr0 = 0.0
    else:
        euler = 0.5772156649015329
        z1 = stats.norm.ppf(1.0 - 1.0 / n_trials)
        z2 = stats.norm.ppf(1.0 - 1.0 / (n_trials * np.e))
        sr0 = sr_trials_std * ((1.0 - euler) * z1 + euler * z2)
    denom = np.sqrt(max(1e-18, 1.0 - skew * sr + (kurtosis - 1.0) / 4.0 * sr**2))
    stat = (sr - sr0) * np.sqrt(max(n_obs - 1, 1)) / denom
    return float(stats.norm.cdf(stat))
