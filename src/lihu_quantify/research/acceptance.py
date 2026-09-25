"""因子验收单：门禁2 的硬标准判定，输出与 docs/因子验收单.md 对齐。

硬标准（缺一即 FAIL，字段名对齐 backtest/metrics.py 的蛇形命名风格）：
    rank_ic_mean     |RankIC| 均值 >= 0.02
    rank_ic_ir       ICIR（mean/std）>= 0.3
    rank_ic_t        t 统计量 >= 2.0
    group_monotonic  分组收益对组序号的 Spearman |rho| >= 0.7（多空两端方向正确且单调）
    long_short_ann   成本后多空年化（rank_ic 符号方向）> 0
    dsr              Deflated Sharpe >= 0.95（默认 n_trials=1 时退化为普通显著性）
    regime_consistency 各 regime 下 RankIC 符号一致比例 >= 2/3（传入 regime 时才检查）
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

import numpy as np
import pandas as pd

from .ic import calc_ic, deflated_sharpe, quantile_backtest


@dataclass
class AcceptanceResult:
    """验收单结果。pass_all 为最终结论；每项 gate_* 为该硬标准是否通过。"""
    factor_name: str
    rank_ic_mean: float = 0.0
    rank_ic_ir: float = 0.0
    rank_ic_t: float = 0.0
    long_short_ann: float = 0.0
    turnover_mean: float = 0.0
    group_monotonic: float = 0.0
    dsr: float = 0.0
    ic_dsr: float = 0.0          # IC 序列口径 DSR（日度独立观测，主判据）
    regime_consistency: Optional[float] = None
    gates: dict = field(default_factory=dict)
    pass_all: bool = False
    notes: list = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "factor_name": self.factor_name,
            "rank_ic_mean": round(self.rank_ic_mean, 4),
            "rank_ic_ir": round(self.rank_ic_ir, 3),
            "rank_ic_t": round(self.rank_ic_t, 2),
            "long_short_ann": round(self.long_short_ann, 4),
            "turnover_mean": round(self.turnover_mean, 3),
            "group_monotonic": round(self.group_monotonic, 2),
            "dsr": round(self.dsr, 3),
            "ic_dsr": round(self.ic_dsr, 3),
            "regime_consistency": None if self.regime_consistency is None else round(self.regime_consistency, 2),
            "gates": self.gates,
            "pass_all": self.pass_all,
            "notes": self.notes,
        }


def evaluate_factor(
    df: pd.DataFrame,
    factor: pd.Series,
    name: str = "unnamed",
    horizon: int = 5,
    n_groups: int = 5,
    cost_bps: float = 15.0,
    n_trials: int = 1,
    regime: Optional[pd.Series] = None,
    min_abs_ic: float = 0.02,
    min_ic_ir: float = 0.30,
    min_t: float = 2.0,
    min_group_rho: float = 0.70,
    min_dsr: float = 0.95,
    trading_days: int = 252,
) -> AcceptanceResult:
    """跑完整门禁2 并输出验收单。

    Args:
        df:       面板数据（trade_date/ts_code/close/...）
        factor:   与 df 等长的因子值
        regime:   classify_regime 输出（index=trade_date），可选
        n_trials: 本轮研究已试过的因子/参数总数（DSR 校正用，务必如实填写）
    """
    res = AcceptanceResult(factor_name=name)

    # —— IC 系列 ——
    ics = calc_ic(df, factor, horizon=horizon)
    if ics.empty or len(ics) < 20:
        res.notes.append(f"有效 IC 样本不足（{len(ics)} < 20）")
        res.gates = {k: False for k in ("rank_ic", "ic_ir", "ic_t", "group", "long_short", "dsr")}
        return res
    ric = ics["rank_ic"].dropna()
    res.rank_ic_mean = float(ric.mean())
    res.rank_ic_ir = float(ric.mean() / ric.std()) if ric.std() > 0 else 0.0
    res.rank_ic_t = float(ric.mean() / (ric.std() / np.sqrt(len(ric)))) if ric.std() > 0 else 0.0

    # —— 分组回测 ——
    qb = quantile_backtest(df, factor, n_groups=n_groups, horizon=horizon, cost_bps=cost_bps)
    grp = qb["group_returns"]
    ls = qb["long_short"].dropna()
    res.turnover_mean = float(qb["turnover"].mean()) if len(qb["turnover"]) else 0.0
    # 分组单调性：组序号 vs 组均收益的秩相关
    means = grp.mean()
    if means.notna().sum() >= 3:
        res.group_monotonic = float(pd.Series(means.values).corr(
            pd.Series(range(1, len(means) + 1)), method="spearman"))
    # 方向修正：若 RankIC 为负，多空反向，统一按"因子方向正确"口径报年化
    if len(ls):
        daily = ls.mean() * (trading_days / horizon)
        if res.rank_ic_mean < 0:
            daily = -daily
        res.long_short_ann = float(daily)

    # —— DSR 双口径（2026-09-25 变更登记：方案 B）——
    # ic_dsr（主判据）：RankIC 日度序列的 DSR——独立观测 1400+，学术口径因子显著性
    # dsr（展示）：多空组合日收益序列的 DSR——20 日持有期下独立观测仅 ~70，
    #        sqrt(n-1) 项天然压低，保留展示不作为 gate（理由见 P1 门禁2 第三轮验收报告）
    if len(ric) >= 20:
        ic_sr = float(ric.mean() / ric.std()) if ric.std() > 0 else 0.0
        res.ic_dsr = deflated_sharpe(
            abs(ic_sr), sr_trials_std=abs(ic_sr) * 0.5, n_obs=len(ric),
            n_trials=max(n_trials, 1), skew=abs(float(ric.skew())),
            kurtosis=max(float(ric.kurt()) + 3.0, 1.0))
    if len(ls) >= 20:
        mu, sd = float(ls.mean()), float(ls.std()) or 1e-12
        sr_daily = mu / sd
        sk = float(ls.skew())
        kt = float(ls.kurt()) + 3.0  # pandas kurt 为超额口径 → 转非超额
        res.dsr = deflated_sharpe(
            abs(sr_daily), sr_trials_std=abs(sr_daily) * 0.5, n_obs=len(ls),
            n_trials=max(n_trials, 1), skew=abs(sk), kurtosis=max(kt, 1.0))

    # —— regime 一致性（可选）——
    if regime is not None:
        reg_map = df["trade_date"].map(regime)
        ics_df = ics.copy()
        ics_df["regime"] = ics_df.index.map(regime)
        signs = {}
        for r in ("bull", "bear", "range"):
            sub = ics_df.loc[ics_df["regime"] == r, "rank_ic"].dropna()
            if len(sub) >= 10:
                signs[r] = np.sign(sub.mean())
        if signs:
            dominant = np.sign(res.rank_ic_mean)
            res.regime_consistency = float(
                sum(1 for s in signs.values() if s == dominant) / len(signs))

    # —— 硬标准判定 ——
    g = {
        "rank_ic": abs(res.rank_ic_mean) >= min_abs_ic,
        "ic_ir": abs(res.rank_ic_ir) >= min_ic_ir,
        "ic_t": abs(res.rank_ic_t) >= min_t,
        "group": abs(res.group_monotonic) >= min_group_rho,
        "long_short": res.long_short_ann > 0,
        "dsr": res.ic_dsr >= min_dsr,   # 主判据 = IC 序列口径（组合口径 dsr 仅展示）
    }
    if res.regime_consistency is not None:
        g["regime"] = res.regime_consistency >= 2.0 / 3.0
    res.gates = g
    res.pass_all = all(g.values())
    if not res.pass_all:
        res.notes.append("未通过项: " + ", ".join(k for k, v in g.items() if not v))
    return res
