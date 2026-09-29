import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
import duckdb, pandas as pd
from lihu_quantify.backtest.broker import SimulatedBroker
from lihu_quantify.backtest.engine import EventDrivenEngine
from lihu_quantify.config import get_settings
from lihu_quantify.market import classify_market_state
from lihu_quantify.risk.stop_loss import StopLossManager
from lihu_quantify.strategy.weekly_band import WeeklyBandReversal
import numpy as np

df = duckdb.query("SELECT * FROM 'data/research_panel_full.parquet' WHERE trade_date BETWEEN '20240101' AND '20241231'").fetchdf()
df["trade_date"] = pd.to_datetime(df["trade_date"], format="%Y%m%d").dt.date
df = df.sort_values(["ts_code", "trade_date"])
codes = sorted(df["ts_code"].unique())[:200]
df = df[df["ts_code"].isin(codes)]
df["wbr"] = -np.log(df["close"] / df["open"]).groupby(df["ts_code"]).transform(lambda s: s.rolling(20, min_periods=10).sum())
df["wbr_cs_pct"] = df.groupby("trade_date")["wbr"].rank(pct=True, ascending=False)
data = {c: g.sort_values("trade_date").reset_index(drop=True) for c, g in df.groupby("ts_code") if len(g) >= 60}
con = duckdb.connect("data/lihu_quant.duckdb", read_only=True)
idx = con.execute("SELECT trade_date, close FROM index_daily WHERE ts_code='000001.SH' AND trade_date BETWEEN '2024-01-01' AND '2024-12-31' ORDER BY trade_date").fetchdf()
con.close()
idx["trade_date"] = pd.to_datetime(idx["trade_date"]).dt.date
s = get_settings()
broker = SimulatedBroker(commission_rate=s.backtest.commission, stamp_tax_rate=s.backtest.stamp_tax, slippage=s.backtest.slippage)
eng = EventDrivenEngine(
    strategy=WeeklyBandReversal(max_position_pct=0.05, cs_entry_pct=0.30),
    broker=broker,
    stop_loss_mgr=StopLossManager(enabled=False),
    max_single=0.05,
    market_states=None, market_filter_on=False, market_filter_mode="block")
res = eng.run(data, init_capital=1_000_000.0)
m = res.metrics
print(f"结果: ret={m['total_return']:+.2%} trades={m['total_trades']} 信号={res.signals_generated} 闸门拒={res.orders_rejected} 持有={m.get('avg_holding_days',0)}天")
# 期末持仓与到期日检查
print(f"期末持仓数: {len(res.portfolio.positions)}")
