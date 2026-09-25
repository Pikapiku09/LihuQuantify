"""组合回测：CherryClaw(趋势,reduce) + IntradayReversal(反转,block) 各50%。

假设：趋势吃牛市beta + 反转做防御超额 → 组合逐年超额更稳。
口径：每年初各分配50%资金独立复利，年末合并（年度等权再平衡）。
对照：单策略 / 组合 / 指数。
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


def run_one(strategy, mode: str, year: str) -> dict:
    data, idx = load_data(f"{year}-01-01", f"{year}-12-31")
    if not data:
        return {"ret": 0.0, "mdd": 0.0, "trades": 0, "sharpe": 0.0}
    s = get_settings()
    broker = SimulatedBroker(commission_rate=s.backtest.commission,
                             stamp_tax_rate=s.backtest.stamp_tax, slippage=s.backtest.slippage)
    ms = classify_market_state(idx)
    engine = EventDrivenEngine(strategy=strategy, broker=broker, max_single=0.25,
                               market_states=ms, market_filter_on=True, market_filter_mode=mode)
    res = engine.run(data, init_capital=s.init_capital)
    m = res.metrics
    return {"ret": round(m["total_return"], 4), "mdd": round(m["max_drawdown"], 3),
            "trades": m["total_trades"], "sharpe": round(m["sharpe"], 2)}


def main() -> None:
    rows = []
    for y in ("2019", "2020", "2021", "2022", "2023", "2024", "2025"):
        cc = run_one(CherryClaw(), "reduce", y)
        ir = run_one(IntradayReversal(), "block", y)
        _, idx = load_data(f"{y}-01-01", f"{y}-12-31")
        idx_ret = float(idx["close"].iloc[-1] / idx["close"].iloc[0] - 1) if len(idx) else 0.0
        combo = (cc["ret"] + ir["ret"]) / 2
        rows.append({"year": y, "cc": cc, "ir": ir, "combo": round(combo, 4),
                     "idx": round(idx_ret, 4)})
        logger.info(f"{y}: CC={cc['ret']:+.2%} IR={ir['ret']:+.2%} 组合={combo:+.2%} 指数={idx_ret:+.2%}")

    # 累计（年度等权再平衡复利）
    def cum(key):
        r = 1.0
        for x in rows:
            r *= 1 + (x[key] if isinstance(x[key], float) else x[key]["ret"])
        return r - 1
    summary = {
        "years": rows,
        "cum": {"cherry_claw": round(cum("cc"), 4), "intraday_rev": round(cum("ir"), 4),
                "combo": round(cum("combo"), 4), "index": round(cum("idx"), 4)},
    }
    out = ROOT / "outputs" / "p1_combo_backtest.json"
    out.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    logger.info("累计7年: " + json.dumps(summary["cum"], ensure_ascii=False))


if __name__ == "__main__":
    main()
