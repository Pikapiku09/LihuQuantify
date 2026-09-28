"""P7 策略化回测：WeeklyBandReversal 全量面板分年（vs IR20 日频版对照）。

全量 2810 只（2019-2026 面板）× block 择时 × 全约束引擎（T+1/涨跌停/费用）
输出：分年 ret/sharpe/mdd/trades + 区间累计
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
from lihu_quantify.strategy.weekly_band import WeeklyBandReversal


def load(start: str, end: str):
    df = duckdb.query(f"SELECT * FROM '{ROOT / 'data' / 'research_panel_full.parquet'}' "
                      f"WHERE trade_date BETWEEN '{start}' AND '{end}'").fetchdf()
    df["trade_date"] = pd.to_datetime(df["trade_date"], format="%Y%m%d").dt.date
    data = {}
    for code, g in df.groupby("ts_code"):
        g = g.sort_values("trade_date").reset_index(drop=True)
        if len(g) >= 60:
            data[code] = g
    # index_daily.trade_date 是 DATE 类型 → 用 YYYY-MM-DD 格式
    s_dash = f"{start[:4]}-{start[4:6]}-{start[6:]}"
    e_dash = f"{end[:4]}-{end[4:6]}-{end[6:]}"
    con = duckdb.connect(str(ROOT / "data" / "lihu_quant.duckdb"), read_only=True)
    idx = con.execute("SELECT trade_date, close FROM index_daily WHERE ts_code='000001.SH' "
                      f"AND trade_date BETWEEN '{s_dash}' AND '{e_dash}' ORDER BY trade_date").fetchdf()
    con.close()
    idx["trade_date"] = pd.to_datetime(idx["trade_date"]).dt.date
    return data, idx


def run(start: str, end: str, max_single: float = 0.20, capital: float = 1_000_000.0) -> dict:
    data, idx = load(start, end)
    s = get_settings()
    broker = SimulatedBroker(commission_rate=s.backtest.commission,
                             stamp_tax_rate=s.backtest.stamp_tax, slippage=s.backtest.slippage)
    ms = classify_market_state(idx)
    engine = EventDrivenEngine(strategy=WeeklyBandReversal(max_position_pct=max_single),
                               broker=broker, max_single=max_single, market_states=ms,
                               market_filter_on=True, market_filter_mode="block")
    res = engine.run(data, init_capital=capital)
    m = res.metrics
    return {"ret": round(m["total_return"], 4), "annual": round(m["annual_return"], 4),
            "sharpe": round(m["sharpe"], 2), "mdd": round(m["max_drawdown"], 3),
            "calmar": round(m["calmar"], 2), "trades": m["total_trades"],
            "final_equity": round(m["final_equity"], 0), "stocks": len(data)}


def main() -> None:
    out = {}
    # 分年（全量池，100 万本金避免小资金手数限制）
    for y in ("2022", "2023", "2024", "2025", "2026"):
        end = "20260831" if y == "2026" else f"{y}1231"
        r = run(f"{y}0101", end)
        out[y] = r
        logger.info(f"{y}: ret={r['ret']:+.2%} 年化={r['annual']:+.2%} 夏普={r['sharpe']} "
                    f"回撤={r['mdd']:.1%} 笔数={r['trades']} 池={r['stocks']}只")
    # 全区间
    r = run("20220101", "20260831")
    out["full_2022_2026"] = r
    logger.info(f"全区间: ret={r['ret']:+.2%} 年化={r['annual']:+.2%} 夏普={r['sharpe']} "
                f"回撤={r['mdd']:.1%} 卡玛={r['calmar']} 笔数={r['trades']}")
    p = ROOT / "outputs" / "p7_strategy_result.json"
    p.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    logger.info(f"→ {p}")


if __name__ == "__main__":
    main()
