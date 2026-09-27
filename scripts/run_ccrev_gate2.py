"""CC-Reversal 门禁2：个股 MA20 趋势过滤对 intraday_rev_20 因子的增强检验。

假设：超卖反弹在"个股 MA20 向上（自身趋势健康）"时更强——回调买入 vs 接飞刀。
方法：把面板按 close>MA20 且 MA20 斜率>0 分两组，分别算 RankIC/多空，对比基线（全样本）。
n_trials：P1 计数 19→20（一次条件化试验）。
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

from lihu_quantify.research import compute_factor
from lihu_quantify.research.acceptance import evaluate_factor
from lihu_quantify.research.regime import classify_regime


def load() -> pd.DataFrame:
    df = duckdb.query(f"SELECT * FROM '{ROOT / 'data' / 'research_panel.parquet'}'").fetchdf()
    df = df[(df["close"] > 0.5) & (df["amount"] > 0)]
    df = df.sort_values(["ts_code", "trade_date"]).reset_index(drop=True)
    # 个股 MA20 与斜率
    df["ma20"] = df.groupby("ts_code")["close"].transform(lambda s: s.rolling(20, min_periods=20).mean())
    df["ma20_slope"] = df.groupby("ts_code")["ma20"].transform(lambda s: s.diff(5) / 5)
    return df


def main() -> None:
    df = load()
    factor = compute_factor(df, "intraday_rev_20")
    con = duckdb.connect(str(ROOT / "data" / "lihu_quant.duckdb"), read_only=True)
    idx = con.execute("SELECT trade_date, close FROM index_daily WHERE ts_code='000001.SH' "
                      "AND trade_date BETWEEN '2019-01-01' AND '2026-08-31' ORDER BY trade_date").fetchdf()
    con.close()
    regime = classify_regime(idx)

    groups = {
        "baseline_all": pd.Series(True, index=df.index),
        "trend_up": (df["close"] > df["ma20"]) & (df["ma20_slope"] > 0),
        "trend_down": (df["close"] < df["ma20"]) & (df["ma20_slope"] < 0),
    }
    out = {}
    for name, mask in groups.items():
        m = mask.fillna(False).to_numpy()
        sub = df[m].reset_index(drop=True)
        f = factor[m].reset_index(drop=True)
        res = evaluate_factor(sub, f, name=name, horizon=20, cost_bps=15.0,
                              n_trials=20, regime=regime)
        out[name] = res.as_dict()
        d = out[name]
        logger.info(f"[{name}] n={len(sub)} RankIC={d['rank_ic_mean']:+.4f} ICIR={d['rank_ic_ir']:+.2f} "
                    f"t={d['rank_ic_t']:+.1f} 多空={d['long_short_ann']:+.2%} 单调ρ={d['group_monotonic']:+.2f} "
                    f"icDSR={d['ic_dsr']:.3f} regime={d['regime_consistency']} "
                    f"→ {'PASS' if d['pass_all'] else 'FAIL'}")

    p = ROOT / "outputs" / "ccrev_gate2_result.json"
    p.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    logger.info(f"→ {p}")


if __name__ == "__main__":
    main()
