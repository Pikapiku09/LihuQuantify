"""第四梯队①：周频(WBR ms25)与日频(IR20)双腿相关性——决定是否双轨。

口径（各自实盘配置）：
  腿A = IntradayReversal：日频、block 择时、max_single 15%、止损-8%/MA10
  腿B = WeeklyBandReversal：周频(ms25)、无择时、max_single 2.5%、纯持有期离场
区间 2022-2026.8 全量面板。输出：日/周收益相关、50/50 组合指标。
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
from lihu_quantify.strategy.intraday_reversal import IntradayReversal
from lihu_quantify.strategy.weekly_band import WeeklyBandReversal

START, END = "20220101", "20260831"


def load():
    df = duckdb.query(f"SELECT * FROM '{ROOT / 'data' / 'research_panel_full.parquet'}' "
                      f"WHERE trade_date BETWEEN '{START}' AND '{END}'").fetchdf()
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
    s_dash = f"{START[:4]}-{START[4:6]}-{START[6:]}"
    e_dash = f"{END[:4]}-{END[4:6]}-{END[6:]}"
    con = duckdb.connect(str(ROOT / "data" / "lihu_quant.duckdb"), read_only=True)
    idx = con.execute("SELECT trade_date, close FROM index_daily WHERE ts_code='000001.SH' "
                      f"AND trade_date BETWEEN '{s_dash}' AND '{e_dash}' ORDER BY trade_date").fetchdf()
    con.close()
    idx["trade_date"] = pd.to_datetime(idx["trade_date"]).dt.date
    return data, idx


def metrics(rets: pd.Series, name: str) -> dict:
    if len(rets) == 0 or rets.std() == 0:
        return {"name": name}
    cum = float((1 + rets).prod() - 1)
    years = len(rets) / 252
    ann = (1 + cum) ** (1 / years) - 1 if years > 0 else 0.0
    sharpe = float(rets.mean() / rets.std() * np.sqrt(252))
    eq = (1 + rets).cumprod()
    mdd = float((eq / eq.cummax() - 1).min())
    calmar = ann / abs(mdd) if mdd < 0 else 0.0
    return {"name": name, "ret": round(cum, 4), "ann": round(ann, 4),
            "sharpe": round(sharpe, 2), "mdd": round(mdd, 4), "calmar": round(calmar, 2),
            "n_days": len(rets)}


def main() -> None:
    data, idx = load()
    s = get_settings()
    broker_cls = lambda: SimulatedBroker(  # noqa: E731
        commission_rate=s.backtest.commission, stamp_tax_rate=s.backtest.stamp_tax,
        slippage=s.backtest.slippage)
    ms = classify_market_state(idx)

    # 腿A：IR20 主账本口径（block）
    eng_a = EventDrivenEngine(
        strategy=IntradayReversal(max_position_pct=0.15),
        broker=broker_cls(), max_single=0.15,
        market_states=ms, market_filter_on=True, market_filter_mode="block")
    res_a = eng_a.run(data, init_capital=1_000_000)   # 统一 100 万（对齐 P7 复现口径）
    res_a.equity.to_frame("equity_a").to_csv(ROOT / "outputs" / "dual_leg_a.csv")
    logger.info(f"腿A IR20: {metrics(res_a.equity.pct_change().dropna(), 'A')['ret']}")

    # 腿B：WBR ms25 配置
    eng_b = EventDrivenEngine(
        strategy=WeeklyBandReversal(max_position_pct=0.025, cs_entry_pct=0.30),
        broker=broker_cls(), stop_loss_mgr=StopLossManager(enabled=False),
        max_single=0.025, market_states=None,
        market_filter_on=False, market_filter_mode="block")
    res_b = eng_b.run(data, init_capital=1_000_000)
    res_b.equity.to_frame("equity_b").to_csv(ROOT / "outputs" / "dual_leg_b.csv")
    logger.info(f"腿B WBR: {metrics(res_b.equity.pct_change().dropna(), 'B')['ret']}")

    ra = res_a.equity.pct_change().dropna()
    rb = res_b.equity.pct_change().dropna()
    joined = pd.concat([ra.rename("a"), rb.rename("b")], axis=1).dropna()
    joined.index = pd.to_datetime(joined.index)   # resample 需 DatetimeIndex

    out = {
        "leg_a_ir20": metrics(ra, "IR20 日频(block,15%)"),
        "leg_b_wbr": metrics(rb, "WBR 周频(ms25)"),
        "corr_daily": round(float(joined["a"].corr(joined["b"])), 4),
        "corr_weekly": round(float(joined["a"].resample("W").sum()
                                   .corr(joined["b"].resample("W").sum())), 4),
        "corr_monthly": round(float(joined["a"].resample("ME").sum()
                                    .corr(joined["b"].resample("ME").sum())), 4),
        "n_joined_days": len(joined),
    }
    # 50/50 组合
    comb = 0.5 * joined["a"] + 0.5 * joined["b"]
    out["combo_50_50"] = metrics(comb, "50/50 组合")
    # 滚动 63 日相关
    roll = joined["a"].rolling(63).corr(joined["b"]).dropna()
    out["corr_rolling63_mean"] = round(float(roll.mean()), 4)
    out["corr_rolling63_min"] = round(float(roll.min()), 4)
    out["corr_rolling63_max"] = round(float(roll.max()), 4)

    logger.info(f"日频相关={out['corr_daily']} 周频={out['corr_weekly']} "
                f"月频={out['corr_monthly']} 滚动63日均值={out['corr_rolling63_mean']}")
    logger.info(f"组合50/50: {out['combo_50_50']}")
    p = ROOT / "outputs" / "dual_leg_corr.json"
    p.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    logger.info(f"→ {p}")


if __name__ == "__main__":
    main()
