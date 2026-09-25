"""residual_vol 拥挤度复核：G5 的 ls_vol_trend=6.03 是否市场 beta 环境导致。

方法：因子多空收益滚动波动 / 同窗口指数收益滚动波动（归一化趋势）。
若归一后趋势仍显著>1.5 → 真拥挤；若回落 → 市场环境效应，G5 改判。
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

from lihu_quantify.research import compute_factor, quantile_backtest


def main() -> None:
    df = duckdb.query(f"SELECT * FROM '{ROOT / 'data' / 'research_panel.parquet'}'").fetchdf()
    df = df[(df["close"] > 0.5) & (df["amount"] > 0)]
    df = df.sort_values(["ts_code", "trade_date"]).reset_index(drop=True)

    factor = compute_factor(df, "residual_vol")
    ls = quantile_backtest(df, factor, n_groups=5, horizon=20, cost_bps=15.0)["long_short"].dropna()

    con = duckdb.connect(str(ROOT / "data" / "lihu_quant.duckdb"), read_only=True)
    idx = con.execute("SELECT trade_date, close FROM index_daily WHERE ts_code='000001.SH' "
                      "AND trade_date BETWEEN '2019-01-01' AND '2026-08-31' ORDER BY trade_date").fetchdf()
    con.close()
    idx["trade_date"] = pd.to_datetime(idx["trade_date"])
    idx = idx.set_index("trade_date")
    idx_ret = idx["close"].pct_change()

    # 对齐：ls 的 index 为每期建仓日（20日间隔），滚动12期波动
    ls_s = pd.Series(ls.values, index=pd.to_datetime(ls.index))
    ls_std = ls_s.rolling(12).std()
    # 市场波动：同日期窗口的日收益波动×sqrt(20)（对齐20日持有期口径）
    mkt_std = idx_ret.rolling(60).std() * np.sqrt(20)
    mkt_aligned = mkt_std.reindex(ls_s.index, method="ffill")
    ratio = (ls_std / mkt_aligned).dropna()

    n = len(ratio)
    head = ratio.head(n // 4).mean()
    tail = ratio.tail(n // 4).mean()
    raw_tail = ls_std.tail(len(ls_std) // 4).mean()
    raw_head = ls_std.head(len(ls_std) // 4).mean()

    logger.info(f"原始多空波动趋势(尾/头): {raw_tail / raw_head:.2f}（门禁3 报的 6.03 口径）")
    logger.info(f"市场波动同期趋势(尾/头): {mkt_aligned.tail(len(mkt_aligned)//4).mean() / mkt_aligned.head(len(mkt_aligned)//4).mean():.2f}")
    logger.info(f"归一后(因子/市场)趋势(尾/头): {tail / head:.2f}")
    logger.info(f"归一比率当前值: {ratio.iloc[-1]:.3f} | 历史分位: {(ratio < ratio.iloc[-1]).mean():.0%}")
    verdict = "真拥挤（归一后仍抬升）" if tail / head > 1.5 else "市场环境效应（归一后回落）"
    logger.info(f"复核结论: {verdict}")


if __name__ == "__main__":
    main()
