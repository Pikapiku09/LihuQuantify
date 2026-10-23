"""真实 CherryClaw 策略跑 601101 单股 2026——验证完整条件体系的实际捕获。"""
import sys
sys.path.insert(0, "E:/LihuQuantify/src")
import duckdb
import pandas as pd
from lihu_quantify.backtest.broker import SimulatedBroker
from lihu_quantify.backtest.engine import EventDrivenEngine
from lihu_quantify.config import get_settings
from lihu_quantify.market import classify_market_state
from lihu_quantify.strategy.cherry_claw import CherryClaw

con = duckdb.connect("E:/LihuQuantify/data/lihu_quant.duckdb", read_only=True)
df = con.execute("SELECT * FROM daily_quotes WHERE ts_code='601101.SH' AND trade_date BETWEEN '2024-06-01' AND '2026-08-31' ORDER BY trade_date").fetchdf()
idx = con.execute("SELECT trade_date, close FROM index_daily WHERE ts_code='000001.SH' AND trade_date BETWEEN '2024-06-01' AND '2026-08-31' ORDER BY trade_date").fetchdf()
con.close()
df["trade_date"] = pd.to_datetime(df["trade_date"]).dt.date
idx["trade_date"] = pd.to_datetime(idx["trade_date"]).dt.date
for c in ("open", "high", "low", "close"):
    df[c] = pd.to_numeric(df[c])
df["vol"] = pd.to_numeric(df["vol"])
df["amount"] = df["vol"] * df["close"]
s = get_settings()
broker = SimulatedBroker(commission_rate=s.backtest.commission, stamp_tax_rate=s.backtest.stamp_tax, slippage=s.backtest.slippage)
ms = classify_market_state(idx)
for filt in (False, True):
    eng = EventDrivenEngine(strategy=CherryClaw(), broker=broker, max_single=0.95,
                            market_states=ms, market_filter_on=filt,
                            market_filter_mode="reduce" if filt else "reduce")
    res = eng.run({"601101.SH": df}, init_capital=100000)
    m = res.metrics
    tag = "含市场过滤" if filt else "无市场过滤"
    print(f"CherryClaw 真实引擎 [{tag}]: ret={m['total_return']:+.1%} 笔数={m['total_trades']} "
          f"信号={res.signals_generated} 拒单={res.orders_rejected}")
    for t in res.trades[:8]:
        print(f"   {t.trade_date} {t.side} {t.volume}股 @ {t.price:.2f} | {t.reason[:26] if t.reason else ''}")
