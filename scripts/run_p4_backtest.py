"""P4 验证：个股级波动率目标仓位——CherryClaw 与 IntradayReversal 各开/关对比。

通过标准：回撤下降 + 卡玛提升 + 夏普不降（风控改进判定，非收益增强判定）。
实现：包装策略——WrapVolTarget 在 latest_signal 外套 VolTargetSizer（不改原策略代码，
回测口径与 P1 策略化完全一致：同引擎/费率/风控闸门）。
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
from lihu_quantify.risk.vol_target import VolTargetSizer
from lihu_quantify.strategy.cherry_claw import CherryClaw
from lihu_quantify.strategy.intraday_reversal import IntradayReversal


class WrapVolTarget:
    """给任意策略的买入信号套波动率目标缩放（组合代理：委托内部策略）。"""

    def __init__(self, inner, target_vol: float = 0.35):
        self.inner = inner
        self.sizer = VolTargetSizer(target_vol=target_vol)
        self.name = f"{inner.name}+VT"
        self.stateless = getattr(inner, "stateless", False)

    def _prepare_indicators(self, df):
        return self.inner._prepare_indicators(df)

    def _evaluate(self, df, indicators):
        sigs = self.inner._evaluate(df, indicators)
        d = indicators.get("df", df)
        return self.sizer.apply(sigs, d)

    def scan(self, df):
        sigs = self.inner.scan(df)
        return self.sizer.apply(sigs, df)

    def latest_signal(self, df):
        sigs = self.scan(df)
        return sigs[-1] if sigs else None

    @property
    def stop_loss_mgr(self):
        return self.inner.stop_loss_mgr


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


def run(strategy, mode: str, start: str, end: str) -> dict:
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
    out = {}
    for label, mk, mode, segs in (
        ("CherryClaw", lambda: CherryClaw(), "reduce",
         [("2022", "2022-01-01", "2022-12-31"), ("2023", "2023-01-01", "2023-12-31"),
          ("2024", "2024-01-01", "2024-12-31"), ("2025-26", "2025-01-01", "2026-08-31")]),
        ("IntradayReversal", lambda: IntradayReversal(), "block",
         [("2022", "2022-01-01", "2022-12-31"), ("2023", "2023-01-01", "2023-12-31"),
          ("2024", "2024-01-01", "2024-12-31"), ("2025-26", "2025-01-01", "2026-08-31")]),
    ):
        base, vt = {}, {}
        for seg, a, b in segs:
            base[seg] = run(mk(), mode, a, b)
            vt[seg] = run(WrapVolTarget(mk()), mode, a, b)
            logger.info(f"[{label} {seg}] base={base[seg]} vt={vt[seg]}")
        out[label] = {"base": base, "vol_target": vt}

    p = ROOT / "outputs" / "p4_vol_target_result.json"
    p.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    logger.info(f"P4 结果 → {p}")


if __name__ == "__main__":
    main()
