"""601101 单股对照：买持 vs 完整条件趋势 vs 砍条件趋势（骑住 vs 被洗出）。

策略：
  A. Buy&Hold 2026-01-02 起持有到 2026-08-31（基准：能不能吃到 +110%）
  B. 完整条件趋势（CherryClaw 风格）：MA5金叉 + 量比>1.5 + 实体>0.4 + 破8%/MA10止损 + 卖出后8日禁买
  C. 砍条件趋势（防爆仓级）：close>MA20 且 MA20 向上 → 持有；close<MA20 → 离场（无 ATR 止损、无禁买期）
  D. 神奇支撑线（用户纪律）：11 周均线——周线口径：持有至收盘跌破 11 周均线
输出：各自 2026 年区间收益、买入次数、被洗出次数。
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str("E:/LihuQuantify/src"))
import duckdb
import pandas as pd


def load():
    con = duckdb.connect("E:/LihuQuantify/data/lihu_quant.duckdb", read_only=True)
    df = con.execute("""
        SELECT trade_date, open, high, low, close, vol FROM daily_quotes
        WHERE ts_code='601101.SH' AND trade_date BETWEEN '2025-10-01' AND '2026-08-31'
        ORDER BY trade_date
    """).fetchdf()
    con.close()
    df["trade_date"] = pd.to_datetime(df["trade_date"])
    for c in ("open", "high", "low", "close", "vol"):
        df[c] = pd.to_numeric(df[c])
    return df


def run():
    df = load().reset_index(drop=True)
    n = len(df)
    print(f"样本: {n} 根日线（2025-10 ~ 2026-08）")
    seg = df[df["trade_date"] >= "2026-01-01"].reset_index(drop=True)
    print(f"2026 区间: {seg['trade_date'].iloc[0].date()} ~ {seg['trade_date'].iloc[-1].date()} | "
          f"涨幅 {(seg['close'].iloc[-1]/seg['close'].iloc[0]-1)*100:+.1f}%")

    # 指标
    df["ma5"] = df["close"].rolling(5).mean()
    df["ma10"] = df["close"].rolling(10).mean()
    df["ma20"] = df["close"].rolling(20).mean()
    df["ma20_up"] = df["ma20"] > df["ma20"].shift(3)
    df["vr"] = df["vol"] / df["vol"].rolling(5).mean()
    df["entity"] = (df["close"] - df["open"]).abs() / df["close"]

    # ---- B: 完整条件趋势 ----
    pos_b, entry_b = 0, 0.0
    buys_b, stops_b = 0, 0
    last_buy_i = -999
    rets_b = []
    for i in range(1, n):
        r = df.iloc[i]
        p = df.iloc[i - 1]
        if pos_b:
            if r["close"] < entry_b * 0.92 or r["close"] < r["ma10"]:
                pos_b = 0; stops_b += 1
            else:
                rets_b.append(r["close"] / p["close"] - 1)
        else:
            gold = p["ma5"] and p["ma5"] < p["close"] and r["ma5"] > r["close"] * 0.97
            cond = (gold and r["vr"] >= 1.5 and r["entity"] >= 0.4
                    and (i - last_buy_i) > 8)
            if cond:
                pos_b = 1; entry_b = r["close"]; buys_b += 1; last_buy_i = i
    ret_b = (1 + pd.Series(rets_b)).prod() - 1 if rets_b else 0.0

    # ---- C: 砍条件趋势（MA20 趋势跟随）----
    pos_c, rets_c, buys_c = 0, [], 0
    for i in range(1, n):
        r = df.iloc[i]
        p = df.iloc[i - 1]
        if pos_c:
            if r["close"] < r["ma20"]:
                pos_c = 0
            else:
                rets_c.append(r["close"] / p["close"] - 1)
        else:
            if r["ma20"] == r["ma20"] and r["close"] > r["ma20"] and r["ma20_up"]:
                pos_c = 1; buys_c += 1
    ret_c = (1 + pd.Series(rets_c)).prod() - 1 if rets_c else 0.0

    # ---- D: 神奇支撑线（11 周均线，周线口径）----
    wk = df.set_index("trade_date")["close"].resample("W-FRI").last().dropna()
    wk_ma11 = wk.rolling(11).mean()
    pos_d, rets_d, buys_d = 0, [], 0
    wk_prev_close = wk.shift(1)
    for i in range(1, len(wk)):
        if pos_d:
            if wk.iloc[i] < wk_ma11.iloc[i]:
                pos_d = 0
            else:
                rets_d.append(wk.iloc[i] / wk.iloc[i - 1] - 1)
        else:
            if wk.iloc[i] > wk_ma11.iloc[i] and wk.iloc[i - 1] > (wk_ma11.iloc[i - 1] if wk_ma11.iloc[i - 1] == wk_ma11.iloc[i - 1] else 0):
                pos_d = 1; buys_d += 1
    ret_d = (1 + pd.Series(rets_d)).prod() - 1 if rets_d else 0.0

    # ---- 输出 ----
    hold_a = seg["close"].iloc[-1] / seg["close"].iloc[0] - 1
    print()
    print(f"A. 买持 2026:            {hold_a*100:+.1f}%")
    print(f"B. 完整条件趋势:         {ret_b*100:+.1f}%   买入 {buys_b} 次 / 止损离场 {stops_b} 次")
    print(f"C. 砍条件 MA20 趋势:     {ret_c*100:+.1f}%   买入 {buys_c} 次")
    print(f"D. 神奇支撑线(11周):     {ret_d*100:+.1f}%   买入 {buys_d} 次")


if __name__ == "__main__":
    run()
