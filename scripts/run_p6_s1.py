"""P6-S1：市场情绪温度计三指标 + 对 IR20 的择时增强 A/B。

指标（逐日）：
  limit_count —— 非一字涨停家数（limit=U 且 first_time<093000 之外；简化：limit=U 计数）
  max_streak  — 连板高度（up_stat 'N/M' 取当日最大 N）
  fail_rate   — 炸板率（Z / (U+Z)）
A/B 设计：
  基线：IR20 + block（现有 market_filter，指数20日涨幅口径）
  增强：IR20 + block(情绪口径：fail_rate>30% 或 max_streak>=5 → 退潮禁开仓)
  分年对比。n_trials P6: 3（三指标各一个 A/B 窗口定义）。
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import duckdb
import numpy as np
import pandas as pd
from loguru import logger

from lihu_quantify.backtest.broker import SimulatedBroker
from lihu_quantify.backtest.engine import EventDrivenEngine
from lihu_quantify.config import get_settings
from lihu_quantify.strategy.intraday_reversal import IntradayReversal


def build_sentiment() -> pd.DataFrame:
    lim = duckdb.query(f'SELECT trade_date, ts_code, "limit", up_stat, open_times, first_time '
                       f"FROM '{ROOT / 'data' / 'p6_limit.parquet'}'").fetchdf()
    lim["trade_date"] = pd.to_datetime(lim["trade_date"], format="%Y%m%d")
    # up_stat '4/6' → 取 N（连板天数）
    lim["streak"] = lim["up_stat"].astype(str).str.extract(r"^(\d+)").astype(float)
    g = lim.groupby("trade_date")
    sent = pd.DataFrame({
        "limit_u": g.apply(lambda x: (x["limit"] == "U").sum()),
        "limit_z": g.apply(lambda x: (x["limit"] == "Z").sum()),
        "max_streak": g["streak"].max(),
    }).reset_index()
    sent["fail_rate"] = sent["limit_z"] / (sent["limit_u"] + sent["limit_z"]).clip(lower=1)
    sent["limit_count_ma5"] = sent["limit_u"].rolling(5, min_periods=3).mean()
    return sent


def load_data(start: str, end: str):
    df = duckdb.query(f"SELECT * FROM '{ROOT / 'data' / 'research_panel.parquet'}' "
                      f"WHERE trade_date BETWEEN '{start}' AND '{end}'").fetchdf()
    df["trade_date"] = pd.to_datetime(df["trade_date"]).dt.date
    data = {}
    for code, g in df.groupby("ts_code"):
        g = g.sort_values("trade_date").reset_index(drop=True)
        if len(g) >= 60:
            data[code] = g
    con = duckdb.connect(str(ROOT / "data" / "lihu_quant.duckdb"), read_only=True)
    idx = con.execute("SELECT trade_date, close FROM index_daily WHERE ts_code='000001.SH' "
                      f"AND trade_date BETWEEN '{start}' AND '{end}' ORDER BY trade_date").fetchdf()
    con.close()
    idx["trade_date"] = pd.to_datetime(idx["trade_date"]).dt.date
    return data, idx


def run(year: str, sent_gate=None) -> dict:
    a, b = f"{year}-01-01", (f"{year}-12-31" if year != "2026" else "2026-08-31")
    data, idx = load_data(a, b)
    s = get_settings()
    broker = SimulatedBroker(commission_rate=s.backtest.commission,
                             stamp_tax_rate=s.backtest.stamp_tax, slippage=s.backtest.slippage)
    # 市场状态：基线用 classify_market_state；情绪门控版在其基础上叠加"退潮日禁开仓"
    from lihu_quantify.market import classify_market_state
    ms = classify_market_state(idx)
    if sent_gate is not None:
        # 叠加：情绪退潮日把状态改为 down（block 模式即禁开仓）
        # 修复：ms 的 key 是 date，sent_gate 的 key 是 Timestamp → 统一转 date
        gate_by_date = {}
        for d, is_ebb in sent_gate.items():
            gd = d.date() if hasattr(d, "date") else d
            gate_by_date[gd] = bool(is_ebb)
        for d in list(ms.keys()):
            if gate_by_date.get(d, False):
                ms[d] = "down"
    engine = EventDrivenEngine(strategy=IntradayReversal(), broker=broker, max_single=0.25,
                               market_states=ms, market_filter_on=True, market_filter_mode="block")
    m = engine.run(data, init_capital=s.init_capital).metrics
    return {"ret": round(m["total_return"], 4), "sharpe": round(m["sharpe"], 2),
            "mdd": round(m["max_drawdown"], 3), "trades": m["total_trades"]}


def main() -> None:
    sent = build_sentiment()
    logger.info(f"情绪指标 {len(sent)} 天 | fail_rate 均值 {sent['fail_rate'].mean():.1%} | "
                f"max_streak 均值 {sent['max_streak'].mean():.1f}")

    # 三种情绪门控定义（A/B 变体）
    gates = {
        "S1a_fail_rate": sent.set_index("trade_date")["fail_rate"].gt(0.30).to_dict(),
        "S1b_max_streak": sent.set_index("trade_date")["max_streak"].ge(5).to_dict(),
        "S1c_count_cold": sent.set_index("trade_date")["limit_u"].le(20).to_dict(),  # 冰点（涨停极少）
    }
    out = {}
    for y in ("2022", "2023", "2024", "2025", "2026"):
        base = run(y)
        row = {"base": base}
        for gname, g in gates.items():
            row[gname] = run(y, sent_gate={k: bool(v) for k, v in g.items()})
        out[y] = row
        logger.info(f"{y}: base={base['ret']:+.2%} | fail30={row['S1a_fail_rate']['ret']:+.2%} | "
                    f"streak5={row['S1b_max_streak']['ret']:+.2%} | cold={row['S1c_count_cold']['ret']:+.2%}")

    p = ROOT / "outputs" / "p6_s1_result.json"
    p.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    logger.info(f"→ {p}")


if __name__ == "__main__":
    main()
