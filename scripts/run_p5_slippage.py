"""P5 滑点敏感性：策略收益对交易滑点的弹性 → 实盘容量/成本容忍度判断。

网格：{CherryClaw+VT(reduce), IntradayReversal(block)} × 滑点{0.05%, 0.1%, 0.2%} × 时段{2022-2024, 2025-2026.8}
判读：
  - 收益随滑点衰减的斜率 = 每bp成本吃掉多少年化
  - 滑点 0.2% 仍为正 → 成本容忍充足；0.1% 即转负 → 只适合低摩擦执行
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


def run(strategy, mode, start, end, slip) -> dict:
    data, idx = load_data(start, end)
    s = get_settings()
    broker = SimulatedBroker(commission_rate=s.backtest.commission,
                             stamp_tax_rate=s.backtest.stamp_tax, slippage=slip)
    ms = classify_market_state(idx)
    engine = EventDrivenEngine(strategy=strategy, broker=broker, max_single=0.25,
                               market_states=ms, market_filter_on=True, market_filter_mode=mode)
    res = engine.run(data, init_capital=s.init_capital)
    m = res.metrics
    return {"ret": round(m["total_return"], 4), "sharpe": round(m["sharpe"], 2),
            "mdd": round(m["max_drawdown"], 3), "trades": m["total_trades"]}


def main() -> None:
    s = get_settings()
    vt = s.risk.vol_target
    cc = lambda: wrap_vol_target(CherryClaw(), vt)   # noqa: E731
    ir = lambda: IntradayReversal()                   # noqa: E731
    segs = [("2022-2024", "2022-01-01", "2024-12-31"), ("2025-26", "2025-01-01", "2026-08-31")]
    slips = {"0.05%": 0.0005, "0.10%": 0.0010, "0.20%": 0.0020}

    out = {}
    for sname, mk, mode in (("CC+VT", cc, "reduce"), ("IR20", ir, "block")):
        out[sname] = {}
        for seg, a, b in segs:
            out[sname][seg] = {}
            for lbl, slip in slips.items():
                r = run(mk(), mode, a, b, slip)
                out[sname][seg][lbl] = r
                logger.info(f"[{sname} {seg} slip={lbl}] ret={r['ret']:+.2%} sharpe={r['sharpe']:+.2f} "
                            f"mdd={r['mdd']:+.1%} trades={r['trades']}")

    p = ROOT / "outputs" / "p5_slippage_result.json"
    p.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    logger.info(f"P5 结果 → {p}")


if __name__ == "__main__":
    main()
