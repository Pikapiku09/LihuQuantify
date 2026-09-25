"""P2 门禁1+2：业绩预告事件研究（PEAD 检验）。

事件定义：forecast 公告日（ann_date），信号 = p_change_mid（预告净利润同比中值）
执行口径（真实可交易）：
    T+1 开盘买入（公告次日），剔除一字板不可买（T+1 的 low==high 且 high>=前收×1.097）
    持有 {5, 20, 60} 日，以收盘计收益
基准：同期池等权均值（近似市场超额）；对照组 = 事件前 60 日的收益（预漂移）
分组：p_change_mid 五分组 + type 类别（预增/预减/扭亏/预亏/略增/略减）
核心问题：A 股业绩预告后是否存在可交易的漂移（PEAD），方向如何。
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


def load() -> tuple[pd.DataFrame, pd.DataFrame]:
    ev = duckdb.query(f"SELECT * FROM '{ROOT / 'data' / 'p2_events.parquet'}'").fetchdf()
    px = duckdb.query(f"SELECT ts_code, trade_date, open, high, low, close, "
                      f"lag(close) OVER (PARTITION BY ts_code ORDER BY trade_date) AS pre_close "
                      f"FROM '{ROOT / 'data' / 'research_panel.parquet'}'").fetchdf()
    px["trade_date"] = pd.to_datetime(px["trade_date"]).dt.strftime("%Y%m%d")
    px = px.sort_values(["ts_code", "trade_date"]).reset_index(drop=True)
    # pre_close 缺失时前向填充
    px["pre_close"] = px.groupby("ts_code")["pre_close"].ffill()
    return ev, px


def build_event_returns(ev: pd.DataFrame, px: pd.DataFrame, horizons=(5, 20, 60)) -> pd.DataFrame:
    """对每个事件计算 T+1 开盘买入、持有 h 日的收益与可交易标记。"""
    px_idx = {c: g.reset_index(drop=True) for c, g in px.groupby("ts_code")}
    rows = []
    for _, e in ev.iterrows():
        g = px_idx.get(e["ts_code"])
        if g is None:
            continue
        pos = g.index[g["trade_date"] > e["ann_date"]]
        if len(pos) == 0:
            continue
        i1 = pos[0]
        if i1 + max(horizons) >= len(g):
            continue
        r1 = g.loc[i1]
        # 一字板：low==high 且涨幅接近涨停 → 不可买
        tradable = not (r1["low"] == r1["high"] and r1["high"] >= r1["pre_close"] * 1.097)
        entry = r1["open"]
        row = {"ts_code": e["ts_code"], "ann_date": e["ann_date"], "type": e["type"],
               "p_change_mid": e["p_change_mid"], "tradable": tradable}
        for h in horizons:
            exit_close = g.loc[i1 + h, "close"]
            row[f"ret_{h}"] = exit_close / entry - 1.0
        # 事件前 60 日收益（预漂移对照）
        if i1 >= 60:
            row["pre_ret_60"] = g.loc[i1 - 1, "close"] / g.loc[i1 - 60, "close"] - 1.0
        rows.append(row)
    return pd.DataFrame(rows)


def main() -> None:
    ev, px = load()
    logger.info(f"事件 {len(ev)} 条 | 行情 {len(px)} 行")
    df = build_event_returns(ev, px)
    df = df.dropna(subset=["ret_20"])
    df = df[df["tradable"]]      # 只看可交易的
    logger.info(f"可交易事件 {len(df)} 条")

    # 基准：同期池等权（用全体事件收益均值近似——同池同时窗）
    print("\n=== 按 type 类别（20日持有） ===")
    t = df.groupby("type").agg(n=("ret_20", "size"), ret5=("ret_5", "mean"),
                               ret20=("ret_20", "mean"), ret60=("ret_60", "mean"),
                               pre60=("pre_ret_60", "mean")).round(4)
    print(t.to_string())

    print("\n=== 按 p_change_mid 五分组（超额=组均值-全体均值） ===")
    df["g"] = pd.qcut(df["p_change_mid"].rank(method="first"), 5, labels=False) + 1
    g = df.groupby("g").agg(n=("ret_20", "size"), ret_5=("ret_5", "mean"), ret_20=("ret_20", "mean"),
                            ret_60=("ret_60", "mean"), pc=("p_change_mid", "mean")).round(4)
    overall = {h: df[f"ret_{h}"].mean() for h in (5, 20, 60)}
    for h in (5, 20, 60):
        g[f"excess_{h}"] = (g[f"ret_{h}"] - overall[h]).round(4)
    print(g.to_string())
    print(f"\n全体事件均值: ret5={overall[5]:+.4f} ret20={overall[20]:+.4f} ret60={overall[60]:+.4f}")

    # 单调性与显著性（G1-G5 组 ret20 的 Spearman 与 t 检验）
    from scipy import stats
    gm = g["ret_20"].values
    rho, pv = stats.spearmanr(range(1, 6), gm)
    hi, lo = df[df["g"] == 5]["ret_20"], df[df["g"] == 1]["ret_20"]
    tt = stats.ttest_ind(hi.dropna(), lo.dropna(), equal_var=False)
    print(f"\n分组单调ρ={rho:.2f}(p={pv:.4f}) | G5-G1 均值差={hi.mean()-lo.mean():+.4f} (t={tt.statistic:.2f}, p={tt.pvalue:.4f})")

    out = ROOT / "outputs" / "p2_event_study.json"
    import json
    out.write_text(json.dumps({
        "by_type": t.reset_index().to_dict("records"),
        "by_group": g.reset_index().to_dict("records"),
        "overall": overall, "spearman_rho": round(float(rho), 3),
        "g5_g1_diff": round(float(hi.mean() - lo.mean()), 4), "t_stat": round(float(tt.statistic), 2),
        "p_value": round(float(tt.pvalue), 4), "n_events": int(len(df)),
    }, ensure_ascii=False, indent=2), encoding="utf-8")
    logger.info(f"P2 事件研究 → {out}")


if __name__ == "__main__":
    main()
