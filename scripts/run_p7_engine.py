"""P7 真实引擎复现：WeeklyBandReversal 持有期到期版（评审第三梯队 ⑦）。

对齐组合模拟口径（docs/papers/P7_策略化终版_正确形态.md，年化 +15.8%）：
  - 周一信号 + 截面最超卖 30% + 流动性 ≥1250万/日
  - holding_days=20（4周兑现，引擎到期平仓——第一梯队引擎改造②）
  - 止损全禁用（StopLossManager(enabled=False)——纯持有期离场）
  - 无市场择时（组合模拟口径）；max_single=5%（100万持 20 只，近似分散）
  - 全约束不变：T+1/涨跌停拒单/佣金印花税滑点/整手
另跑 block 择时版（实际部署形态参考）。
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
from lihu_quantify.risk.stop_loss import StopLossManager
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


def run(data, idx, *, with_block: bool, cs_pct: float = 0.30,
        max_single: float = 0.05,
        capital: float = 1_000_000.0) -> dict:
    s = get_settings()
    broker = SimulatedBroker(commission_rate=s.backtest.commission,
                             stamp_tax_rate=s.backtest.stamp_tax, slippage=s.backtest.slippage)
    ms = classify_market_state(idx) if with_block else None
    engine = EventDrivenEngine(
        strategy=WeeklyBandReversal(max_position_pct=max_single, cs_entry_pct=cs_pct),
        broker=broker,
        stop_loss_mgr=StopLossManager(enabled=False),   # 纯持有期离场
        max_single=max_single,
        market_states=ms,
        market_filter_on=with_block,
        market_filter_mode="block",
    )
    res = engine.run(data, init_capital=capital)
    m = res.metrics
    return {"ret": round(m["total_return"], 4), "annual": round(m["annual_return"], 4),
            "sharpe": round(m["sharpe"], 2), "mdd": round(m["max_drawdown"], 3),
            "calmar": round(m["calmar"], 2), "trades": m["total_trades"],
            "hold_days": round(m.get("avg_holding_days", 0), 1),
            "signals": res.signals_generated, "rejected": res.orders_rejected}


def main() -> None:
    data, idx = load("20220101", "20260831")
    logger.info(f"数据就绪：{len(data)} 只")
    out = {}
    # 现金时序修复后主对照：5% 单票
    r = run(data, idx, with_block=False, max_single=0.05)
    out["engine_ms5"] = r
    logger.info(f"[engine_ms5] 全区间: ret={r['ret']:+.2%} 年化={r['annual']:+.2%} "
                f"夏普={r['sharpe']} 回撤={r['mdd']:.1%} 卡玛={r['calmar']} "
                f"笔数={r['trades']} 持有={r['hold_days']}天 拒单={r['rejected']}")
    # 分散度对照：2.5% 单票（40 只）
    r2 = run(data, idx, with_block=False, max_single=0.025)
    out["engine_ms25"] = r2
    logger.info(f"[engine_ms25] 全区间: ret={r2['ret']:+.2%} 年化={r2['annual']:+.2%} "
                f"夏普={r2['sharpe']} 回撤={r2['mdd']:.1%} 卡玛={r2['calmar']} "
                f"笔数={r2['trades']} 持有={r2['hold_days']}天 拒单={r2['rejected']}")
    # 分年（5% 版）
    for y in ("2022", "2023", "2024", "2025", "2026"):
        end = "20260831" if y == "2026" else f"{y}1231"
        dy, di = load(f"{y}0101", end)
        r = run(dy, di, with_block=False, max_single=0.05)
        out[f"y{y}"] = r
        logger.info(f"[ms5] {y}: ret={r['ret']:+.2%} 夏普={r['sharpe']} "
                    f"回撤={r['mdd']:.1%} 笔数={r['trades']} 拒单={r['rejected']}")
    p = ROOT / "outputs" / "p7_engine_v2_result.json"
    p.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    logger.info(f"→ {p}")


if __name__ == "__main__":
    main()
