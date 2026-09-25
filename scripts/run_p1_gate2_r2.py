"""P1 门禁2 第二轮：方向修正 + 参数网格。

试验（R2 新增 6 个，累计 n_trials = R1(3) + R2(6) = 9）：
  1. rev_mom_20        —— momentum_20 取反（A 股 20 日反转，R1 已证 t=-10.4）
  2. rev_mom_20_winsor —— 每日截面 MAD 去极值后的负动量（妖股极端值可能破坏中段单调）
  3. rev_mom_20_rank   —— 截面秩变换后的负动量（对异常分布稳健）
  4-6. turnover_{10,20,60} —— 换手/流动性溢价窗口网格（R1 turnover_20 DSR 仅差 0.006）

新因子在脚本内定义（通过门禁才固化进 research/factor.py——避免注册表膨胀）。
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

from lihu_quantify.research import compute_factor, evaluate_factor, momentum, turnover_premium
from lihu_quantify.research.regime import classify_regime

N_TRIALS_CUM = 9   # R1:3 + R2:6


def load_panel() -> pd.DataFrame:
    df = duckdb.query(f"SELECT * FROM '{ROOT / 'data' / 'research_panel.parquet'}'").fetchdf()
    df = df[(df["close"] > 0.5) & (df["amount"] > 0)]
    return df.sort_values(["ts_code", "trade_date"]).reset_index(drop=True)


def load_regime() -> pd.Series:
    con = duckdb.connect(str(ROOT / "data" / "lihu_quant.duckdb"), read_only=True)
    idx = con.execute(
        "SELECT trade_date, close FROM index_daily WHERE ts_code = '000001.SH' "
        "AND trade_date BETWEEN '2019-01-01' AND '2024-12-31' ORDER BY trade_date"
    ).fetchdf()
    con.close()
    return classify_regime(idx)


def cs_winsorize(s: pd.Series, by: pd.Series, n_mad: float = 3.0) -> pd.Series:
    """按日截面 MAD 去极值。"""
    def _w(g: pd.Series) -> pd.Series:
        med = g.median()
        mad = (g - med).abs().median() * 1.4826
        lo, hi = med - n_mad * mad, med + n_mad * mad
        return g.clip(lo, hi)
    return s.groupby(by, observed=True).transform(_w)


def cs_rank(s: pd.Series, by: pd.Series) -> pd.Series:
    """按日截面百分位秩。"""
    return s.groupby(by, observed=True).rank(pct=True)


def main() -> None:
    panel = load_panel()
    regime = load_regime()
    td = panel["trade_date"]
    logger.info(f"面板：{len(panel)} 行 × {panel['ts_code'].nunique()} 只")

    mom = momentum(panel)                      # 原 20 日动量
    rev_raw = -mom
    rev_winsor = -cs_winsorize(mom, td)
    rev_rank = -cs_rank(mom, td)

    candidates = {
        "rev_mom_20": rev_raw,
        "rev_mom_20_winsor": rev_winsor,
        "rev_mom_20_rank": rev_rank,
        "turnover_10": turnover_premium(panel, n=10),
        "turnover_20": turnover_premium(panel, n=20),
        "turnover_60": turnover_premium(panel, n=60),
    }

    results = {}
    for name, factor in candidates.items():
        res = evaluate_factor(panel, factor, name=name, horizon=5, cost_bps=15.0,
                              n_trials=N_TRIALS_CUM, regime=regime)
        results[name] = res.as_dict()
        d = results[name]
        gates_fail = [k for k, v in d["gates"].items() if not v]
        logger.info(
            f"[{name}] RankIC={d['rank_ic_mean']:+.4f} ICIR={d['rank_ic_ir']:+.2f} "
            f"t={d['rank_ic_t']:+.1f} 多空={d['long_short_ann']:+.2%} 单调ρ={d['group_monotonic']:+.2f} "
            f"DSR={d['dsr']:.3f} regime={d['regime_consistency']} "
            f"→ {'PASS' if d['pass_all'] else 'FAIL(' + ','.join(gates_fail) + ')'}"
        )

    out = ROOT / "outputs" / "p1_gate2_r2_result.json"
    out.write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
    logger.info(f"R2 验收单 → {out}")


if __name__ == "__main__":
    main()
