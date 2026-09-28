"""组合模拟：直接验证 wk_intraday_rev 纯多头可实现收益（绕过引擎止损限制）。

方法：每周截面上取最超卖 top X% 等权，持有 4 周（=每周调仓 1/4 仓位），扣双边成本。
对照组：全市场等权、指数。
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
    df["fwd1"] = g["close"].transform(lambda s: s.shift(-1) / s - 1)     # 下周收益
    df = df.dropna(subset=["wbr"])
    weeks = sorted(df["trade_date"].unique())

    def backtest(top_pct: float, cost_bps: float = 15.0):
        """每周调 1/4 仓（持有 4 周），成本 = 换手 × 双边费率。"""
        rets = []
        for i, w in enumerate(weeks[:-1]):
            cur = df[df["trade_date"] == w].dropna(subset=["fwd1"])
            if len(cur) < 50:
                continue
            n = max(1, int(len(cur) * top_pct))
            sel = cur.nlargest(n, "wbr")          # wbr 最高 = 最超卖
            gross = sel["fwd1"].mean()
            # 每周换 1/4 仓：换手约 50%（进出各半）→ 成本 ~0.5 × 2 × cost
            net = gross - 0.5 * 2 * cost_bps / 10000
            rets.append(net)
        r = pd.Series(rets)
        cum = (1 + r).prod() - 1
        ann = (1 + cum) ** (52 / len(r)) - 1
        sharpe = r.mean() / r.std() * np.sqrt(52) if r.std() > 0 else 0
        return cum, ann, sharpe, len(r)

    logger.info("=== wk_intraday_rev 纯多头组合模拟（周频调仓，持有4周）===")
    for top in (0.05, 0.10, 0.20, 0.30):
        cum, ann, sharpe, n = backtest(top)
        logger.info(f"top {top:.0%}: 累计 {cum:+.1%} 年化 {ann:+.1%} 夏普 {sharpe:.2f}（{n} 周）")

    # 对照：全市场等权
    all_r = []
    for w in weeks[:-1]:
        cur = df[df["trade_date"] == w].dropna(subset=["fwd1"])
        if len(cur) >= 50:
            all_r.append(cur["fwd1"].mean())
    r = pd.Series(all_r)
    cum = (1 + r).prod() - 1
    logger.info(f"对照-全市场等权: 累计 {cum:+.1%} 年化 {(1+cum)**(52/len(r))-1:+.1%} 夏普 {r.mean()/r.std()*np.sqrt(52):.2f}")

    # 分年（top 10%）
    logger.info("=== top 10% 分年 ===")
    df["year"] = pd.to_datetime(df["trade_date"]).dt.year
    for y in sorted(df["year"].unique()):
        cum, ann, sharpe, n = backtest(0.10)
        if n > 0:
            wy = [w for w in weeks[:-1] if pd.Timestamp(w).year == y]
            rets = []
            for w in wy:
                cur = df[df["trade_date"] == w].dropna(subset=["fwd1"])
                if len(cur) < 50:
                    continue
                nsel = max(1, int(len(cur) * 0.10))
                sel = cur.nlargest(nsel, "wbr")
                rets.append(sel["fwd1"].mean() - 0.5 * 2 * 15.0 / 10000)
            if rets:
                rr = pd.Series(rets)
                cc = (1 + rr).prod() - 1
                logger.info(f"  {y}: 累计 {cc:+.1%}（{len(rr)} 周）")


if __name__ == "__main__":
    main()
