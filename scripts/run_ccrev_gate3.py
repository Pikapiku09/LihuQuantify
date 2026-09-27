"""CC-Reversal 门禁3：策略级 A/B——IntradayReversal ± trend_filter 分年对比。

判定：开过滤后（风险调整后）不劣于基线，且亏损年（2023/2026）明显改善 → 通过。
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


def run(year: str, tf: bool) -> dict:
    a, b = f"{year}-01-01", (f"{year}-12-31" if year != "2026" else "2026-08-31")
    data, idx = load_data(a, b)
    s = get_settings()
    broker = SimulatedBroker(commission_rate=s.backtest.commission,
                             stamp_tax_rate=s.backtest.stamp_tax, slippage=s.backtest.slippage)
    ms = classify_market_state(idx)
    engine = EventDrivenEngine(strategy=IntradayReversal(trend_filter=tf), broker=broker,
                               max_single=0.25, market_states=ms,
                               market_filter_on=True, market_filter_mode="block")
    m = engine.run(data, init_capital=s.init_capital).metrics
    return {"ret": round(m["total_return"], 4), "sharpe": round(m["sharpe"], 2),
            "mdd": round(m["max_drawdown"], 3), "trades": m["total_trades"]}


def main() -> None:
    out = {}
    for y in ("2022", "2023", "2024", "2025", "2026"):
        base = run(y, False)
        tf = run(y, True)
        out[y] = {"base": base, "trend_filter": tf}
        logger.info(f"{y}: base={base} | +tf={tf}")

    p = ROOT / "outputs" / "ccrev_gate3_result.json"
    p.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    logger.info(f"→ {p}")


if __name__ == "__main__":
    main()
