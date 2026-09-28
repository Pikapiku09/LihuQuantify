"""严谨版：4周持有 + 每周1/4调仓（真实可实现口径）。

与 v1 模拟的差异：
  - 持有 4 周（因子兑现周期），非 1 周
  - 每周只换 1/4 仓 → 成本 = 25% 换手 × 双边费率
  - 分批建仓：组合收益 = 4 个批次的加权平均
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
    g = df.groupby("ts_code", observed=True)
    liq = g["amount"].transform(lambda s: s.rolling(4, min_periods=2).mean())
    df = df[(liq >= 50000).fillna(False)].reset_index(drop=True)
    g = df.groupby("ts_code", observed=True)
    df["wbr"] = -g["week_intraday"].transform(lambda s: s.rolling(4, min_periods=3).sum())
    df["fwd4"] = g["close"].transform(lambda s: s.shift(-4) / s - 1)   # 4周收益
    df = df.dropna(subset=["wbr"])
    weeks = sorted(df["trade_date"].unique())
    cost = 2 * 15.0 / 10000      # 双边

    def sim(top_pct: float):
        """每周建 1/4 仓、持有 4 周 → 组合周收益 = 各批次 4 周收益的 1/4 贡献。"""
        weekly = []
        for w in weeks[:-4]:
            cur = df[df["trade_date"] == w].dropna(subset=["fwd4"])
            if len(cur) < 50:
                continue
            n = max(1, int(len(cur) * top_pct))
            sel = cur.nlargest(n, "wbr")           # 最超卖
            weekly.append(sel["fwd4"].mean() / 4 - 0.25 * cost)   # 持有4周收益均摊到每周
        r = pd.Series(weekly)
        cum = (1 + r).prod() - 1
        ann = (1 + cum) ** (52 / len(r)) - 1
        sharpe = r.mean() / r.std() * np.sqrt(52) if r.std() > 0 else 0
        mdd = ((1 + r).cumprod() / (1 + r).cumprod().cummax() - 1).min()
        return cum, ann, sharpe, mdd, len(r)

    logger.info("=== 严谨版：4周持有 + 每周1/4调仓（扣双边0.15%）===")
    out = {}
    for top in (0.05, 0.10, 0.20, 0.30):
        cum, ann, sharpe, mdd, n = sim(top)
        out[top] = {"cum": round(cum, 4), "ann": round(ann, 4), "sharpe": round(sharpe, 2), "mdd": round(mdd, 4)}
        logger.info(f"top {top:.0%}: 累计 {cum:+.1%} 年化 {ann:+.1%} 夏普 {sharpe:.2f} 回撤 {mdd:.1%}")

    # 分年（top 20%）
    logger.info("=== top 20% 分年 ===")
    df["year"] = pd.to_datetime(df["trade_date"]).dt.year
    for y in sorted(df["year"].unique()):
        weekly = []
        for w in weeks[:-4]:
            if pd.Timestamp(w).year != y:
                continue
            cur = df[df["trade_date"] == w].dropna(subset=["fwd4"])
            if len(cur) < 50:
                continue
            n = max(1, int(len(cur) * 0.20))
            sel = cur.nlargest(n, "wbr")
            weekly.append(sel["fwd4"].mean() / 4 - 0.25 * cost)
        if weekly:
            rr = pd.Series(weekly)
            logger.info(f"  {y}: 累计 {((1+rr).prod()-1):+.1%}（{len(rr)} 周）")


if __name__ == "__main__":
    main()
