"""P7 策略化 v2：截面口径版 WeeklyBandReversal（对齐门禁2）。

关键修正：门禁2 用截面分位（每周全市场排序取最超卖），v1 误用自身历史分位 → 信号稀疏。
v2：预注入 wbr_cs_pct（按 trade_date 全市场降序分位），策略取最超卖 10%。
"""
from __future__ import annotations

import json
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
from lihu_quantify.market import classify_market_state
from lihu_quantify.strategy.weekly_band import WeeklyBandReversal


def load(start: str, end: str):
    df = duckdb.query(f"SELECT * FROM '{ROOT / 'data' / 'research_panel_full.parquet'}' "
                      f"WHERE trade_date BETWEEN '{start}' AND '{end}'").fetchdf()
    df["trade_date"] = pd.to_datetime(df["trade_date"], format="%Y%m%d").dt.date
    # 截面因子：20日日内收益取负 → 当日全市场降序分位（越超卖分位越小）
    df = df.sort_values(["ts_code", "trade_date"])
    df["wbr"] = -np.log(df["close"] / df["open"]).groupby(df["ts_code"]).transform(
        lambda s: s.rolling(20, min_periods=10).sum())
    df["wbr_cs_pct"] = df.groupby("trade_date")["wbr"].rank(pct=True, ascending=False)
    data = {}
    for code, g in df.groupby("ts_code"):
        g = g.sort_values("trade_date").reset_index(drop=True)
        if len(g) >= 60:
            data[code] = g
    s_dash = f"{start[:4]}-{start[4:6]}-{start[6:]}"
    e_dash = f"{end[:4]}-{end[4:6]}-{end[6:]}"
    con = duckdb.connect(str(ROOT / "data" / "lihu_quant.duckdb"), read_only=True)
    idx = con.execute("SELECT trade_date, close FROM index_daily WHERE ts_code='000001.SH' "
                      f"AND trade_date BETWEEN '{s_dash}' AND '{e_dash}' ORDER BY trade_date").fetchdf()
    con.close()
    idx["trade_date"] = pd.to_datetime(idx["trade_date"]).dt.date
    return data, idx


def run(start: str, end: str, mode: str = "block", cs_pct: float = 0.10,
        max_single: float = 0.20, capital: float = 1_000_000.0) -> dict:
    data, idx = load(start, end)
    s = get_settings()
    broker = SimulatedBroker(commission_rate=s.backtest.commission,
                             stamp_tax_rate=s.backtest.stamp_tax, slippage=s.backtest.slippage)
    ms = classify_market_state(idx)
    engine = EventDrivenEngine(
        strategy=WeeklyBandReversal(max_position_pct=max_single, cs_entry_pct=cs_pct),
        broker=broker, max_single=max_single, market_states=ms,
        market_filter_on=(mode != "off"), market_filter_mode="block" if mode == "block" else "reduce")
    res = engine.run(data, init_capital=capital)
    m = res.metrics
    return {"ret": round(m["total_return"], 4), "annual": round(m["annual_return"], 4),
            "sharpe": round(m["sharpe"], 2), "mdd": round(m["max_drawdown"], 3),
            "calmar": round(m["calmar"], 2), "trades": m["total_trades"],
            "hold_days": round(m.get("avg_holding_days", 0), 1)}


def main() -> None:
    out = {}
    for y in ("2022", "2023", "2024", "2025", "2026"):
        end = "20260831" if y == "2026" else f"{y}1231"
        r = run(f"{y}0101", end)
        out[y] = r
        logger.info(f"[v2 block] {y}: ret={r['ret']:+.2%} 夏普={r['sharpe']} 回撤={r['mdd']:.1%} "
                    f"笔数={r['trades']} 持有={r['hold_days']}天")
    r = run("20220101", "20260831")
    out["full_2022_2026"] = r
    logger.info(f"[v2 block] 全区间: ret={r['ret']:+.2%} 年化={r['annual']:+.2%} 夏普={r['sharpe']} "
                f"回撤={r['mdd']:.1%} 卡玛={r['calmar']} 笔数={r['trades']}")
    p = ROOT / "outputs" / "p7_strategy_v2_result.json"
    p.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    logger.info(f"→ {p}")


if __name__ == "__main__":
    main()
