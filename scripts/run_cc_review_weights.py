"""CherryClaw 复盘 Step3：CC vs IR20 权重扫描（组合层决策依据）。

区间 2022-2026.8（两策略同池同引擎，修复后口径）
权重：{100/0, 75/25, 50/50, 25/75, 0/100}，年度等权再平衡（分年计算后几何累乘）
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

from lihu_quantify.backtest.broker import SimulatedBroker
from lihu_quantify.backtest.engine import EventDrivenEngine
from lihu_quantify.config import get_settings
from lihu_quantify.market import classify_market_state
from lihu_quantify.strategy.cherry_claw import CherryClaw
from lihu_quantify.strategy.intraday_reversal import IntradayReversal
from lihu_quantify.strategy.vol_wrap import wrap_vol_target


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


def run_year(year: str, kind: str) -> float:
    a, b = f"{year}-01-01", f"{year}-12-31"
    if year == "2026":
        b = "2026-08-31"
    data, idx = load_data(a, b)
    if not data:
        return 0.0
    s = get_settings()
    broker = SimulatedBroker(commission_rate=s.backtest.commission,
                             stamp_tax_rate=s.backtest.stamp_tax, slippage=s.backtest.slippage)
    ms = classify_market_state(idx)
    if kind == "cc":
        strat, mode = wrap_vol_target(CherryClaw(), s.risk.vol_target), "reduce"
    else:
        strat, mode = IntradayReversal(), "block"
    engine = EventDrivenEngine(strategy=strat, broker=broker, max_single=0.25,
                               market_states=ms, market_filter_on=True, market_filter_mode=mode)
    return engine.run(data, init_capital=s.init_capital).metrics["total_return"]


def main() -> None:
    years = ["2022", "2023", "2024", "2025", "2026"]
    per = {"cc": {}, "ir": {}}
    for y in years:
        per["cc"][y] = run_year(y, "cc")
        per["ir"][y] = run_year(y, "ir")
        logger.info(f"{y}: CC={per['cc'][y]:+.2%} IR20={per['ir'][y]:+.2%}")

    weights = [(1.0, 0.0), (0.75, 0.25), (0.5, 0.5), (0.25, 0.75), (0.0, 1.0)]
    out = {"per_year": per, "weights": []}
    for wc, wi in weights:
        cum = 1.0
        for y in years:
            r = wc * per["cc"][y] + wi * per["ir"][y]
            cum *= (1 + r)
        ann = cum ** (1 / 4.67) - 1
        row = {"cc": wc, "ir": wi, "cum_return": round(cum - 1, 4), "annual": round(ann, 4)}
        out["weights"].append(row)
        logger.info(f"权重 CC{int(wc*100)}/IR{int(wi*100)}: 累计 {cum-1:+.2%} 年化 {ann:+.2%}")

    p = ROOT / "outputs" / "cc_review_weights.json"
    p.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    logger.info(f"→ {p}")


if __name__ == "__main__":
    main()
