"""滑点方向修复后的影响复验：重跑 P1 策略化与 P4 A/B 的关键段。

背景：broker 滑点方向 bug（2026-09-26 修复）使历史回测低估成本，
需复验此前结论是否成立（结论方向是否翻转）。
复验对象（同口径重跑）：
  P1: IntradayReversal block 2025-26（原 +14.5%）与 2022-2024（原 +37.4%）
  P4: CherryClaw ±vol_target 2025-26（原 base -6.3% / vt +3.0%）
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


def run(strategy, mode, start, end) -> dict:
    data, idx = load_data(start, end)
    s = get_settings()
    broker = SimulatedBroker(commission_rate=s.backtest.commission,
                             stamp_tax_rate=s.backtest.stamp_tax, slippage=s.backtest.slippage)
    ms = classify_market_state(idx)
    engine = EventDrivenEngine(strategy=strategy, broker=broker, max_single=0.25,
                               market_states=ms, market_filter_on=True, market_filter_mode=mode)
    res = engine.run(data, init_capital=s.init_capital)
    m = res.metrics
    return {"ret": round(m["total_return"], 4), "sharpe": round(m["sharpe"], 2),
            "mdd": round(m["max_drawdown"], 3), "calmar": round(m["calmar"], 2),
            "trades": m["total_trades"]}


def main() -> None:
    s = get_settings()
    out = {}
    out["P1_IR20_2022_2024"] = run(IntradayReversal(), "block", "2022-01-01", "2024-12-31")
    logger.info(f"P1 IR20 2022-2024: {out['P1_IR20_2022_2024']}")
    out["P1_IR20_2025_26"] = run(IntradayReversal(), "block", "2025-01-01", "2026-08-31")
    logger.info(f"P1 IR20 2025-26: {out['P1_IR20_2025_26']}")
    out["P4_CC_base_2025_26"] = run(CherryClaw(), "reduce", "2025-01-01", "2026-08-31")
    logger.info(f"P4 CC base 2025-26: {out['P4_CC_base_2025_26']}")
    out["P4_CC_vt_2025_26"] = run(wrap_vol_target(CherryClaw(), s.risk.vol_target), "reduce",
                                  "2025-01-01", "2026-08-31")
    logger.info(f"P4 CC+VT 2025-26: {out['P4_CC_vt_2025_26']}")
    out["P4_CC_base_2022_2024"] = run(CherryClaw(), "reduce", "2022-01-01", "2024-12-31")
    out["P4_CC_vt_2022_2024"] = run(wrap_vol_target(CherryClaw(), s.risk.vol_target), "reduce",
                                    "2022-01-01", "2024-12-31")
    logger.info(f"P4 2022-2024 base={out['P4_CC_base_2022_2024']} vt={out['P4_CC_vt_2022_2024']}")

    p = ROOT / "outputs" / "p5_impact_recheck.json"
    p.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    logger.info(f"复验结果 → {p}")


if __name__ == "__main__":
    main()
