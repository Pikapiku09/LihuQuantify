"""P1 策略化回测：IntradayReversal 全约束（T+1/涨跌停/费用/风控闸门）。

数据：data/research_panel.parquet（前复权，2019-01~2026-08）
区间：
    主验证 = 样本外 2025-01-02 ~ 2026-08-31（门禁3 用过的只有因子 IC 口径，
             引擎级策略回测含换手约束/风控闸门/仓位限制，仍是首次）
    对照 = 2019-2024 全区间
"""
from __future__ import annotations

import json
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import duckdb
import pandas as pd
from loguru import logger

from lihu_quantify.backtest.broker import SimulatedBroker
from lihu_quantify.backtest.engine import EventDrivenEngine
from lihu_quantify.config import get_settings
from lihu_quantify.data.tushare_client import TushareClient
from lihu_quantify.data.duckdb_store import DuckDBStore
from lihu_quantify.market import classify_market_state
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


def run_segment(label: str, start: str, end: str):
    data, idx = load_data(start, end)
    logger.info(f"[{label}] {len(data)} 只 | {start} ~ {end}")
    s = get_settings()
    broker = SimulatedBroker(
        commission_rate=s.backtest.commission, stamp_tax_rate=s.backtest.stamp_tax,
        slippage=s.backtest.slippage)
    market_states = classify_market_state(idx)   # 保守：reduce 模式
    engine = EventDrivenEngine(
        strategy=IntradayReversal(), broker=broker,
        max_single=0.25, market_states=market_states,
        market_filter_on=True, market_filter_mode="reduce",
    )
    result = engine.run(data, init_capital=s.init_capital, start=None, end=None)
    m = result.metrics
    summary = {
        "segment": label, "signals": result.signals_generated,
        "rejected": result.orders_rejected,
        "total_return": round(m["total_return"], 4), "annual_return": round(m["annual_return"], 4),
        "sharpe": round(m["sharpe"], 2), "max_drawdown": round(m["max_drawdown"], 4),
        "calmar": round(m["calmar"], 2), "win_rate": round(m["win_rate"], 3),
        "profit_loss_ratio": round(m["profit_loss_ratio"], 2),
        "total_trades": m["total_trades"], "avg_cost_ratio": round(m["avg_cost_ratio"], 5),
    }
    logger.info(f"[{label}] " + json.dumps(summary, ensure_ascii=False))
    return summary


def main() -> None:
    out = {}
    out["oos_2025_2026"] = run_segment("样本外 2025-2026.8", "2025-01-02", "2026-08-31")
    out["full_2019_2024"] = run_segment("全区间 2019-2024", "2019-01-02", "2024-12-31")
    p = ROOT / "outputs" / "p1_strategy_backtest.json"
    p.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    logger.info(f"策略回测汇总 → {p}")


if __name__ == "__main__":
    main()
