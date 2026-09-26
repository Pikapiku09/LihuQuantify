"""CherryClaw 复盘 Step1：成本拆解（含零成本对照）。

输出：
  - 净收益 vs 毛收益（费率全 0）→ 成本吃掉的年化
  - 交易统计：笔数/年、平均持有天数、胜率、盈亏比
  - 费用结构：总费用、费用/初始资金、单笔平均费用
  - 换手率与成本敏感性
"""
from __future__ import annotations

import json
import sys
from collections import Counter
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
from lihu_quantify.strategy.cherry_claw import CherryClaw
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


def run(start, end, zero_cost: bool):
    data, idx = load_data(start, end)
    s = get_settings()
    if zero_cost:
        broker = SimulatedBroker(commission_rate=0.0, stamp_tax_rate=0.0, slippage=0.0)
    else:
        broker = SimulatedBroker(commission_rate=s.backtest.commission,
                                 stamp_tax_rate=s.backtest.stamp_tax, slippage=s.backtest.slippage)
    ms = classify_market_state(idx)
    strat = wrap_vol_target(CherryClaw(), s.risk.vol_target)
    engine = EventDrivenEngine(strategy=strat, broker=broker, max_single=0.25,
                               market_states=ms, market_filter_on=True, market_filter_mode="reduce")
    res = engine.run(data, init_capital=s.init_capital)
    return res, idx


def trade_stats(res) -> dict:
    sells = [t for t in res.trades if t.side == "sell"]
    wins = [t for t in sells if t.pnl > 0]
    losses = [t for t in sells if t.pnl < 0]
    hold = []
    buy_by_code = {}
    for t in res.trades:
        if t.side == "buy":
            buy_by_code.setdefault(t.ts_code, []).append(t.trade_date)
        elif t.side == "sell" and buy_by_code.get(t.ts_code):
            b = buy_by_code[t.ts_code].pop(0)
            if b and t.trade_date:
                hold.append((t.trade_date - b).days)
    reason = Counter((t.reason or "?")[:20] for t in sells)
    return {
        "sell_trades": len(sells),
        "win_trades": len(wins), "loss_trades": len(losses),
        "avg_win": round(float(np.mean([t.pnl for t in wins])), 2) if wins else 0.0,
        "avg_loss": round(float(np.mean([t.pnl for t in losses])), 2) if losses else 0.0,
        "total_pnl": round(sum(t.pnl for t in sells), 2),
        "avg_hold_days": round(float(np.mean(hold)), 1) if hold else None,
        "sell_reasons_top": reason.most_common(6),
    }


def main() -> None:
    start, end = "2022-01-01", "2026-08-31"
    span_years = 4.67
    out = {}
    for tag, zero in (("net", False), ("gross_zero_cost", True)):
        res, _ = run(start, end, zero)
        m = res.metrics
        st = trade_stats(res)
        out[tag] = {
            "total_return": round(m["total_return"], 4),
            "annual_return": round(m["annual_return"], 4),
            "sharpe": round(m["sharpe"], 2),
            "mdd": round(m["max_drawdown"], 4),
            "total_fees": round(m["total_fees"], 2),
            "cost_ratio": round(m["avg_cost_ratio"], 5),
            "trades_per_year": round(m["total_trades"] / span_years, 1),
            **st,
        }
        logger.info(f"[{tag}] ret={m['total_return']:+.2%} ann={m['annual_return']:+.2%} "
                    f"fees={m['total_fees']:.0f} cost_ratio={m['avg_cost_ratio']:.4%} "
                    f"卖出笔数={st['sell_trades']} 胜={st['win_trades']} 负={st['loss_trades']} "
                    f"均盈={st['avg_win']} 均亏={st['avg_loss']} 平均持有={st['avg_hold_days']}天")

    cost_drag = out["gross_zero_cost"]["annual_return"] - out["net"]["annual_return"]
    out["cost_drag_annual"] = round(cost_drag, 4)
    logger.info(f"成本拖累年化 = {cost_drag:+.2%}（毛 {out['gross_zero_cost']['annual_return']:+.2%} → 净 {out['net']['annual_return']:+.2%}）")
    p = ROOT / "outputs" / "cc_review_cost.json"
    p.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    logger.info(f"→ {p}")


if __name__ == "__main__":
    main()
