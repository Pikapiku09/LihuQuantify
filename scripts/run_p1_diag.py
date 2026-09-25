"""策略回测差异诊断：为什么样本外(+14%)与全区间(-18%)矛盾？

假设：反转是超卖接刀策略，纯多头吃市场 beta——熊市年亏、牛市年赚。
验证：① 分年统计 ② 与指数对比 ③ market_filter block 模式对照
"""
from __future__ import annotations

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


def run_year(year: str, mode: str) -> dict:
    data, idx = load_data(f"{year}-01-01", f"{year}-12-31")
    if not data:
        return {"year": year, "mode": mode, "error": "no data"}
    s = get_settings()
    broker = SimulatedBroker(commission_rate=s.backtest.commission,
                             stamp_tax_rate=s.backtest.stamp_tax, slippage=s.backtest.slippage)
    ms = classify_market_state(idx)
    engine = EventDrivenEngine(strategy=IntradayReversal(), broker=broker,
                               max_single=0.25, market_states=ms,
                               market_filter_on=True, market_filter_mode=mode)
    res = engine.run(data, init_capital=s.init_capital)
    m = res.metrics
    idx_ret = float(idx["close"].iloc[-1] / idx["close"].iloc[0] - 1)
    return {"year": year, "mode": mode, "ret": round(m["total_return"], 4),
            "idx_ret": round(idx_ret, 4), "excess": round(m["total_return"] - idx_ret, 4),
            "sharpe": round(m["sharpe"], 2), "mdd": round(m["max_drawdown"], 3),
            "trades": m["total_trades"], "win": round(m["win_rate"], 2)}


def main() -> None:
    rows = []
    for y in ("2019", "2020", "2021", "2022", "2023", "2024", "2025"):
        if y == "2025":
            rows.append(run_year("2025", "reduce"))       # 样本外沿用 reduce
        else:
            rows.append(run_year(y, "reduce"))
            rows.append(run_year(y, "block"))             # 熊市切换对照
    print("\n年份 | 模式 | 策略收益 | 指数 | 超额 | 夏普 | 回撤 | 笔数 | 胜率")
    for r in rows:
        if "error" in r:
            print(r); continue
        print(f"{r['year']} | {r['mode']} | {r['ret']:+.2%} | {r['idx_ret']:+.2%} | "
              f"{r['excess']:+.2%} | {r['sharpe']:+.2f} | {r['mdd']:+.2%} | {r['trades']} | {r['win']:.0%}")


if __name__ == "__main__":
    main()
