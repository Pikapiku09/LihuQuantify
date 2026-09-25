"""P3 门禁2：主力资金流因子初验（2024-2026.8，horizon=5 短窗）。

因子候选（资金流是快变量，持有期 5 日）：
  mf_net_5   —— 5 日主力净流入合计（amount 占比标准化）
  mf_net_20  —— 20 日主力净流入（慢版本）
  elg_net_5  —— 5 日超大单净买入（聪明钱口径）
  mf_rev_5   —— 5 日主力净流入取反（反向假设：流出极端=机构出货后反弹？）
样本说明：资金流数据仅 2024 起（约 640 交易日）——初筛口径，通过后再考虑扩展样本源。
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import duckdb
import numpy as np
import pandas as pd
from loguru import logger

from lihu_quantify.research import evaluate_factor
from lihu_quantify.research.regime import classify_regime

N_TRIALS = 4   # 本轮 4 个试验（P3 新计数，与 P1/P2 分项目登记）


def load() -> pd.DataFrame:
    px = duckdb.query(f"SELECT ts_code, trade_date, open, high, low, close, vol, amount "
                      f"FROM '{ROOT / 'data' / 'research_panel.parquet'}'").fetchdf()
    mf = duckdb.query(f"SELECT * FROM '{ROOT / 'data' / 'p3_moneyflow.parquet'}'").fetchdf()
    mf["trade_date"] = pd.to_datetime(mf["trade_date"])
    df = px.merge(mf, on=["ts_code", "trade_date"], how="inner")
    df = df[(df["close"] > 0.5) & (df["amount"] > 0)]
    return df.sort_values(["ts_code", "trade_date"]).reset_index(drop=True)


def main() -> None:
    df = load()
    logger.info(f"合并面板：{len(df)} 行 × {df['ts_code'].nunique()} 只 "
                f"({df['trade_date'].min()} ~ {df['trade_date'].max()})")

    con = duckdb.connect(str(ROOT / "data" / "lihu_quant.duckdb"), read_only=True)
    idx = con.execute("SELECT trade_date, close FROM index_daily WHERE ts_code='000001.SH' "
                      "AND trade_date BETWEEN '2024-01-01' AND '2026-08-31' ORDER BY trade_date").fetchdf()
    con.close()
    regime = classify_regime(idx)

    td = df["trade_date"]
    # 标准化：净流入 / 成交额（比率化，去规模效应）
    for col in ("net_amount", "buy_elg_amount"):
        df[f"{col}_r"] = df[col] / (df["amount"] * 10)   # amount 千元→元近似（比率口径足够）

    def roll_sum(col, n):
        return (df[col] * 10).groupby(df["ts_code"], observed=True).transform(
            lambda x: x.rolling(n, min_periods=n // 2).sum()) / (
            df["amount"].groupby(df["ts_code"], observed=True).transform(
                lambda x: x.rolling(n, min_periods=n // 2).sum()) * 10)

    candidates = {
        "mf_net_5": roll_sum("net_amount", 5),
        "mf_net_20": roll_sum("net_amount", 20),
        "elg_net_5": roll_sum("buy_elg_amount", 5),
        "mf_rev_5": -roll_sum("net_amount", 5),
    }

    import json
    results = {}
    for name, factor in candidates.items():
        res = evaluate_factor(df, factor, name=name, horizon=5, cost_bps=15.0,
                              n_trials=N_TRIALS, regime=regime)
        results[name] = res.as_dict()
        d = results[name]
        gates_fail = [k for k, v in d["gates"].items() if not v]
        logger.info(f"[{name}] RankIC={d['rank_ic_mean']:+.4f} ICIR={d['rank_ic_ir']:+.2f} "
                    f"t={d['rank_ic_t']:+.1f} 多空={d['long_short_ann']:+.2%} 单调ρ={d['group_monotonic']:+.2f} "
                    f"icDSR={d['ic_dsr']:.3f} regime={d['regime_consistency']} "
                    f"→ {'PASS' if d['pass_all'] else 'FAIL(' + ','.join(gates_fail) + ')'}")

    out = ROOT / "outputs" / "p3_gate2_result.json"
    out.write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
    logger.info(f"P3 验收单 → {out}")


if __name__ == "__main__":
    main()
