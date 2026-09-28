"""决定性检验：wk_intraday_rev 各分组的【绝对收益】（非多空差）。

若 G1（最超卖）绝对收益为负 → 因子纯多头方向不可用（+65.6% 多空全靠做空端）。
用周频面板，horizon=4 周，等权持有收益。
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


def main() -> None:
    df = duckdb.query(f"SELECT * FROM '{ROOT / 'data' / 'weekly_panel_full.parquet'}'").fetchdf()
    df = df[(df["close"] > 0.5) & (df["amount"] > 0)].sort_values(["ts_code", "trade_date"]).reset_index(drop=True)
    # 流动性过滤（对齐 P7-R2）
    g = df.groupby("ts_code", observed=True)
    liq = g["amount"].transform(lambda s: s.rolling(4, min_periods=2).mean())
    df = df[(liq >= 50000).fillna(False)].reset_index(drop=True)
    df["wbr"] = -g["week_intraday"].transform(lambda s: s.rolling(4, min_periods=3).sum()) if False else np.nan
    # 重算（过滤后需按新分组）
    g2 = df.groupby("ts_code", observed=True)
    df["wbr"] = -g2["week_intraday"].transform(lambda s: s.rolling(4, min_periods=3).sum())
    # 未来 4 周收益
    df["fwd4"] = g2["close"].transform(lambda s: s.shift(-4) / s - 1)
    df = df.dropna(subset=["wbr", "fwd4"])
    # 每日截面分 5 组
    df["grp"] = df.groupby("trade_date")["wbr"].transform(
        lambda s: pd.qcut(s.rank(method="first"), 5, labels=False) if s.notna().sum() >= 20 else np.nan)
    df = df.dropna(subset=["grp"])
    res = df.groupby("grp")["fwd4"].agg(["mean", "count"])
    logger.info("分组绝对收益（4周持有，G1=最超卖 wbr 最高）：")
    for gi, row in res.iterrows():
        logger.info(f"  G{int(gi)+1}: 绝对收益 {row['mean']:+.2%}（n={int(row['count'])}）")
    g1 = res.loc[0, "mean"]; g5 = res.loc[4, "mean"]
    logger.info(f"多空差 G1-G5 = {g1 - g5:+.2%} | G1 绝对 {g1:+.2%} | G5 绝对 {g5:+.2%}")
    # 全样本基准（等权全市场）
    logger.info(f"全市场等权基准：{df['fwd4'].mean():+.2%}")


if __name__ == "__main__":
    main()
