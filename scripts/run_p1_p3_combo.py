"""P1×P3 组合试验：intraday_rev_20 × 排除主力净流入前20%。

假设：主力仍在净流入的票 = 接盘未完成，其超卖反弹质量更差 → 剔除后 IC 提升。
对照（同窗同口径）：
    A = intraday_rev_20 原始（2024-2026.8 子样本基准，因资金流数据 2024 起）
    B = intraday_rev_20 + 剔除「20日主力净流入率前 20%」
n_trials：P1 计数 18→19（B 是一次新试验；A 为同窗基准不新增选择）。
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

from lihu_quantify.research import compute_factor, evaluate_factor
from lihu_quantify.research.regime import classify_regime


def load() -> pd.DataFrame:
    px = duckdb.query(f"SELECT ts_code, trade_date, open, high, low, close, vol, amount "
                      f"FROM '{ROOT / 'data' / 'research_panel.parquet'}'").fetchdf()
    mf = duckdb.query(f"SELECT ts_code, trade_date, net_amount FROM '{ROOT / 'data' / 'p3_moneyflow.parquet'}'").fetchdf()
    mf["trade_date"] = pd.to_datetime(mf["trade_date"])
    df = px.merge(mf, on=["ts_code", "trade_date"], how="inner")
    df = df[(df["close"] > 0.5) & (df["amount"] > 0)]
    return df.sort_values(["ts_code", "trade_date"]).reset_index(drop=True)


def mf20_ratio(df: pd.DataFrame) -> pd.Series:
    """20 日主力净流入占成交额比率（P3 的 mf_net_20 口径）。"""
    num = df["net_amount"].groupby(df["ts_code"], observed=True).transform(
        lambda x: x.rolling(20, min_periods=10).sum())
    den = df["amount"].groupby(df["ts_code"], observed=True).transform(
        lambda x: x.rolling(20, min_periods=10).sum()) * 10
    return num / den


def main() -> None:
    df = load()
    logger.info(f"面板：{len(df)} 行 × {df['ts_code'].nunique()} 只 ({df['trade_date'].min()} ~ {df['trade_date'].max()})")

    con = duckdb.connect(str(ROOT / "data" / "lihu_quant.duckdb"), read_only=True)
    idx = con.execute("SELECT trade_date, close FROM index_daily WHERE ts_code='000001.SH' "
                      "AND trade_date BETWEEN '2024-01-01' AND '2026-08-31' ORDER BY trade_date").fetchdf()
    con.close()
    regime = classify_regime(idx)

    ir = compute_factor(df, "intraday_rev_20")
    mf20 = mf20_ratio(df)
    # 截面前 20% 标记（主力净流入极端）
    rank_pct = mf20.groupby(df["trade_date"], observed=True).rank(pct=True)
    excluded = (rank_pct >= 0.80).fillna(False)
    factor_b = ir.mask(excluded)   # 被剔除处 NaN → IC 计算自动跳过

    import json
    results = {}
    for name, factor, nt in (("A_ir20_base_2024_26", ir, 19),
                             ("B_ir20_excl_mf20top20", factor_b, 19)):
        res = evaluate_factor(df, factor, name=name, horizon=20, cost_bps=15.0,
                              n_trials=nt, regime=regime)
        results[name] = res.as_dict()
        d = results[name]
        logger.info(f"[{name}] n_days={d['rank_ic_mean'] and ''}{len(factor.dropna())} "
                    f"RankIC={d['rank_ic_mean']:+.4f} ICIR={d['rank_ic_ir']:+.2f} t={d['rank_ic_t']:+.1f} "
                    f"多空={d['long_short_ann']:+.2%} 单调ρ={d['group_monotonic']:+.2f} icDSR={d['ic_dsr']:.3f}")

    out = ROOT / "outputs" / "p1_p3_combo_result.json"
    out.write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
    logger.info(f"P1×P3 结果 → {out}")


if __name__ == "__main__":
    main()
