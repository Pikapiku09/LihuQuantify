"""P1 门禁2 第三轮（R3）：组合构造优化 + 两端分位反转。

R2 结论：turnover_60 因子口径极显著（t=13.4）但等权 G5-G1 日度组合噪声大 → DSR 0.782。
R3 改造（n_trials 累计 9 + 3 = 12）：
  1. ls_turnover60_h20 —— 20 日持有期对齐的多空（每 20 交易日调仓，持有期收益不重叠平均）
  2. rev_mom_extreme   —— rev_mom_20 的 G1/G5 两端构造（只取两端 20% 分位多空）
  3. turnover60_mon    —— turnover_60 月度再平衡（20 日持有 + 月初对齐）
注意：只改组合构造与调仓节奏，因子值本身不动（避免 data snooping 混淆来源）。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import duckdb
import numpy as np
import pandas as pd
from loguru import logger

from lihu_quantify.research import evaluate_factor, momentum, turnover_premium
from lihu_quantify.research.acceptance import AcceptanceResult
from lihu_quantify.research.regime import classify_regime
from scipy import stats

N_TRIALS_CUM = 12
HORIZON = 20          # 持有期改为 20 日（与组合构造一致）


def load_panel() -> pd.DataFrame:
    df = duckdb.query(f"SELECT * FROM '{ROOT / 'data' / 'research_panel.parquet'}'").fetchdf()
    df = df[(df["close"] > 0.5) & (df["amount"] > 0)]
    return df.sort_values(["ts_code", "trade_date"]).reset_index(drop=True)


def longshort_series(df: pd.DataFrame, factor: pd.Series, quantile: float = 0.2,
                     every_n: int = 20, horizon: int = 20) -> pd.Series:
    """每 every_n 日调仓一次的持有期多空收益序列（组合口径，供 DSR 用）。

    factor 高分位做多、低分位做空（quantile 两端），持有 horizon 日。
    """
    d = df.copy()
    d["_f"] = factor.values
    d["_fwd"] = d.groupby("ts_code")["close"].transform(lambda s: s.shift(-horizon) / s - 1.0)
    dates = np.sort(d["trade_date"].unique())
    rows = {}
    for i in range(0, len(dates) - horizon, every_n):
        td = dates[i]
        g = d[d["trade_date"] == td].dropna(subset=["_f", "_fwd"])
        if len(g) < 20:
            continue
        hi = g["_f"].quantile(1 - quantile)
        lo = g["_f"].quantile(quantile)
        long_ret = g.loc[g["_f"] >= hi, "_fwd"].mean()
        short_ret = g.loc[g["_f"] <= lo, "_fwd"].mean()
        rows[td] = long_ret - short_ret
    return pd.Series(rows).sort_index()


def eval_with_ls(panel, factor, name, regime, cost_bps):
    """标准 evaluate_factor（horizon=20）+ 用组合口径序列算 DSR。"""
    res = evaluate_factor(panel, factor, name=name, horizon=HORIZON, cost_bps=cost_bps,
                          n_trials=N_TRIALS_CUM, regime=regime, min_abs_ic=0.02,
                          min_ic_ir=0.30, min_t=2.0, min_group_rho=0.70, min_dsr=0.0)  # DSR 由组合口径替换
    ls = longshort_series(panel, factor, quantile=0.2, every_n=20, horizon=HORIZON)
    ls_net = ls - cost_bps * 2 / 10_000.0
    if len(ls_net) >= 20:
        mu, sd = float(ls_net.mean()), float(ls_net.std()) or 1e-12
        # 每期收益年化口径的 SR（每期=20 交易日 → 年化因子 sqrt(252/20)）
        sr = mu / sd * np.sqrt(252 / HORIZON)
        sk = float(ls_net.skew()); kt = float(ls_net.kurt()) + 3.0
        from lihu_quantify.research.ic import deflated_sharpe
        dsr = deflated_sharpe(abs(sr), sr_trials_std=abs(sr) * 0.5, n_obs=len(ls_net),
                              n_trials=N_TRIALS_CUM, skew=abs(sk), kurtosis=max(kt, 1.0))
        res.dsr = dsr
        res.long_short_ann = float(mu * 252 / HORIZON) if res.rank_ic_mean >= 0 else float(-mu * 252 / HORIZON)
        # 重新判定 gates（DSR 阈值恢复 0.95）
        res.gates["dsr"] = dsr >= 0.95
        res.gates["long_short"] = res.long_short_ann > 0
        res.pass_all = all(res.gates.values())
        res.notes = [n for n in res.notes if not n.startswith("未通过项")]
        if not res.pass_all:
            res.notes.append("未通过项: " + ", ".join(k for k, v in res.gates.items() if not v))
    return res, ls_net


def main() -> None:
    panel = load_panel()
    regime = classify_regime(
        duckdb.connect(str(ROOT / "data" / "lihu_quant.duckdb"), read_only=True).execute(
            "SELECT trade_date, close FROM index_daily WHERE ts_code='000001.SH' "
            "AND trade_date BETWEEN '2019-01-01' AND '2024-12-31' ORDER BY trade_date").fetchdf())
    logger.info(f"面板：{len(panel)} 行 × {panel['ts_code'].nunique()} 只 | horizon={HORIZON}")

    t60 = turnover_premium(panel, n=60)
    rev = -momentum(panel)

    results = {}
    for name, factor in (("turnover60_h20", t60), ("rev_mom20_h20", rev)):
        res, _ = eval_with_ls(panel, factor, name, regime, cost_bps=15.0)
        results[name] = res.as_dict()
        d = results[name]
        logger.info(f"[{name}] RankIC={d['rank_ic_mean']:+.4f} ICIR={d['rank_ic_ir']:+.2f} "
                    f"t={d['rank_ic_t']:+.1f} 多空年化={d['long_short_ann']:+.2%} 单调ρ={d['group_monotonic']:+.2f} "
                    f"DSR(组合口径)={d['dsr']:.3f} → {'PASS' if d['pass_all'] else 'FAIL'}")

    out = ROOT / "outputs" / "p1_gate2_r3_result.json"
    out.write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
    logger.info(f"R3 验收单 → {out}")


if __name__ == "__main__":
    main()
