"""P6-S2：龙虎榜机构席位因子 IC 验证（预期：反向或零——公开信息快速定价）。

因子（个股日频，披露日口径）：
  inst_net_5 —— 近5日机构席位净买额占成交额比（top_inst net_buy 聚合）
  inst_cnt_30 —— 近30日上榜次数（游资/机构关注度）
验证：PIT 池门禁2 口径（horizon=20）。n_trials P6: +2。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import duckdb
import pandas as pd
from loguru import logger

from lihu_quantify.research.acceptance import evaluate_factor
from lihu_quantify.research.regime import classify_regime


def build_panel() -> pd.DataFrame:
    px = duckdb.query(f"SELECT ts_code, trade_date, open, high, low, close, vol, amount "
                      f"FROM '{ROOT / 'data' / 'research_panel.parquet'}'").fetchdf()
    ti = duckdb.query(f"SELECT trade_date, ts_code, net_buy, side FROM '{ROOT / 'data' / 'p6_topinst.parquet'}'").fetchdf()
    ti["trade_date"] = pd.to_datetime(ti["trade_date"], format="%Y%m%d")
    px["trade_date"] = pd.to_datetime(px["trade_date"])
    # 机构席位（side='B' 买/'S' 卖？exalter 含机构字样更准——先按 side 聚合全部席位净买）
    agg = ti.groupby(["trade_date", "ts_code"])["net_buy"].sum().reset_index(name="ti_net")
    cnt = ti.groupby(["trade_date", "ts_code"]).size().reset_index(name="ti_cnt")
    agg = agg.merge(cnt, on=["trade_date", "ts_code"])
    df = px.merge(agg, on=["ts_code", "trade_date"], how="left")
    df[["ti_net", "ti_cnt"]] = df.groupby("ts_code")[["ti_net", "ti_cnt"]].ffill().fillna(0)
    # 因子：5日净买占成交额；30日上榜次数
    df["ti_net_5"] = df.groupby("ts_code")["ti_net"].transform(
        lambda x: x.rolling(5, min_periods=1).sum()) / (df["amount"] * 10).groupby(df["ts_code"]).transform(
        lambda x: x.rolling(5, min_periods=1).sum() * 10)
    df["ti_cnt_30"] = df.groupby("ts_code")["ti_cnt"].transform(lambda x: x.rolling(30, min_periods=5).sum())
    df = df[(df["close"] > 0.5) & (df["amount"] > 0)]
    return df.sort_values(["ts_code", "trade_date"]).reset_index(drop=True)


def main() -> None:
    df = build_panel()
    logger.info(f"面板：{len(df)} 行 × {df['ts_code'].nunique()} 只")
    con = duckdb.connect(str(ROOT / "data" / "lihu_quant.duckdb"), read_only=True)
    idx = con.execute("SELECT trade_date, close FROM index_daily WHERE ts_code='000001.SH' "
                      "AND trade_date BETWEEN '2019-01-01' AND '2026-08-31' ORDER BY trade_date").fetchdf()
    con.close()
    regime = classify_regime(idx)
    out = {}
    for name, factor in (("inst_net_5", df["ti_net_5"]),
                         ("inst_cnt_30_rev", -df["ti_cnt_30"])):  # 上榜多=关注高，反向假设
        res = evaluate_factor(df, factor, name=name, horizon=20, cost_bps=15.0,
                              n_trials=8, regime=regime)
        out[name] = res.as_dict()
        d = out[name]
        logger.info(f"[{name}] RankIC={d['rank_ic_mean']:+.4f} ICIR={d['rank_ic_ir']:+.2f} "
                    f"t={d['rank_ic_t']:+.1f} 多空={d['long_short_ann']:+.2%} 单调ρ={d['group_monotonic']:+.2f}")
    p = ROOT / "outputs" / "p6_s2_result.json"
    p.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    logger.info(f"→ {p}")


if __name__ == "__main__":
    main()
