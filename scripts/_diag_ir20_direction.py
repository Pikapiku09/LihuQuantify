"""校验 IntradayReversal 的入场方向是否与因子定义一致。

因子定义（intraday_reversal.py:4）：
    ir20 = -Σ ln(close/open)，日内跌得越多 → ir20 越大 → 越超卖 → 看多
    ⇒ 应买入 ir20 的【高分位】

策略实现（intraday_reversal.py:91）：
    mask = ir20_pct <= 0.20   ⇒ 买入 ir20 的【低分位】

本脚本用真实数据算 ir20_pct 与「未来 20 日收益」的 Spearman 相关：
  - 负相关 → 低分位对应高收益 → 代码方向正确
  - 正相关 → 高分位对应高收益 → 代码方向反了
"""
from __future__ import annotations

import json
import random
import sys
from datetime import timedelta
from pathlib import Path

sys.path.insert(0, r"E:\LihuQuantify\src")

import numpy as np
import pandas as pd
from scipy.stats import spearmanr

from lihu_quantify.data.tushare_client import TushareClient
from lihu_quantify.indicators.standard import add_all_standard

START, END = "20250101", "20261110"
SAMPLE_N = 40
FWD = 20


def load_token() -> str:
    obj = json.loads(Path(r"E:\LihuQuantify\tushareMcp.json").read_text(encoding="utf-8"))
    for _n, cfg in obj.get("mcpServers", {}).items():
        url = cfg.get("url", "")
        if "token=" in url:
            return url.split("token=")[-1].split("&")[0]
    raise SystemExit("token 未找到")


def main() -> None:
    token = load_token()
    pool = json.loads(Path(r"E:\LihuQuantify\data\strat_pool_n200_L5_seed42.json")
                      .read_text(encoding="utf-8"))
    random.seed(7)
    codes = random.sample(pool, SAMPLE_N)

    client = TushareClient(token=token, cache_dir=r"E:\LihuQuantify\data\_diag_cache",
                           cache_mtime_ttl=0.0)

    frames = []
    for code in codes:
        try:
            df = client.query("daily", {"ts_code": code, "start_date": START, "end_date": END})
            if df is None or df.empty or len(df) < 300:
                continue
            df = df.sort_values("trade_date").reset_index(drop=True)
            df["trade_date"] = pd.to_datetime(df["trade_date"], format="%Y%m%d").dt.date
            d = add_all_standard(df)

            # 与策略完全一致的因子构造
            intraday = np.log(d["close"] / d["open"])
            ir20 = -intraday.rolling(20, min_periods=10).sum()
            ir20_pct = ir20.rolling(250, min_periods=120).rank(pct=True)
            fwd = d["close"].shift(-FWD) / d["close"] - 1

            sub = pd.DataFrame({
                "code": code, "date": d["trade_date"],
                "ir20": ir20, "ir20_pct": ir20_pct, "fwd20": fwd,
            }).dropna()
            frames.append(sub)
        except Exception as e:
            print(f"{code} 跳过: {type(e).__name__}")

    if not frames:
        raise SystemExit("无有效样本")

    allx = pd.concat(frames, ignore_index=True)
    rho_pct, p_pct = spearmanr(allx["ir20_pct"], allx["fwd20"])
    rho_raw, p_raw = spearmanr(allx["ir20"], allx["fwd20"])

    print("=" * 78)
    print(f"样本: {len(frames)} 只股票 / {len(allx)} 个 (股票,日期) 观测")
    print("=" * 78)
    print(f"Spearman(ir20_pct, 未来{FWD}日收益) = {rho_pct:+.4f}   (p={p_pct:.2e})")
    print(f"Spearman(ir20   , 未来{FWD}日收益) = {rho_raw:+.4f}   (p={p_raw:.2e})")

    # 分位组均值
    q = pd.qcut(allx["ir20_pct"], 5, labels=["Q1低","Q2","Q3","Q4","Q5高"], duplicates="drop")
    grp = allx.groupby(q, observed=True)["fwd20"].agg(["mean", "count"])
    print(f"\n按 ir20_pct 五分组，未来 {FWD} 日平均收益:")
    for name, row in grp.iterrows():
        print(f"  {name:<6} n={int(row['count']):>6}  平均 {row['mean']*100:+.2f}%")

    print("\n" + "-" * 78)
    print("策略实际买入的是 ir20_pct <= 0.20（Q1 最低分位）")
    q1 = grp.iloc[0]["mean"] if len(grp) else float("nan")
    q5 = grp.iloc[-1]["mean"] if len(grp) else float("nan")
    if rho_pct > 0:
        print(f"结论：正相关（高分位收益更高）→ 代码买 Q1({q1*100:+.2f}%) 方向【反了】，"
              f"应买 Q5({q5*100:+.2f}%)")
    else:
        print(f"结论：负相关（低分位收益更高）→ 代码买 Q1({q1*100:+.2f}%) 方向【正确】")


if __name__ == "__main__":
    main()
