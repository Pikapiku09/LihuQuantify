"""P1 门禁3：walkforward 样本外验证 + 月度滚动 IC + 参数敏感性 + 拥挤度。

对象：residual_vol（独立入库）与 intraday_rev_20（反转族代表）
数据：2019-01 ~ 2026-08 面板（2025 之后为纯样本外，从未参与任何门禁2 试验）
判定（docs/因子验收单.md 第三节）：
  G1 各折 IC 符号一致（train/valid/oos 三段）
  G2 参数 ±30% 内结论不变（敏感性检查，不挑参数、不计 n_trials）
  G3 子样本方向不反转（前后半段）
  G4 月度滚动 IC：近 12 个月均值不翻符号（regime 弱化监控）
  G5 拥挤度（简化）：多头组 60 日配对相关均值 < 0.65 且因子多空滚动波动无趋势抬升
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

from lihu_quantify.research import calc_ic, compute_factor, quantile_backtest

SEGMENTS = {
    "train(2019-2022)": ("2019-01-01", "2022-12-31"),
    "valid(2023-2024)": ("2023-01-01", "2024-12-31"),
    "oos(2025-2026.8)": ("2025-01-01", "2026-08-31"),
}


def load_panel() -> pd.DataFrame:
    df = duckdb.query(f"SELECT * FROM '{ROOT / 'data' / 'research_panel.parquet'}'").fetchdf()
    df = df[(df["close"] > 0.5) & (df["amount"] > 0)]
    return df.sort_values(["ts_code", "trade_date"]).reset_index(drop=True)


def seg_stats(panel: pd.DataFrame, factor: pd.Series, seg: str) -> dict:
    lo, hi = SEGMENTS[seg]
    m = (panel["trade_date"] >= lo) & (panel["trade_date"] <= hi)
    ics = calc_ic(panel[m], factor[m], horizon=20)
    ric = ics["rank_ic"].dropna()
    qb = quantile_backtest(panel[m], factor[m], n_groups=5, horizon=20, cost_bps=15.0)
    ls = qb["long_short"].dropna()
    ann = (ls.mean() * 252 / 20) if len(ls) else 0.0
    return {
        "n_days": int(len(ric)),
        "rank_ic": round(float(ric.mean()), 4) if len(ric) else None,
        "ic_ir": round(float(ric.mean() / ric.std()), 2) if len(ric) and ric.std() > 0 else None,
        "ic_t": round(float(ric.mean() / (ric.std() / np.sqrt(len(ric)))), 1) if len(ric) and ric.std() > 0 else None,
        "ls_ann": round(float(ann), 4),
    }


def monthly_ic(panel: pd.DataFrame, factor: pd.Series) -> pd.Series:
    ics = calc_ic(panel, factor, horizon=20)
    ric = ics["rank_ic"].dropna()
    idx = pd.to_datetime(ric.index)
    return pd.Series(ric.values, index=idx).resample("ME").mean()


def crowding(panel: pd.DataFrame, factor: pd.Series, window: int = 60) -> dict:
    """拥挤度简化：多头组（前20%分位）收益的 60 日配对相关均值 + 多空滚动波动。"""
    d = panel[["trade_date", "ts_code", "close"]].copy()
    d["_f"] = factor.values
    d["_r"] = d.groupby("ts_code")["close"].transform(lambda s: s.pct_change())
    # 取近一年多头组，计算两两相关均值（抽样 40 只控制算量）
    last = d["trade_date"].max()
    recent = d[d["trade_date"] >= str(pd.Timestamp(last) - pd.Timedelta(days=365))]
    top = recent.groupby("trade_date")["_f"].transform(lambda x: x.quantile(0.8))
    hold = recent[recent["_f"] >= top]["ts_code"].unique()
    sample = list(hold[: min(40, len(hold))])
    piv = recent[recent["ts_code"].isin(sample)].pivot_table(index="trade_date", columns="ts_code", values="_r")
    corr = piv.tail(window).corr().values
    iu = np.triu_indices_from(corr, 1)
    pair_corr = float(np.nanmean(corr[iu]))
    # 因子多空滚动波动趋势
    qb_ls = quantile_backtest(d.drop(columns=["_r"]), d["_f"], n_groups=5, horizon=20, cost_bps=15.0)["long_short"].dropna()
    roll_std = qb_ls.rolling(12).std().dropna()
    trend = float(roll_std.tail(12).mean() / roll_std.head(12).mean()) if len(roll_std) > 24 else 1.0
    return {"long_pair_corr": round(pair_corr, 3), "ls_vol_trend": round(trend, 2)}


def sensitivity(panel: pd.DataFrame, name: str, param: str, base: int, grid: list) -> dict:
    out = {}
    for v in grid:
        f = compute_factor(panel, name, **{param: v})
        ics = calc_ic(panel, f, horizon=20)
        ric = ics["rank_ic"].dropna()
        out[v] = round(float(ric.mean()), 4) if len(ric) else None
    return out


def main() -> None:
    panel = load_panel()
    logger.info(f"面板：{len(panel)} 行 × {panel['ts_code'].nunique()} 只 "
                f"({panel['trade_date'].min()} ~ {panel['trade_date'].max()})")

    report = {}
    for name, param, base, grid in (
        ("residual_vol", "halflife", 42, [30, 42, 60]),
        ("intraday_rev_20", "n", 20, [15, 20, 30]),
    ):
        factor = compute_factor(panel, name, **{param: base})
        r = {"segments": {}, "sensitivity": {}, "crowding": None, "gates": {}, "monthly_tail": None}
        for seg in SEGMENTS:
            r["segments"][seg] = seg_stats(panel, factor, seg)
        r["sensitivity"] = sensitivity(panel, name, param, base, grid)
        mi = monthly_ic(panel, factor)
        r["monthly_tail"] = round(float(mi.tail(12).mean()), 4)
        r["monthly_ic_series_tail"] = {str(k.date()): round(float(v), 4) for k, v in mi.tail(18).items()}
        r["crowding"] = crowding(panel, factor)

        segs = r["segments"]
        ics = [segs[s]["rank_ic"] for s in SEGMENTS if segs[s]["rank_ic"] is not None]
        r["gates"]["G1_段间符号一致"] = all(x > 0 for x in ics) or all(x < 0 for x in ics)
        sens = [v for v in r["sensitivity"].values() if v is not None]
        r["gates"]["G2_参数稳健"] = all(x > 0 for x in sens) or all(x < 0 for x in sens)
        half = monthly_halves(panel, factor)
        r["gates"]["G3_前后半段同号"] = half
        r["gates"]["G4_近12月IC同号"] = (r["monthly_tail"] is not None
                                        and np.sign(r["monthly_tail"]) == np.sign(segs["train(2019-2022)"]["rank_ic"]))
        cr = r["crowding"]
        r["gates"]["G5_不拥挤"] = (cr["long_pair_corr"] < 0.65 and cr["ls_vol_trend"] < 1.5)
        report[name] = r
        logger.info(f"[{name}] gates={r['gates']} | 段间IC={[segs[s]['rank_ic'] for s in SEGMENTS]} "
                    f"| 近12月IC={r['monthly_tail']} | 拥挤={cr}")

    out = ROOT / "outputs" / "p1_gate3_result.json"

    def _to_py(o):
        if isinstance(o, dict): return {k: _to_py(v) for k, v in o.items()}
        if isinstance(o, (list, tuple)): return [_to_py(v) for v in o]
        if isinstance(o, (np.bool_,)): return bool(o)
        if isinstance(o, (np.floating,)): return float(o)
        if isinstance(o, (np.integer,)): return int(o)
        return o

    out.write_text(json.dumps(_to_py(report), ensure_ascii=False, indent=2), encoding="utf-8")
    logger.info(f"门禁3 报告 → {out}")


def monthly_halves(panel: pd.DataFrame, factor: pd.Series) -> bool:
    mid = panel["trade_date"].quantile(0.5)
    m1 = (panel["trade_date"] <= mid).values
    m2 = (panel["trade_date"] > mid).values
    a = calc_ic(panel[m1], factor[m1], horizon=20)["rank_ic"].dropna()
    b = calc_ic(panel[m2], factor[m2], horizon=20)["rank_ic"].dropna()
    ma, mb = float(a.mean()) if len(a) else 0.0, float(b.mean()) if len(b) else 0.0
    return np.sign(ma) == np.sign(mb) and ma != 0


if __name__ == "__main__":
    main()
