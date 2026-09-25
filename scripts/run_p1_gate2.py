"""P1 门禁2：三个起步因子的第一轮真实数据验收。

数据：DuckDB daily_quotes + adj_factors（前复权），2019-01-01 ~ 2024-12-31
      （2025+ 数据留在库里作门禁3 样本外，本脚本不用）
因子：momentum_20 / reversal_5 / turnover_20（research/factor.py）
输出：outputs/p1_gate2_result.json + 控制台摘要
"""

from __future__ import annotations

import json
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import duckdb
import pandas as pd
from loguru import logger

from lihu_quantify.research import compute_factor, evaluate_factor
from lihu_quantify.research.regime import classify_regime

START, END = "2019-01-01", "2024-12-31"


def load_panel() -> pd.DataFrame:
    """读 build_research_panel.py 产出的前复权面板 parquet。"""
    df = duckdb.query(f"SELECT * FROM '{ROOT / 'data' / 'research_panel.parquet'}'").fetchdf()
    df = df[(df["close"] > 0.5) & (df["amount"] > 0)]      # 剔除极端/停牌行
    return df.sort_values(["ts_code", "trade_date"]).reset_index(drop=True)


def load_regime() -> pd.Series:
    con = duckdb.connect(str(ROOT / "data" / "lihu_quant.duckdb"), read_only=True)
    idx = con.execute(
        "SELECT trade_date, close FROM index_daily WHERE ts_code = '000001.SH' "
        "AND trade_date BETWEEN ? AND ? ORDER BY trade_date",
        [START, END],
    ).fetch_df()
    con.close()
    return classify_regime(idx)


def main() -> None:
    panel = load_panel()
    regime = load_regime()
    logger.info(f"面板：{len(panel)} 行 × {panel['ts_code'].nunique()} 只 "
                f"({panel['trade_date'].min()} ~ {panel['trade_date'].max()})")

    results = {}
    for name in ("momentum_20", "reversal_5", "turnover_20"):
        factor = compute_factor(panel, name)
        res = evaluate_factor(panel, factor, name=name, horizon=5, cost_bps=15.0,
                              n_trials=3, regime=regime)
        results[name] = res.as_dict()
        d = results[name]
        logger.info(
            f"[{name}] RankIC={d['rank_ic_mean']:+.4f} ICIR={d['rank_ic_ir']:+.2f} "
            f"t={d['rank_ic_t']:+.1f} 多空年化={d['long_short_ann']:+.2%} "
            f"单调ρ={d['group_monotonic']:+.2f} DSR={d['dsr']:.3f} "
            f"regime一致={d['regime_consistency']} → {'PASS' if d['pass_all'] else 'FAIL'}"
        )

    out = ROOT / "outputs" / "p1_gate2_result.json"
    out.parent.mkdir(exist_ok=True)
    out.write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
    logger.info(f"验收单已写入 {out}")


if __name__ == "__main__":
    main()
