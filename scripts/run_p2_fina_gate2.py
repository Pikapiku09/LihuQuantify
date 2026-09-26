"""P2-财务因子门禁2：披露日对齐的基本面因子（ATO变动/ROE/净利同比/毛利率变动）。

防前视铁律：交易日只能看到 ann_date <= trade_date 的最新报告期（merge_asof 式展开）。
因子（horizon=20 与 P1 口径一致）：
  ato_chg   —— assets_turn 同比变动（源达构造：本期 - 去年同期）
  roe_level —— ROE 水平
  np_yoy_rev—— 净利同比取反（检验正式财报口径是否与预告同样 price-in）
  gm_chg    —— 毛利率同比变动
n_trials：P2 计数 2→6。
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

from lihu_quantify.research import evaluate_factor
from lihu_quantify.research.regime import classify_regime

N_TRIALS = 6


def load() -> pd.DataFrame:
    px = duckdb.query(f"SELECT ts_code, trade_date, open, high, low, close, vol, amount "
                      f"FROM '{ROOT / 'data' / 'research_panel.parquet'}'").fetchdf()
    fina = duckdb.query(f"SELECT * FROM '{ROOT / 'data' / 'p2_fina.parquet'}'").fetchdf()
    fina["ann_date"] = pd.to_datetime(fina["ann_date"], format="%Y%m%d")
    fina["end_date"] = pd.to_datetime(fina["end_date"], format="%Y%m%d")
    # 同比变动：本期 - 去年同期（同季）
    fina["year"] = fina["end_date"].dt.year
    fina["q"] = fina["end_date"].dt.quarter
    key = ["ts_code", "q"]
    prev = fina[["ts_code", "q", "year", "assets_turn", "gross_margin"]].copy()
    prev["year"] = prev["year"] + 1
    prev = prev.rename(columns={"assets_turn": "ato_prev", "gross_margin": "gm_prev"})
    fina = fina.merge(prev, on=key + ["year"], how="left")
    fina["ato_chg"] = fina["assets_turn"] - fina["ato_prev"]
    fina["gm_chg"] = fina["gross_margin"] - fina["gm_prev"]
    # 按披露日排序（merge_asof 要求）
    px["trade_date"] = pd.to_datetime(px["trade_date"])
    px = px.sort_values(["ts_code", "trade_date"]).reset_index(drop=True)
    fina = fina.sort_values(["ts_code", "ann_date"]).reset_index(drop=True)
    # 披露日对齐展开（每股 merge_asof）
    cols = ["ato_chg", "roe", "netprofit_yoy", "gm_chg"]
    parts = []
    for code, g in px.groupby("ts_code"):
        f = fina[fina["ts_code"] == code]
        if f.empty:
            continue
        merged = pd.merge_asof(g[["ts_code", "trade_date"]], f[["ann_date"] + cols],
                               left_on="trade_date", right_on="ann_date", direction="backward")
        merged = merged.drop(columns=["ann_date"])
        merged.index = g.index          # 对齐回原行
        parts.append(merged)
    ext = pd.concat(parts)
    df = px.merge(ext, on=["ts_code", "trade_date"], how="inner")
    df = df[(df["close"] > 0.5) & (df["amount"] > 0)]
    return df.sort_values(["ts_code", "trade_date"]).reset_index(drop=True)


def main() -> None:
    df = load()
    logger.info(f"面板：{len(df)} 行 × {df['ts_code'].nunique()} 只")
    con = duckdb.connect(str(ROOT / "data" / "lihu_quant.duckdb"), read_only=True)
    idx = con.execute("SELECT trade_date, close FROM index_daily WHERE ts_code='000001.SH' "
                      "AND trade_date BETWEEN '2019-01-01' AND '2026-08-31' ORDER BY trade_date").fetchdf()
    con.close()
    regime = classify_regime(idx)

    candidates = {
        "ato_chg": df["ato_chg"],
        "roe_level": df["roe"],
        "np_yoy_rev": -df["netprofit_yoy"],
        "gm_chg": df["gm_chg"],
    }
    results = {}
    for name, factor in candidates.items():
        res = evaluate_factor(df, factor, name=name, horizon=20, cost_bps=15.0,
                              n_trials=N_TRIALS, regime=regime)
        results[name] = res.as_dict()
        d = results[name]
        gates_fail = [k for k, v in d["gates"].items() if not v]
        logger.info(f"[{name}] RankIC={d['rank_ic_mean']:+.4f} ICIR={d['rank_ic_ir']:+.2f} "
                    f"t={d['rank_ic_t']:+.1f} 多空={d['long_short_ann']:+.2%} 单调ρ={d['group_monotonic']:+.2f} "
                    f"icDSR={d['ic_dsr']:.3f} regime={d['regime_consistency']} "
                    f"→ {'PASS' if d['pass_all'] else 'FAIL(' + ','.join(gates_fail) + ')'}")

    out = ROOT / "outputs" / "p2_fina_gate2_result.json"
    out.write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
    logger.info(f"P2-财务验收单 → {out}")


if __name__ == "__main__":
    main()
