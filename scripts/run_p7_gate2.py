"""P7-2：低频（周K）因子门禁2——周频截面 IC 验收。

因子候选（全部纯量价，PIT 面板直接可算）：
  rev_4w       —— 4 周反转（IR20 的周频对应）
  rev_12w      —— 12 周反转
  mom_48w_1m   —— 48周动量剔除近4周（经典 12-1 月线构造；检验 A 股中长期动量）
  wk_intraday_rev —— 周内日内收益反转（周聚合的 ln(c/o) 和取负）
  wk_vol_ratio_rev — 周量比反转（本周量/前4周均量 取负：放量滞涨看空）
horizon 以周 bar 计：4 周（≈20 交易日，与 IR20 兑现周期对齐）。
n_trials P7: 5。
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

from lihu_quantify.research.acceptance import evaluate_factor
from lihu_quantify.research.regime import classify_regime

N_TRIALS = 5
H = 4   # 4 周持有


def load() -> pd.DataFrame:
    df = duckdb.query(f"SELECT * FROM '{ROOT / 'data' / 'weekly_panel.parquet'}'").fetchdf()
    df = df[(df["close"] > 0.5) & (df["amount"] > 0)]
    df = df.sort_values(["ts_code", "trade_date"]).reset_index(drop=True)
    # 指数周频（regime 用）——走 Tushare client 缓存，避开 DuckDB 写锁（扩池任务占用中）
    import re as _re
    from lihu_quantify.data.tushare_client import TushareClient
    from lihu_quantify.config import get_settings as _gs
    _cfg = _gs()
    _raw = Path(_cfg.tushare.token_file).read_text(encoding="utf-8").strip()
    _m = _re.search(r"token=([A-Za-z0-9]+)", _raw)
    _tok = _m.group(1) if _raw.startswith("{") else _raw
    _c = TushareClient(token=_tok, cache_dir=_cfg.tushare.cache_dir)
    idx = _c.query("index_daily", {"ts_code": "000001.SH", "start_date": "20190101", "end_date": "20260831"})
    idx["week"] = pd.to_datetime(idx["trade_date"]).dt.to_period("W-FRI").dt.end_time.dt.date
    wk = idx.groupby("week", as_index=False).agg(trade_date=("trade_date", "max"), close=("close", "last"))
    return df, wk


def roll_ret(week_ret: pd.Series, n: int, skip: int = 0) -> pd.Series:
    """n 周收益，跳过近 skip 周（12-1 构造用）。按股票分组。"""
    def _r(s):
        if skip:
            return (1 + s.shift(skip)).rolling(n, min_periods=n).apply(lambda x: (1 + x).prod() - 1, raw=False)
        return (1 + s).rolling(n, min_periods=n).apply(lambda x: (1 + x).prod() - 1, raw=False)
    return None


def main() -> None:
    df, wk = load()
    g = df.groupby("ts_code", observed=True)
    # 周收益滚动积
    def cum_ret(s, n, skip=0):
        if skip > 0:
            s2 = s.shift(skip)
        else:
            s2 = s
        return (1 + s2.fillna(0)).rolling(n, min_periods=n).apply(lambda x: (1 + x).prod() - 1, raw=False)

    td = df["trade_date"]
    factors = {
        "rev_4w": -g["week_ret"].transform(lambda s: cum_ret(s, 4)),
        "rev_12w": -g["week_ret"].transform(lambda s: cum_ret(s, 12)),
        "mom_48w_1m": g["week_ret"].transform(lambda s: cum_ret(s, 44, skip=4)),
        "wk_intraday_rev": -g["week_intraday"].transform(lambda s: s.rolling(4, min_periods=3).sum()),
        "wk_vol_ratio_rev": -(df["vol"] / g["vol"].transform(lambda s: s.rolling(4, min_periods=3).mean().shift(1))),
    }
    regime = classify_regime(wk)
    logger.info(f"周频面板：{len(df)} 行 × {df['ts_code'].nunique()} 只")
    out = {}
    for name, factor in factors.items():
        res = evaluate_factor(df, factor, name=name, horizon=H, cost_bps=15.0,
                              n_trials=N_TRIALS, regime=regime)
        out[name] = res.as_dict()
        d = out[name]
        gates_fail = [k for k, v in d["gates"].items() if not v]
        logger.info(f"[{name}] RankIC={d['rank_ic_mean']:+.4f} ICIR={d['rank_ic_ir']:+.2f} "
                    f"t={d['rank_ic_t']:+.1f} 多空={d['long_short_ann']:+.2%} 单调ρ={d['group_monotonic']:+.2f} "
                    f"icDSR={d['ic_dsr']:.3f} regime={d['regime_consistency']} "
                    f"→ {'PASS' if d['pass_all'] else 'FAIL(' + ','.join(gates_fail) + ')'}")
    p = ROOT / "outputs" / "p7_gate2_result.json"
    p.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    logger.info(f"→ {p}")


if __name__ == "__main__":
    main()
