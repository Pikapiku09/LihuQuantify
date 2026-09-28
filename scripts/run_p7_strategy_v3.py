"""P7 策略化 v3：宽止损 × 放宽阈值（救回周频路线的最短路径）。

v2 诊断：破 MA10 止损过于频繁 → 持有仅 3-5 天，波段退化为超短线。
v3 变更：
  stop_mode="wide"（仅 -15% 强制止损，不用 MA10）
  cs_entry_pct ∈ {0.10, 0.20}（0.20 = 信号数翻倍，提升分散度）
对照：v2 = trend + 0.10
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


def run(data, idx, stop_mode: str, cs_pct: float, mode: str = "block",
        capital: float = 1_000_000.0) -> dict:
    s = get_settings()
    broker = SimulatedBroker(commission_rate=s.backtest.commission,
                             stamp_tax_rate=s.backtest.stamp_tax, slippage=s.backtest.slippage)
    ms = classify_market_state(idx)
    engine = EventDrivenEngine(
        strategy=WeeklyBandReversal(max_position_pct=0.20, cs_entry_pct=cs_pct, stop_mode=stop_mode),
        broker=broker, max_single=0.20, market_states=ms,
        market_filter_on=True, market_filter_mode="block" if mode == "block" else "reduce")
    m = engine.run(data, init_capital=capital).metrics
    return {"ret": round(m["total_return"], 4), "annual": round(m["annual_return"], 4),
            "sharpe": round(m["sharpe"], 2), "mdd": round(m["max_drawdown"], 3),
            "calmar": round(m["calmar"], 2), "trades": m["total_trades"],
            "hold_days": round(m.get("avg_holding_days", 0), 1)}


def main() -> None:
    # 一次加载全区间（数据加载是瓶颈，配置循环复用）
    data, idx = load("20220101", "20260831")
    logger.info(f"数据就绪：{len(data)} 只")
    out = {}
    for stop_mode in ("wide", "trend"):
        for cs_pct in (0.20, 0.10):
            key = f"{stop_mode}_{cs_pct:.2f}"
            r = run(data, idx, stop_mode, cs_pct)
            out[key] = r
            logger.info(f"[{key}] 全区间: ret={r['ret']:+.2%} 年化={r['annual']:+.2%} "
                        f"夏普={r['sharpe']} 回撤={r['mdd']:.1%} 卡玛={r['calmar']} "
                        f"笔数={r['trades']} 持有={r['hold_days']}天")
    p = ROOT / "outputs" / "p7_strategy_v3_result.json"
    p.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    logger.info(f"→ {p}")


if __name__ == "__main__":
    main()
