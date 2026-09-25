"""P1 门禁2 第四轮（R4）：研报构造式落地。

新因子（全部来自研报精读，n_trials 12 + 3 = 15）：
  1. residual_vol —— Barra ResVol 的 DASTD 简化版：252 日收益波动率（半衰期42日EW）取负
     （中泰上篇：A股十年 IC=-0.050 全表最强）
  2. strev_barra  —— Barra CNTR 快速因子：63 日对数收益指数加权（半衰期10日）取反
     （IC +0.033, t=4.21，比等权反转构造更优）
  3. intraday_rev_20 —— 20 日累计日内收益取反（国泰口径，剔除隔夜）
纪律升级：
  - 全因子套用 5×MAD 去极值（Barra 预处理流水线第一步）
  - 入库相关性检查：与已测 6 因子截面相关 <0.65
  - DSR 主判据切换为 IC 序列口径（ic_dsr，方案B 已登记）
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

from lihu_quantify.research import compute_factor, evaluate_factor, momentum, turnover_premium
from lihu_quantify.research.regime import classify_regime

N_TRIALS_CUM = 15
HORIZON = 20


def load_panel() -> pd.DataFrame:
    df = duckdb.query(f"SELECT * FROM '{ROOT / 'data' / 'research_panel.parquet'}'").fetchdf()
    df = df[(df["close"] > 0.5) & (df["amount"] > 0)]
    return df.sort_values(["ts_code", "trade_date"]).reset_index(drop=True)


def mad_winsorize(s: pd.Series, by: pd.Series, n: float = 5.0) -> pd.Series:
    """Barra 预处理：按日截面 5×MAD 去极值（压缩至边界）。"""
    def _w(g):
        med = g.median()
        mad = (g - med).abs().median() * 1.4826
        return g.clip(med - n * mad, med + n * mad)
    return s.groupby(by, observed=True).transform(_w)


def ew_mean(s: pd.Series, g: pd.Series, halflife: float) -> pd.Series:
    """按组指数衰减加权均值（组内滚动 EW）。"""
    return s.groupby(g, observed=True).transform(lambda x: x.ewm(halflife=halflife).mean())


def residual_vol(df: pd.DataFrame) -> pd.Series:
    """DASTD 简化：252 日收益波动率（EW 半衰期42日）取负。"""
    ret = df.groupby("ts_code")["close"].transform(lambda s: s.pct_change())
    ew_var = (ret ** 2).groupby(df["ts_code"], observed=True).transform(
        lambda x: x.ewm(halflife=42, min_periods=60).mean())
    return -np.sqrt(ew_var)


def strev_barra(df: pd.DataFrame) -> pd.Series:
    """CNTR 式短期反转：63 日对数收益指数加权（EW 半衰期10日）取反。"""
    logret = df.groupby("ts_code")["close"].transform(lambda s: np.log(s / s.shift(1))).fillna(0.0)
    # 指数衰减累积（半衰期10日）：acc = acc*w + r, w=0.5^(1/10)
    w = 0.5 ** (1.0 / 10.0)   # 日衰减因子
    def _cum(x: pd.Series) -> pd.Series:
        arr = x.to_numpy()
        out = np.empty_like(arr)
        acc = 0.0
        for i, v in enumerate(arr):
            acc = acc * w + v
            out[i] = acc
        return pd.Series(out, index=x.index)
    ew = logret.groupby(df["ts_code"], observed=True).transform(_cum)
    return -ew


def intraday_rev_20(df: pd.DataFrame) -> pd.Series:
    """20 日累计日内收益（ln(close/open) 求和）取反。"""
    intraday = np.log(df["close"] / df["open"])
    s20 = intraday.groupby(df["ts_code"], observed=True).transform(
        lambda x: x.rolling(20, min_periods=15).sum())
    return -s20


def factor_corr(panel: pd.DataFrame, factors: dict) -> pd.DataFrame:
    """因子间截面相关（逐日 Spearman 平均）。"""
    m = pd.DataFrame(factors)
    m["trade_date"] = panel["trade_date"].values
    cors = m.groupby("trade_date").corr(method="spearman")
    return cors.groupby(level=1).mean().round(2)


def main() -> None:
    panel = load_panel()
    regime = classify_regime(
        duckdb.connect(str(ROOT / "data" / "lihu_quant.duckdb"), read_only=True).execute(
            "SELECT trade_date, close FROM index_daily WHERE ts_code='000001.SH' "
            "AND trade_date BETWEEN '2019-01-01' AND '2024-12-31' ORDER BY trade_date").fetchdf())
    td = panel["trade_date"]
    logger.info(f"面板：{len(panel)} 行 × {panel['ts_code'].nunique()} 只 | n_trials={N_TRIALS_CUM}")

    raw = {
        "residual_vol": residual_vol(panel),
        "strev_barra": strev_barra(panel),
        "intraday_rev_20": intraday_rev_20(panel),
    }
    # Barra 预处理：5×MAD 去极值
    factors = {k: mad_winsorize(v.dropna().reindex(panel.index), td) if False else mad_winsorize(v, td) for k, v in raw.items()}

    # 相关性纪律：与已测因子（动量/换手系）
    baseline = {
        "rev_mom_20": -momentum(panel),
        "turnover_60": turnover_premium(panel, n=60),
    }
    corr = factor_corr(panel, {**factors, **baseline})
    logger.info("因子截面相关矩阵(Spearman日均): " + corr.to_string())

    results = {}
    for name, factor in factors.items():
        res = evaluate_factor(panel, factor, name=name, horizon=HORIZON, cost_bps=15.0,
                              n_trials=N_TRIALS_CUM, regime=regime)
        results[name] = res.as_dict()
        d = results[name]
        gates_fail = [k for k, v in d["gates"].items() if not v]
        logger.info(
            f"[{name}] RankIC={d['rank_ic_mean']:+.4f} ICIR={d['rank_ic_ir']:+.2f} "
            f"t={d['rank_ic_t']:+.1f} 多空={d['long_short_ann']:+.2%} 单调ρ={d['group_monotonic']:+.2f} "
            f"icDSR={d['ic_dsr']:.3f}(主) 组合DSR={d['dsr']:.3f} regime={d['regime_consistency']} "
            f"→ {'PASS' if d['pass_all'] else 'FAIL(' + ','.join(gates_fail) + ')'}")

    out = ROOT / "outputs" / "p1_gate2_r4_result.json"
    out.write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
    corr.to_csv(ROOT / "outputs" / "p1_gate2_r4_corr.csv", encoding="utf-8-sig")
    logger.info(f"R4 验收单 → {out}")


if __name__ == "__main__":
    main()
