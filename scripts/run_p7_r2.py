"""P7-R2：流动性过滤版——全市场信号 + 可交易子集执行。

过滤：滚动 4 周均成交额 ≥ 5000 万（周频口径的可交易门槛）
因子：wk_intraday_rev（R1 领头羊）+ rev_4w（对照）
n_trials P7: 5→7。
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import duckdb
import pandas as pd
from loguru import logger

from lihu_quantify.research.acceptance import evaluate_factor
from lihu_quantify.research.regime import classify_regime
from lihu_quantify.data.tushare_client import TushareClient
from lihu_quantify.config import get_settings as _gs

H = 4


def load():
    df = duckdb.query(f"SELECT * FROM '{ROOT / 'data' / 'weekly_panel_full.parquet'}'").fetchdf()
    df = df[(df["close"] > 0.5) & (df["amount"] > 0)]
    df = df.sort_values(["ts_code", "trade_date"]).reset_index(drop=True)
    _cfg = _gs()
    _raw = Path(_cfg.tushare.token_file).read_text(encoding="utf-8").strip()
    _m = re.search(r"token=([A-Za-z0-9]+)", _raw)
    _tok = _m.group(1) if _raw.startswith("{") else _raw
    _c = TushareClient(token=_tok, cache_dir=_cfg.tushare.cache_dir)
    idx = _c.query("index_daily", {"ts_code": "000001.SH", "start_date": "20190101", "end_date": "20260831"})
    idx["week"] = pd.to_datetime(idx["trade_date"], format="%Y%m%d").dt.to_period("W-FRI").dt.end_time.dt.date
    wk = idx.groupby("week", as_index=False).agg(trade_date=("trade_date", "max"), close=("close", "last"))
    return df, wk


def main() -> None:
    df, wk = load()
    g = df.groupby("ts_code", observed=True)
    # 流动性过滤：滚动4周均成交额 ≥ 5000万（amount 单位千元 → 5万千元）
    liq = g["amount"].transform(lambda s: s.rolling(4, min_periods=2).mean())
    mask = (liq >= 50000).fillna(False).to_numpy()
    sub = df[mask].reset_index(drop=True)
    logger.info(f"流动性过滤后：{len(sub)} 行 × {sub['ts_code'].nunique()} 只（全量 {df['ts_code'].nunique()}）")

    def cum_ret(s, n):
        return (1 + s.fillna(0)).rolling(n, min_periods=n).apply(lambda x: (1 + x).prod() - 1, raw=False)

    gs = sub.groupby("ts_code", observed=True)
    factors = {
        "wk_intraday_rev_liq": -gs["week_intraday"].transform(lambda s: s.rolling(4, min_periods=3).sum()),
        "rev_4w_liq": -gs["week_ret"].transform(lambda s: cum_ret(s, 4)),
        "rev_12w_liq": -gs["week_ret"].transform(lambda s: cum_ret(s, 12)),
    }
    regime = classify_regime(wk)
    out = {}
    for name, factor in factors.items():
        res = evaluate_factor(sub, factor, name=name, horizon=H, cost_bps=15.0,
                              n_trials=7, regime=regime)
        out[name] = res.as_dict()
        d = out[name]
        gates_fail = [k for k, v in d["gates"].items() if not v]
        logger.info(f"[{name}] RankIC={d['rank_ic_mean']:+.4f} ICIR={d['rank_ic_ir']:+.2f} "
                    f"t={d['rank_ic_t']:+.1f} 多空={d['long_short_ann']:+.2%} 单调ρ={d['group_monotonic']:+.2f} "
                    f"icDSR={d['ic_dsr']:.3f} → {'PASS' if d['pass_all'] else 'FAIL(' + ','.join(gates_fail) + ')'}")
    p = ROOT / "outputs" / "p7_r2_result.json"
    p.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    logger.info(f"→ {p}")


if __name__ == "__main__":
    main()
