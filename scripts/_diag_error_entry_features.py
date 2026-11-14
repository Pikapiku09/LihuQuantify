# -*- coding: utf-8 -*-
"""即错交易特征诊断：IR 系 292 笔买入里，「入场即错」那 88 笔有没有可辨识的共同特征。

一句话问题
----------
入场信号本身是不是废的？如果「即错」有稳定的、**买入日当天即可观测**的共同特征 →
信号可过滤、策略有救；如果 FDR 校正后一个都不显著、样本外也不复现 →
信号质量在这个形态下就是随机噪声，过滤无效。

口径（全部只读复用，不重写）
--------------------------
- 三分类：`scripts/_diag_entry_quality.py` 的 `build_a2` + `classify`（import 复用）
    · 入场即错     ：3 日内 MAE ≤ −3% 且 MFE < +1%
    · 入场对且顺利 ：轮次 PnL > 0
    · 入场对但被洗 ：持仓期内 MFE ≥ +3% 但轮次 PnL ≤ 0
    · 灰区         ：其余
- PnL：`lihu_quantify.backtest.metrics._pair_rounds`（经 base.attribute_rounds）
- 伪交易日 2026-09-12：`data.integrity.filter_non_trading_days` 剔除（指数 + 个股双查）
- 复权：按 `data.adjustment` 口径 price × adj / 同标的最新 adj，逐只处理；
  行情库只有部分标的带 adj_factors，覆盖率实测后如实披露

统计纪律
--------
1. 所有特征只用「买入日收盘及之前」的数据；`Part 3.2` 做**截断不变性自检**（把面板砍到
   买入日再算一遍，必须逐位相等）——这是防未来函数的硬断言。
2. 连续变量 Mann-Whitney U（双侧，渐近）+ Cliff's delta；分类变量卡方（2×2 且期望频数<5
   时另报 Fisher 精确检验）。
3. 全部特征一起做 Benjamini-Hochberg FDR 校正（一个家族统一校正，不挑着报）。
4. **日期聚类校正**：市场层特征在同一买入日内是常数，买入级 Mann-Whitney 属伪重复；
   `Part 4b` 用「日期层」重检验，并对个股特征做「日内配对符号检验」。
5. **波动率机械基准**：`Part 5` 用每只票自身 20 日波动率做零漂移随机游走模拟，看 30% 的
   即错率是不是「随机入场 + 高波动」的必然产物（即错定义含 −3% 触碰，天然与波动率相关）。
6. 样本内 2026-08-26~09-30 定特征，样本外 2026-10-01~11-10 验证（阈值只用样本内定）。
7. 任何「过滤后胜率」都给 Wilson 95% CI（两侧）+ 下界（复用 monitor.auto_block）。

用法
----
    $env:PYTHONIOENCODING="utf-8"
    .venv\\Scripts\\python.exe scripts/_diag_error_entry_features.py
"""

from __future__ import annotations

import bisect
import math
import re
import sys
from collections import defaultdict
from datetime import date
from pathlib import Path

import numpy as np
import pandas as pd
from scipy import stats

ROOT = Path(r"E:\LihuQuantify")
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts"))

from lihu_quantify.data.duckdb_store import DuckDBStore  # noqa: E402
from lihu_quantify.data.integrity import (  # noqa: E402
    filter_non_trading_days,
    load_trading_days,
)
from lihu_quantify.market import classify_market_state  # noqa: E402
from lihu_quantify.monitor.auto_block import wilson_lower_bound  # noqa: E402

import _diag_entry_quality as base  # noqa: E402  （只读复用三分类逻辑）

# ---------------------------------------------------------------- 配置
DB_PATH = ROOT / "data" / "lihu_quant.duckdb"
LEDGER_DIR = Path(r"E:\Dsh_WorkSapce\Dify_Agents\_tmp_mine")
LEDGERS = {
    "main": "paper_state.json",
    "ir20": "paper_state_ir20.json",
    "wbr25": "paper_state_wbr25.json",
}
IR_BOOKS = ["main", "ir20", "wbr25"]

INDEX_CODE = "000001.SH"
LOAD_START = date(2026, 1, 1)
LOAD_END = date(2026, 11, 10)
IS_START = date(2026, 8, 26)
IS_END = date(2026, 9, 30)
OOS_START = date(2026, 10, 1)
OOS_END = date(2026, 11, 10)

ERR = "入场即错"
SMOOTH = "入场对且顺利"
WASH = "入场对但被洗"
GRAY = "灰区（慢跌/弱信号）"

MIN_BARS = 60
TAG = "【实证】"
SEED = 20261114


def hr(t="", ch="=", w=104):
    print(f"\n{ch * w}\n{t}\n{ch * w}" if t else ch * w)


# ---------------------------------------------------------------- 统计小工具
def mwu(x, y):
    """Mann-Whitney U（双侧，渐近）+ Cliff's delta（>0 表示 x 组更大）。"""
    x = np.asarray(x, dtype=float)
    y = np.asarray(y, dtype=float)
    x = x[np.isfinite(x)]
    y = y[np.isfinite(y)]
    if len(x) < 5 or len(y) < 5:
        return None
    if np.nanstd(np.concatenate([x, y])) == 0:
        return None
    u = stats.mannwhitneyu(x, y, alternative="two-sided", method="asymptotic")
    delta = 2.0 * u.statistic / (len(x) * len(y)) - 1.0
    return {
        "n1": len(x), "n2": len(y), "p": float(u.pvalue), "delta": float(delta),
        "med1": float(np.median(x)), "med2": float(np.median(y)),
        "q1_1": float(np.percentile(x, 25)), "q3_1": float(np.percentile(x, 75)),
        "q1_2": float(np.percentile(y, 25)), "q3_2": float(np.percentile(y, 75)),
    }


def chi_test(cat, binary):
    tab = pd.crosstab(pd.Series(np.asarray(cat, dtype=object)),
                      pd.Series(np.asarray(binary, dtype=object)))
    tab = tab.loc[tab.sum(axis=1) >= 5]
    if tab.shape[0] < 2 or tab.shape[1] < 2:
        return None
    chi2, p, dof, exp = stats.chi2_contingency(tab, correction=False)
    v = math.sqrt(chi2 / (tab.values.sum() * (min(tab.shape) - 1)))
    out = {"p": float(p), "chi2": float(chi2), "v": float(v), "tab": tab,
           "min_exp": float(exp.min()), "fisher_p": None}
    if tab.shape == (2, 2) and exp.min() < 5:
        out["fisher_p"] = float(stats.fisher_exact(tab.values)[1])
    return out


def bh_fdr(pvals) -> np.ndarray:
    """Benjamini-Hochberg q 值（保序）。"""
    p = np.asarray(pvals, dtype=float)
    q = np.full(p.shape, np.nan)
    idx = np.where(np.isfinite(p))[0]
    m = len(idx)
    if m == 0:
        return q
    order = idx[np.argsort(p[idx])]
    ranked = p[order] * m / np.arange(1, m + 1)
    ranked = np.minimum.accumulate(ranked[::-1])[::-1]
    q[order] = np.clip(ranked, 0, 1)
    return q


def wilson_ci(k: int, n: int, z: float = 1.96):
    if n == 0:
        return (float("nan"), float("nan"))
    p = k / n
    d = 1 + z * z / n
    c = p + z * z / (2 * n)
    m = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n))
    return ((c - m) / d, (c + m) / d)


def fmt_pct(v, nd=1):
    return " n/a" if v is None or not np.isfinite(v) else f"{v * 100:.{nd}f}%"


def mde_cliff(n1: int, n2: int, power: float = 0.80) -> float:
    """80% 功效下双侧 α=0.05 可检出的最小 Cliff's delta（正态近似）。"""
    se = math.sqrt((n1 + n2 + 1) / (3.0 * n1 * n2))
    return (stats.norm.ppf(0.975) + stats.norm.ppf(power)) * se


# ---------------------------------------------------------------- 行情装载
def build_panels(store):
    hr("Part 1  行情装载（伪交易日剔除 / 前复权 / 匹配率硬断言）")
    tds = load_trading_days(store)
    print(f"{TAG} trade_cal 开市日 {len(tds)} 天 ({min(tds)} ~ {max(tds)})")

    idx = store.query(
        "SELECT trade_date, close FROM index_daily WHERE ts_code = ? "
        "AND trade_date BETWEEN ? AND ? ORDER BY trade_date",
        [INDEX_CODE, LOAD_START, LOAD_END])
    n0 = len(idx)
    idx, drop_i = filter_non_trading_days(idx, tds)
    print(f"{TAG} {INDEX_CODE}: {n0} 行 → 剔除伪交易日 {len(drop_i)} 条 "
          f"{[str(d) for d in drop_i]} → {len(idx)} 行")
    idx["trade_date"] = pd.to_datetime(idx["trade_date"]).dt.date
    idx = idx.sort_values("trade_date").reset_index(drop=True)
    axis = idx["trade_date"].tolist()
    idx_c = idx["close"].astype(float).to_numpy()

    q = store.query(
        "SELECT ts_code, trade_date, open, high, low, close, pre_close, vol, amount "
        "FROM daily_quotes WHERE trade_date BETWEEN ? AND ? ORDER BY ts_code, trade_date",
        [LOAD_START, LOAD_END])
    n_q0 = len(q)
    q, drop_q = filter_non_trading_days(q, tds)
    print(f"{TAG} daily_quotes: {n_q0} 行 → 剔除伪交易日 {len(drop_q)} 条 → {len(q)} 行")
    q["trade_date"] = pd.to_datetime(q["trade_date"]).dt.date

    adj = store.query(
        "SELECT ts_code, trade_date, adj_factor FROM adj_factors "
        "WHERE trade_date BETWEEN ? AND ? ORDER BY ts_code, trade_date",
        [LOAD_START, LOAD_END])
    adj["trade_date"] = pd.to_datetime(adj["trade_date"]).dt.date
    print(f"{TAG} adj_factors: {len(adj)} 行 / {adj['ts_code'].nunique()} 只")

    m = q.merge(adj, on=["ts_code", "trade_date"], how="left")
    anchor = m.dropna(subset=["adj_factor"]).groupby("ts_code")["adj_factor"].last()
    m["adj_anchor"] = m["ts_code"].map(anchor)
    ratio = m["adj_factor"] / m["adj_anchor"]
    m["ratio"] = ratio.where(np.isfinite(ratio), 1.0)
    for c in ("open", "high", "low", "close", "pre_close"):
        m[c] = m[c].astype(float) * m["ratio"]
    n_adj_code = int(m.loc[m["adj_factor"].notna(), "ts_code"].nunique())
    print(f"{TAG} 前复权：{n_adj_code} 只标的有复权因子（其余按不复权，如实披露）")

    def piv(src, col):
        p = src.pivot_table(index="ts_code", columns="trade_date", values=col, aggfunc="last")
        return p.reindex(columns=axis)

    per_code = {code: sub.sort_values("trade_date").reset_index(drop=True)
                for code, sub in m.groupby("ts_code", sort=False)}

    idx_df = idx[["trade_date", "close"]].copy()
    state_map = classify_market_state(idx_df, window=20)
    print(f"{TAG} 指数状态：{len(state_map)} 天 → "
          f"{dict(pd.Series(list(state_map.values())).value_counts())}")

    return {
        "axis": axis, "tds": tds, "idx_dates": axis, "idx_c": idx_c,
        "state_map": state_map, "per_code": per_code,
        "hi": piv(q, "high"), "lo": piv(q, "low"), "px": piv(q, "close"),
        "hi_adj": piv(m, "high"), "lo_adj": piv(m, "low"), "px_adj": piv(m, "close"),
        "n_adj_code": n_adj_code, "n_drop_i": len(drop_i), "n_drop_q": len(drop_q),
    }


# ---------------------------------------------------------------- 特征登记
FEATURES = []


def _reg(key, name, kind, family):
    FEATURES.append((key, name, kind, family))


# --- 市场层（同一买入日内是常数 → 买入级检验属伪重复，Part 4b 单独校正）---
_reg("m_ret5", "指数近5日涨幅", "cont", "市场层")
_reg("m_ret10", "指数近10日涨幅", "cont", "市场层")
_reg("m_ret20", "指数近20日涨幅", "cont", "市场层")
_reg("m_dist_ma20", "指数偏离20日均线", "cont", "市场层")
_reg("m_ma5_ma20", "指数MA5/MA20−1", "cont", "市场层")
_reg("m_vol20", "指数20日波动率", "cont", "市场层")
_reg("m_dd60", "指数距60日高点", "cont", "市场层")
_reg("m_pct_today", "指数当日涨跌", "cont", "市场层")
_reg("m_state", "指数趋势状态", "cat", "市场层")
# --- 个股动量/位置 ---
_reg("s_ret5", "个股近5日涨幅", "cont", "个股动量")
_reg("s_ret10", "个股近10日涨幅", "cont", "个股动量")
_reg("s_ret20", "个股近20日涨幅", "cont", "个股动量")
_reg("s_dist_ma20", "个股偏离20日均线", "cont", "个股动量")
_reg("s_dist_low20", "个股距20日低点", "cont", "个股动量")
# --- 波动 ---
_reg("s_vol20", "个股20日收益率标准差", "cont", "波动")
_reg("s_atr14", "个股ATR14/收盘", "cont", "波动")
_reg("s_bbwidth", "个股布林带宽度", "cont", "波动")
_reg("s_amp_today", "个股当日振幅", "cont", "波动")
# --- 量能 ---
_reg("s_volratio", "当日量/前20日均量", "cont", "量能")
# --- 技术形态 ---
_reg("s_rsi14", "个股RSI14", "cont", "技术形态")
_reg("s_macd_hist", "个股MACD柱/收盘", "cont", "技术形态")
_reg("s_ma_stack", "均线排列", "cat", "技术形态")
_reg("s_close_pos", "收盘价在当日振幅中的位置", "cont", "技术形态")
_reg("s_gap", "当日跳空幅度", "cont", "技术形态")
_reg("s_limitup20", "近20日涨停次数", "cont", "技术形态")
_reg("s_is_limitup", "当日是否涨停", "cat", "技术形态")
# --- 信号来源/强度 ---
_reg("sig_score", "信号分位值(越小越极端超卖)", "cont", "信号强度")
_reg("sig_source", "信号来源", "cat", "信号强度")
# --- 组合层 ---
_reg("open_positions", "买入时该账本持仓数", "cont", "组合层")
_reg("same_day_buys", "该账本当日买入笔数", "cont", "组合层")
_reg("days_since_prev_buy", "距该账本上一笔买入天数", "cont", "组合层")
# --- 行业 ---
_reg("industry", "所属行业(top8+其他)", "cat", "行业")
# --- 时间 ---
_reg("weekday", "星期几", "cat", "时间")

KIND_OF = {k: kd for k, _, kd, _ in FEATURES}
NAME_OF = {k: nm for k, nm, _, _ in FEATURES}
DATE_CONST = {"m_ret5", "m_ret10", "m_ret20", "m_dist_ma20", "m_ma5_ma20",
              "m_vol20", "m_dd60", "m_pct_today", "m_state"}
MARKET_NUM = ["m_ret5", "m_ret10", "m_ret20", "m_dist_ma20", "m_ma5_ma20",
              "m_vol20", "m_dd60", "m_pct_today"]
# 不可检验的特征（行情深度不足）：个股 60 日类只有 38/292 笔算得出
NOT_TESTABLE = {
    "s_dist_ma60": "行情库对 427 只只回填到 2026-08，60 根 K 线只有 38/292 笔具备",
    "s_dist_high60": "同上",
}
# 个股 60 日特征：计算但不入主检验家族（样本 38 笔，δ 需 >0.5 才能检出，无意义）


# ---------------------------------------------------------------- 特征计算
def _ema(a: np.ndarray, span: int) -> np.ndarray:
    alpha = 2.0 / (span + 1.0)
    out = np.empty_like(a, dtype=float)
    out[0] = a[0]
    for i in range(1, len(a)):
        out[i] = alpha * a[i] + (1 - alpha) * out[i - 1]
    return out


def _rsi(c: np.ndarray, n: int = 14) -> float:
    d = np.diff(c[-(n + 1):])
    up = np.where(d > 0, d, 0.0)
    dn = np.where(d < 0, -d, 0.0)
    au, ad = up.mean(), dn.mean()
    if ad == 0:
        return 100.0 if au > 0 else 50.0
    return float(100.0 - 100.0 / (1 + au / ad))


def stock_feats(code_df: pd.DataFrame, i0: int, j0: int, panels) -> dict:
    """只用 code_df.iloc[: i0+1] 与指数 iloc[: j0+1]，构造买入日收盘可观测特征。"""
    o = code_df["open"].to_numpy(dtype=float)[: i0 + 1]
    h = code_df["high"].to_numpy(dtype=float)[: i0 + 1]
    lo = code_df["low"].to_numpy(dtype=float)[: i0 + 1]
    c = code_df["close"].to_numpy(dtype=float)[: i0 + 1]
    v = code_df["vol"].to_numpy(dtype=float)[: i0 + 1]
    n = len(c)
    f = {}

    def ret(k):
        return c[-1] / c[-1 - k] - 1.0 if n >= k + 2 and c[-1 - k] > 0 else np.nan

    f["s_ret5"], f["s_ret10"], f["s_ret20"] = ret(5), ret(10), ret(20)
    ma20 = c[-20:].mean() if n >= 20 else np.nan
    f["s_dist_ma20"] = c[-1] / ma20 - 1.0 if np.isfinite(ma20) and ma20 > 0 else np.nan
    f["s_dist_low20"] = c[-1] / lo[-20:].min() - 1.0 if n >= 20 and lo[-20:].min() > 0 else np.nan
    if n >= 60 and h[-60:].max() > 0:
        f["s_dist_high60"] = c[-1] / h[-60:].max() - 1.0
        f["s_dist_ma60"] = c[-1] / c[-60:].mean() - 1.0 if c[-60:].mean() > 0 else np.nan
    else:
        f["s_dist_high60"] = f["s_dist_ma60"] = np.nan

    r = c[1:] / c[:-1] - 1.0
    r = r[np.isfinite(r)]
    f["s_vol20"] = float(np.std(r[-20:], ddof=1)) if len(r) >= 21 else np.nan
    tr = np.maximum.reduce([h[1:] - lo[1:], np.abs(h[1:] - c[:-1]),
                            np.abs(lo[1:] - c[:-1])])
    f["s_atr14"] = float(np.mean(tr[-14:]) / c[-1]) if len(tr) >= 14 and c[-1] > 0 else np.nan
    f["s_bbwidth"] = (4.0 * float(np.std(c[-20:], ddof=1)) / ma20
                      if np.isfinite(ma20) and ma20 > 0 else np.nan)
    f["s_amp_today"] = (h[-1] - lo[-1]) / c[-2] if n >= 2 and c[-2] > 0 else np.nan
    f["s_volratio"] = (v[-1] / np.mean(v[-21:-1])
                       if n >= 22 and np.mean(v[-21:-1]) > 0 else np.nan)
    f["s_rsi14"] = _rsi(c) if n >= 16 else np.nan

    dif = _ema(c, 12) - _ema(c, 26)
    dea = _ema(dif, 9)
    f["s_macd_hist"] = float((dif[-1] - dea[-1]) / c[-1]) if n >= 35 and c[-1] > 0 else np.nan

    ma5 = c[-5:].mean() if n >= 5 else np.nan
    ma10 = c[-10:].mean() if n >= 10 else np.nan
    if not (np.isfinite(ma5) and np.isfinite(ma10) and np.isfinite(ma20)):
        f["s_ma_stack"] = "n/a"
    elif ma5 > ma10 > ma20:
        f["s_ma_stack"] = "多头排列"
    elif ma5 < ma10 < ma20:
        f["s_ma_stack"] = "空头排列"
    else:
        f["s_ma_stack"] = "缠绕"

    rng = h[-1] - lo[-1]
    f["s_close_pos"] = (c[-1] - lo[-1]) / rng if rng > 0 else np.nan
    f["s_gap"] = o[-1] / c[-2] - 1.0 if n >= 2 and c[-2] > 0 else np.nan
    lim = 0.095
    f["s_limitup20"] = float(np.sum(r[-20:] >= lim)) if len(r) >= 20 else np.nan
    f["s_is_limitup"] = "涨停" if (n >= 2 and c[-1] / c[-2] - 1.0 >= lim) else "非涨停"

    # --- 市场层 ---
    ic = panels["idx_c"][: j0 + 1]
    ni = len(ic)
    f["m_ret5"] = ic[-1] / ic[-6] - 1.0 if ni >= 6 else np.nan
    f["m_ret10"] = ic[-1] / ic[-11] - 1.0 if ni >= 11 else np.nan
    f["m_ret20"] = ic[-1] / ic[-21] - 1.0 if ni >= 21 else np.nan
    ima20 = ic[-20:].mean() if ni >= 20 else np.nan
    ima5 = ic[-5:].mean() if ni >= 5 else np.nan
    f["m_dist_ma20"] = ic[-1] / ima20 - 1.0 if np.isfinite(ima20) and ima20 > 0 else np.nan
    f["m_ma5_ma20"] = (ima5 / ima20 - 1.0
                       if np.isfinite(ima5) and np.isfinite(ima20) and ima20 > 0 else np.nan)
    ir = np.diff(ic) / ic[:-1]
    ir = ir[np.isfinite(ir)]
    f["m_vol20"] = float(np.std(ir[-20:], ddof=1)) if len(ir) >= 21 else np.nan
    f["m_dd60"] = ic[-1] / ic[-60:].max() - 1.0 if ni >= 60 else np.nan
    f["m_pct_today"] = ic[-1] / ic[-2] - 1.0 if ni >= 2 else np.nan
    f["m_state"] = panels["state_map"].get(panels["idx_dates"][j0], "未知")
    return f


def sig_parse(reason: str):
    r = reason or ""
    m = re.search(r"分位=([0-9.]+)", r)
    score = float(m.group(1)) if m else np.nan
    if "日内反转" in r:
        src = "IR"
    elif "周频波段" in r:
        src = "WBR"
    elif "三层过滤" in r:
        src = "CC"
    elif "策略买入" in r:
        src = "LEGACY"
    else:
        src = "OTHER"
    return score, src


def build_features(d: pd.DataFrame, books, panels, ind_map=None) -> pd.DataFrame:
    hr("Part 3  特征构建（买入日收盘及之前，逐笔硬切片）")
    per_code = panels["per_code"]
    idx_dates = panels["idx_dates"]
    book_of = {bk["name"]: bk for bk in books}

    book_state = {}
    for name, bk in book_of.items():
        recs = bk["records"]
        order = sorted(range(len(recs)), key=lambda i: (recs[i].trade_date, i))
        pos, prev_buy_date, day_cnt, state = 0, None, defaultdict(int), {}
        for i in order:
            r = recs[i]
            if r.side == "buy":
                day_cnt[r.trade_date] += 1
                state[i] = {
                    "open_positions": pos,
                    "same_day_buys": day_cnt[r.trade_date],
                    "days_since_prev_buy": (np.nan if prev_buy_date is None
                                            else float((r.trade_date - prev_buy_date).days)),
                }
                pos += 1
                prev_buy_date = r.trade_date
            else:
                pos = max(0, pos - 1)
        book_state[name] = state

    rows = []
    for _, row in d.iterrows():
        rec = book_of[row["ledger"]]["records"][int(row["buy_idx"])]
        code, bdate = row["ts_code"], row["buy_date"]
        code_df = per_code.get(code)
        if code_df is None:
            raise AssertionError(f"{code} 无行情")
        dates = code_df["trade_date"].tolist()
        i0 = bisect.bisect_right(dates, bdate) - 1
        assert i0 >= 0 and dates[i0] == bdate, f"{code} {bdate} 无当日K线"
        j0 = bisect.bisect_right(idx_dates, bdate) - 1
        assert j0 >= 0 and idx_dates[j0] == bdate, f"{bdate} 不在指数交易日轴"

        f = stock_feats(code_df, i0, j0, panels)
        f["n_bars"] = i0 + 1
        score, src = sig_parse(rec.reason)
        f["sig_score"], f["sig_source"] = score, src
        f.update(book_state[row["ledger"]][int(row["buy_idx"])])
        f["weekday"] = int(bdate.weekday())
        f["industry"] = (ind_map or {}).get(code, "其他")
        f["has_turnover"] = False
        f["turnover_rate"] = np.nan
        f["log_mv"] = np.nan
        f.update({
            "ledger": row["ledger"], "ts_code": code, "buy_date": bdate,
            "buy_price": row["buy_price"], "round_pnl": row["round_pnl"],
            "cls": row["cls"], "mfe3": row["mfe3"], "mae3": row["mae3"],
            "mfe_hold": row.get("mfe_hold", np.nan),
        })
        rows.append(f)

    df = pd.DataFrame(rows)
    df["bd"] = pd.to_datetime(df["buy_date"])
    nb = df["n_bars"]
    print(f"{TAG} 特征行 {len(df)}；可用 K 线根数 min/中位/max = "
          f"{int(nb.min())}/{int(nb.median())}/{int(nb.max())}；≥60 根仅 {int((nb >= 60).sum())} 笔")
    print(f"{TAG} 信号来源 {dict(df['sig_source'].value_counts())}")
    print(f"{TAG} 信号分位值可用 {int(df['sig_score'].notna().sum())} 笔（IR/WBR 才有）")
    print(f"{TAG} 指数状态 {dict(df['m_state'].value_counts())}")
    nn = df[[k for k in NAME_OF if k in df.columns]].isna().sum()
    nn = nn[nn > 0]
    if len(nn):
        print(f"{TAG} 含缺失值特征：{ {k: int(v) for k, v in nn.items()} }")
    print(f"{TAG} 不可检验（行情深度不足，已移出主检验家族）："
          f"{ {k: v for k, v in NOT_TESTABLE.items()} }")
    return df


def attach_subsample(df: pd.DataFrame, store) -> pd.DataFrame:
    b = store.query(
        "SELECT ts_code, trade_date, turnover_rate, total_mv FROM daily_basic "
        "WHERE trade_date BETWEEN ? AND ?", [LOAD_START, LOAD_END])
    if b.empty:
        return df
    b["trade_date"] = pd.to_datetime(b["trade_date"]).dt.date
    bm = b.set_index(["ts_code", "trade_date"])
    turnover = {k: float(bm.loc[k, "turnover_rate"]) for k in bm.index}
    mv = {k: float(bm.loc[k, "total_mv"]) for k in bm.index}
    hit = 0
    for i, row in df.iterrows():
        key = (row["ts_code"], row["buy_date"])
        if key in turnover:
            df.at[i, "turnover_rate"] = turnover[key]
            df.at[i, "log_mv"] = math.log(mv[key]) if mv[key] > 0 else np.nan
            df.at[i, "has_turnover"] = True
            hit += 1
    print(f"{TAG} daily_basic 同日可得 {hit} 笔（{hit / len(df):.1%}）→ 换手率/市值仅子样本登记")
    return df


# ---------------------------------------------------------------- 组间检验
def run_tests(df: pd.DataFrame, tag: str, feats=None, quiet=False) -> pd.DataFrame:
    a = df[df["cls"] == ERR]
    b = df[df["cls"] == SMOOTH]
    rows = []
    for key, name, kind, family in (feats or FEATURES):
        if key not in df.columns:
            continue
        if kind == "cont":
            res = mwu(pd.to_numeric(a[key], errors="coerce").to_numpy(float),
                      pd.to_numeric(b[key], errors="coerce").to_numpy(float))
            if res is None:
                continue
            rows.append({
                "feature": key, "name": name, "kind": "连续", "family": family,
                "n_err": res["n1"], "n_ok": res["n2"], "p": res["p"],
                "effect": res["delta"], "effect_name": "Cliff's δ",
                "err_med": res["med1"], "ok_med": res["med2"],
                "detail": (f"即错 IQR=[{res['q1_1']:.4g},{res['q3_1']:.4g}] "
                           f"顺利 IQR=[{res['q1_2']:.4g},{res['q3_2']:.4g}]"),
            })
        else:
            x = a[key].astype(str).to_numpy()
            y = b[key].astype(str).to_numpy()
            res = chi_test(np.concatenate([x, y]),
                           np.array([ERR] * len(x) + [SMOOTH] * len(y), dtype=object))
            if res is None:
                continue
            p_use, note = res["p"], ""
            if res["fisher_p"] is not None:
                p_use = res["fisher_p"]
                note = f"；2×2 期望<5 → Fisher p={res['fisher_p']:.4f}"
            tab = res["tab"]
            rate = []
            for catv in tab.index:
                k = int(tab.loc[catv].get(ERR, 0))
                n = int(tab.loc[catv].sum())
                rate.append(f"{catv} {k}/{n}={k / n:.0%}")
            rows.append({
                "feature": key, "name": name, "kind": "分类", "family": family,
                "n_err": len(x), "n_ok": len(y), "p": p_use,
                "effect": res["v"], "effect_name": "Cramér's V",
                "err_med": np.nan, "ok_med": np.nan,
                "detail": "即错率 " + " | ".join(rate) + note,
            })
    out = pd.DataFrame(rows)
    if out.empty:
        return out
    out["q"] = bh_fdr(out["p"].to_numpy())
    out = out.sort_values(["q", "p"]).reset_index(drop=True)
    if quiet:
        return out
    print(f"\n{'-' * 108}\n{tag}：即错(n={len(a)}) vs 顺利(n={len(b)})，{len(out)} 个特征，"
          f"BH-FDR 校正（m={int(np.isfinite(out['p']).sum())}）\n{'-' * 108}")
    print(f"{'特征':<24}{'家族':<8}{'n_err':>6}{'n_ok':>6}{'p':>9}{'q(FDR)':>9}{'效应量':>9}  "
          f"{'即错中位':>10}{'顺利中位':>10}  判定")
    for _, r in out.iterrows():
        verdict = "**FDR显著**" if r["q"] < 0.05 else ("名义p<.05" if r["p"] < 0.05 else "")
        mark = "†" if r["feature"] in DATE_CONST else " "
        em = "n/a" if not np.isfinite(r["err_med"]) else f"{r['err_med']:.4g}"
        om = "n/a" if not np.isfinite(r["ok_med"]) else f"{r['ok_med']:.4g}"
        print(f"{mark}{r['name']:<23}{r['family']:<8}{r['n_err']:>6}{r['n_ok']:>6}"
              f"{r['p']:>9.4f}{r['q']:>9.4f}{r['effect']:>9.3f}  {em:>10}{om:>10}  {verdict}")
        if r["kind"] == "分类":
            print(f"    └{r['detail']}")
    print("† = 市场层特征（同一买入日内取值相同）→ 买入级 p 值属伪重复，见 Part 4b 日期层校正")
    return out


# ---------------------------------------------------------------- 泄漏自检
def leak_check(df: pd.DataFrame, panels, n_sample: int = 40, seed: int = 7) -> int:
    print(f"\n{TAG} 防未来函数自检：{n_sample} 笔随机买入，把面板砍到买入日重算，逐位比对")
    per_code = panels["per_code"]
    idx_dates = panels["idx_dates"]
    rng = np.random.default_rng(seed)
    idxs = rng.choice(len(df), size=min(n_sample, len(df)), replace=False)
    bad = 0
    for i in idxs:
        row = df.iloc[int(i)]
        code, bdate = row["ts_code"], row["buy_date"]
        code_df = per_code[code]
        dates = code_df["trade_date"].tolist()
        i0 = bisect.bisect_right(dates, bdate) - 1
        j0 = bisect.bisect_right(idx_dates, bdate) - 1
        full = stock_feats(code_df, i0, j0, panels)
        cut = code_df.iloc[: i0 + 1].reset_index(drop=True)
        cut_panels = {"idx_c": panels["idx_c"][: j0 + 1],
                      "idx_dates": idx_dates[: j0 + 1],
                      "state_map": panels["state_map"]}
        trunc = stock_feats(cut, i0, j0, cut_panels)
        for k, v in full.items():
            t = trunc[k]
            ok = (v == t) if isinstance(v, str) else (
                (np.isnan(v) and np.isnan(t)) or np.isclose(v, t, rtol=0, atol=0))
            if not ok:
                bad += 1
                print(f"    !! 泄漏 {code} {bdate} {k}: 全量={v} 截断={t}")
    print(f"    结果：不一致 {bad} 项 → {'通过' if bad == 0 else '失败'}")
    assert bad == 0, "特征使用了未来数据，禁止出结论"
    return bad


# ---------------------------------------------------------------- 过滤评估
def eval_filter(df: pd.DataFrame, key: str, keep_mask, tag: str, quiet=False) -> dict:
    keep = pd.Series(np.asarray(keep_mask, dtype=bool), index=df.index).fillna(False)
    sub = df[keep]
    n = len(sub)
    n_err = int((sub["cls"] == ERR).sum())
    n_ok = int((sub["cls"] == SMOOTH).sum())
    wins = int((sub["round_pnl"] > 0).sum())
    lo, hi = wilson_ci(wins, n)
    out = {
        "tag": tag, "feature": key, "n": n, "n_err": n_err, "n_ok": n_ok,
        "err_rate": n_err / n if n else np.nan,
        "win_rate": wins / n if n else np.nan,
        "win_ci": (lo, hi), "win_lower": wilson_lower_bound(wins, n),
        "ev": float(sub["round_pnl"].mean()) if n else np.nan,
        "total_pnl": float(sub["round_pnl"].sum()) if n else np.nan,
    }
    if not quiet:
        print(f"  [{tag}] 保留 {n:3d}/{len(df):3d}（{n / len(df):.0%}）| 即错 {n_err:2d} "
              f"{fmt_pct(out['err_rate'])} | 顺利 {n_ok:2d} 胜率 {fmt_pct(out['win_rate'])} "
              f"CI[{fmt_pct(lo)},{fmt_pct(hi)}] 下界 {fmt_pct(out['win_lower'])} | "
              f"EV {out['ev']:+.2f} 元 合计 {out['total_pnl']:+.0f}")
    return out


# ---------------------------------------------------------------- main
def main():
    hr("即错交易特征诊断   |   LihuQuantify   |   本地数据，禁止联网")

    # ---------- Part 0 ----------
    hr("Part 0  账本装载与结构校验（复用 _diag_entry_quality）")
    books_all = {}
    for nm, fn in LEDGERS.items():
        bk = base.load_ledger(nm, fn)
        bk["records"] = base.to_records(bk["raw"])
        books_all[nm] = bk
        cash = bk["init_capital"]
        for r in bk["records"]:
            amt = r.price * r.volume
            cash += (-amt - r.commission) if r.side == "buy" else (
                amt - r.commission - r.stamp_tax)
        snap = bk["asset_snapshot"]["cash"]
        ok = abs(cash - snap) < 1e-6
        nb = sum(1 for r in bk["records"] if r.side == "buy")
        ns = sum(1 for r in bk["records"] if r.side == "sell")
        print(f"  {nm:<6} buy={nb:3d} sell={ns:3d} "
              f"{min(r.trade_date for r in bk['records'])}~"
              f"{max(r.trade_date for r in bk['records'])} "
              f"现金重算 {cash:11.2f} vs 快照 {snap:11.2f} {'OK' if ok else '不一致 <<<'}")
        assert ok, f"{nm} 现金重算与快照不一致"
    books = [books_all[n] for n in IR_BOOKS]
    n_buy = sum(1 for bk in books for r in bk["records"] if r.side == "buy")
    print(f"{TAG} IR 系（main+ir20+wbr25）买入 {n_buy} 笔（任务书口径 292）")
    assert n_buy == 292, f"买入笔数 {n_buy} != 292"

    store = DuckDBStore(DB_PATH, read_only=True)
    try:
        panels = build_panels(store)
        codes = {r.ts_code for bk in books for r in bk["records"]}
        have = set(panels["per_code"].keys())
        rate = len(codes & have) / len(codes)
        print(f"{TAG} 成交涉及 {len(codes)} 只，行情命中 {len(codes & have)} 只 → 匹配率 {rate:.2%}")
        assert rate >= 0.99, "行情匹配率不足，禁止出结论"

        # ---------- Part 2 ----------
        hr("Part 2  三分类复现（直接调用 _diag_entry_quality.build_a2 + classify）")
        d = base.build_a2(books, panels)
        d["cls"] = base.classify(d)
        cnt = d["cls"].value_counts()
        print(f"{TAG} 买入 {len(d)} 笔（行情轴 {LOAD_START}~{LOAD_END}；前向 excursion 与前置报告同口径）")
        for c in base.CLASSES:
            print(f"  {c:<18}{int(cnt.get(c, 0)):>4} 笔 {cnt.get(c, 0) / len(d) * 100:>6.2f}%")
        assert len(d) == 292
        assert int(cnt.get(ERR, 0)) == 88, f"即错 {int(cnt.get(ERR, 0))} != 88"
        assert int(cnt.get(SMOOTH, 0)) == 69, f"顺利 {int(cnt.get(SMOOTH, 0))} != 69"
        assert int(cnt.get(WASH, 0)) == 47, f"被洗 {int(cnt.get(WASH, 0))} != 47"
        print(f"{TAG} 三分类与前置报告逐位一致（88/47/69/88）→ 口径对齐 ✅")

        sell_of = {}
        for bk in books:
            _, per_buy = base.attribute_rounds(bk["records"])
            for bi, info in per_buy.items():
                sell_of[(bk["name"], bi)] = info["sell_date"]
        mkt_adj = {"axis": panels["axis"], "hi": panels["hi_adj"], "lo": panels["lo_adj"]}
        d_adj = d.copy()
        n_adj_price = 0
        for i, r in d_adj.iterrows():
            code, bdate, bp = r["ts_code"], r["buy_date"], r["buy_price"]
            cdf = panels["per_code"][code]
            rat = float(cdf.loc[cdf["trade_date"] == bdate, "ratio"].iloc[0])
            if abs(rat - 1.0) > 1e-9:
                n_adj_price += 1
            abp = bp * rat
            ex = base.excursions(mkt_adj, code, bdate, abp)
            if ex is None:
                continue
            d_adj.at[i, "mfe3"], d_adj.at[i, "mae3"] = ex[3][0], ex[3][1]
            sd = sell_of.get((r["ledger"], int(r["buy_idx"])))
            if sd is not None:
                hm, _, _ = base.hold_excursion(mkt_adj, code, bdate, sd, abp)
                d_adj.at[i, "mfe_hold"] = hm
        cls_adj = base.classify(d_adj)
        print(f"{TAG} 复权敏感性：买入价需换算 {n_adj_price} 笔；"
              f"前复权 vs 不复权，三分类标签变化 {int((cls_adj.to_numpy() != d['cls'].to_numpy()).sum())} 笔")

        # ---------- Part 3 ----------
        sb = store.query("SELECT ts_code, industry FROM stock_basic")
        ind_all = {}
        for _, r in sb.iterrows():
            v = r["industry"]
            ind_all[r["ts_code"]] = v if isinstance(v, str) and v else "其他"
        codes_of_d = list(d["ts_code"])
        raw_ind = pd.Series([ind_all.get(c, "其他") for c in codes_of_d])
        top8 = list(raw_ind.value_counts().index[:8])
        ind_map = {c: (ind_all.get(c, "其他") if ind_all.get(c, "其他") in top8 else "其他")
                   for c in set(codes_of_d)}
        n_named = sum(1 for c in set(codes_of_d) if ind_all.get(c) not in (None, "其他"))
        print(f"{TAG} 行业映射：{len(set(codes_of_d))} 只标的、{n_named} 只有行业标签；"
              f"top8 = {top8}，其余并入「其他」")
        df = build_features(d, books, panels, ind_map)
        df = attach_subsample(df, store)
        leak_check(df, panels)

        # ---------- Part 4 ----------
        hr("Part 4  买入级组间检验：即错(88) vs 顺利(69) + BH-FDR")
        full = run_tests(df, "全样本")
        n_sig_q = int((full["q"] < 0.05).sum())
        n_sig_p = int((full["p"] < 0.05).sum())
        n_sig_bonf = int((full["p"] < 0.05 / len(full)).sum())
        exp_false = 0.05 * len(full)
        print(f"\n{TAG} 名义 p<0.05：{n_sig_p}/{len(full)}；BH-FDR q<0.05：{n_sig_q}；"
              f"Bonferroni 阈值 {0.05 / len(full):.4f} 下显著 {n_sig_bonf}")
        print(f"{TAG} 纯随机预期假阳性 ≈ {exp_false:.1f} 个（检验间高度相关，此数仅供参考）")
        top_nom = full[full["p"] < 0.05]
        if len(top_nom) >= 3:
            print(f"\n{TAG} 名义显著的 {len(top_nom)} 个特征彼此相关吗（Spearman）？"
                  f"若高度相关，就不是 {len(top_nom)} 个独立发现：")
            corr = df[top_nom["feature"].tolist()].corr(method="spearman").round(2)
            print(corr.to_string())

        n1, n2 = 88, 69
        print(f"\n{TAG} 功效：n1={n1}, n2={n2} → 80% 功效可检出最小 Cliff's δ ≈ "
              f"{mde_cliff(n1, n2):.3f}（α=0.05 双侧）；IS 只有 8 vs 12 时为 "
              f"{mde_cliff(8, 12):.3f}。小于该值的真实差异**检不出来**（≠不存在）")

        dup = int(df.duplicated(subset=["ts_code", "buy_date"], keep=False).sum())
        print(f"{TAG} 重复观测：同一 (标的,买入日) 多次出现 {dup} 笔；"
              f"独立 (标的,日) 组 {df[['ts_code', 'buy_date']].drop_duplicates().shape[0]} 组")
        dedup = df.sort_values(["ts_code", "buy_date", "ledger"]).drop_duplicates(
            subset=["ts_code", "buy_date"], keep="first")
        print(f"{TAG} 去重样本 {len(dedup)} 笔（即错 {int((dedup['cls'] == ERR).sum())}, "
              f"顺利 {int((dedup['cls'] == SMOOTH).sum())}）→ 独立性稳健性检验")
        ded = run_tests(dedup, "去重稳健性", quiet=True)
        print(f"  去重后名义 p<0.05：{int((ded['p'] < 0.05).sum())} 个；全样本 top5 在去重样本：")
        for k in full.head(5)["feature"]:
            rr = ded[ded["feature"] == k]
            if rr.empty:
                continue
            r0 = rr.iloc[0]
            print(f"    {NAME_OF[k]:<24} 去重 p={r0['p']:.4f} δ={r0['effect']:+.3f} "
                  f"（全样本 p={full.loc[full['feature'] == k, 'p'].iloc[0]:.4f}）")

        # ---------- Part 4b 日期聚类 ----------
        hr("Part 4b  日期聚类校正：市场层特征在同一买入日内是常数，买入级 p 值不成立")
        by_date = df.groupby("buy_date").agg(
            n=("cls", "size"),
            err=("cls", lambda s: int((s == ERR).sum())),
            ok=("cls", lambda s: int((s == SMOOTH).sum())))
        by_date["err_rate"] = by_date["err"] / by_date["n"]
        by_date["ok_rate"] = by_date["ok"] / by_date["n"]
        mixed = int(((by_date["err"] > 0) & (by_date["ok"] > 0)).sum())
        print(f"{TAG} 292 笔买入分布在 {len(by_date)} 个买入日；每日笔数 min/中位/max = "
              f"{int(by_date['n'].min())}/{int(by_date['n'].median())}/{int(by_date['n'].max())}")
        print(f"{TAG} 同时含「即错」和「顺利」的日期仅 {mixed} 个 → 日期层有效样本 ≈ {len(by_date)}，"
              f"远小于买入笔数 292")
        print(f"{TAG} 每日即错率分布：")
        print(by_date[by_date["n"] >= 3][["n", "err", "err_rate", "ok_rate"]]
              .sort_index().to_string(float_format=lambda v: f"{v:.2f}"))

        print(f"\n{TAG} 市场层特征：改为「日期层」检验（每个买入日一个观测，"
              f"即错率 vs 特征，Spearman）")
        mk = df.groupby("buy_date")[MARKET_NUM].first()
        use = by_date["n"] >= 3
        rows = []
        for feat in MARKET_NUM:
            x = mk.loc[use, feat].to_numpy(float)
            y = by_date.loc[use, "err_rate"].to_numpy(float)
            okm = np.isfinite(x) & np.isfinite(y)
            if okm.sum() < 8 or np.std(x[okm]) == 0:
                continue
            rho, p = stats.spearmanr(x[okm], y[okm])
            rows.append({"feature": feat, "name": NAME_OF[feat], "rho": float(rho),
                         "p": float(p), "n_dates": int(okm.sum())})
        mk_res = pd.DataFrame(rows)
        if len(mk_res):
            mk_res["q"] = bh_fdr(mk_res["p"].to_numpy())
            mk_res = mk_res.sort_values("p").reset_index(drop=True)
            print(f"{'特征':<24}{'n_dates':>9}{'rho':>9}{'p':>9}{'q(FDR)':>9}  判定")
            for _, r in mk_res.iterrows():
                v = "**FDR显著**" if r["q"] < 0.05 else ("名义p<.05" if r["p"] < 0.05 else "")
                print(f"{r['name']:<24}{r['n_dates']:>9}{r['rho']:>9.3f}{r['p']:>9.4f}"
                      f"{r['q']:>9.4f}  {v}")
            print(f"  注：日期层 n_dates={int(mk_res['n_dates'].max())}（每个买入日 1 个观测），"
                  f"ρ 的 95% CI 很宽；仅作「买入级 p 是否虚高」的对照")
        n_mk_sig = int((mk_res["q"] < 0.05).sum()) if len(mk_res) else 0

        print(f"\n{TAG} 个股特征：日内配对符号检验（只用在同一天同时有即错+顺利的日期）")
        stock_top = [f for f in full.head(8)["feature"]
                     if f not in DATE_CONST and f != "weekday"][:5]
        sign_rows = []
        for feat in stock_top:
            signs = []
            for _, g in df.groupby("buy_date"):
                ev = g.loc[g["cls"] == ERR, feat].dropna()
                sv = g.loc[g["cls"] == SMOOTH, feat].dropna()
                if len(ev) and len(sv):
                    signs.append(np.sign(float(ev.median()) - float(sv.median())))
            kpos = int(sum(1 for s in signs if s > 0))
            neff = len(signs)
            if neff < 5:
                continue
            p = float(stats.binomtest(kpos, neff, 0.5).pvalue)
            sign_rows.append({"feature": feat, "k_pos": kpos, "n_dates": neff, "p": p})
            print(f"  {NAME_OF[feat]:<24} 即错中位>顺利中位 的日期 {kpos}/{neff}，符号检验 p={p:.4f}")
        print("  注：日内配对把「市场/时间」完全控掉，是比买入级更干净的检验，但每个日期只贡献 1 个符号")

        # ---------- Part 5 波动率机械基准 ----------
        hr("Part 5  即错率是不是「波动率的机械函数」？零漂移随机游走基准")
        df["mae3_close"] = np.nan
        df["mfe3_close"] = np.nan
        for i, r in df.iterrows():
            cdf = panels["per_code"][r["ts_code"]]
            dates = cdf["trade_date"].tolist()
            i0 = bisect.bisect_right(dates, r["buy_date"]) - 1
            seg = cdf["close"].to_numpy(float)[i0 + 1: i0 + 4]
            if len(seg) == 3:
                df.at[i, "mae3_close"] = float(seg.min() / r["buy_price"] - 1)
                df.at[i, "mfe3_close"] = float(seg.max() / r["buy_price"] - 1)
        close_err = ((df["mae3_close"] <= -0.03) & (df["mfe3_close"] < 0.01))
        print(f"{TAG} 收盘价口径（不用日内高低点）的即错率："
              f"{int(close_err.sum())}/{int(df['mae3_close'].notna().sum())} = "
              f"{close_err.sum() / df['mae3_close'].notna().sum():.1%}"
              f"（标签口径用日内高低点，天然更极端）")

        # 零漂移 / 市场漂移两种随机游走基准：P(3日内 min<=-3% 且 max<+1%)
        rng = np.random.default_rng(SEED)
        sigma = df["s_vol20"].to_numpy(float)
        sigma = np.where(np.isfinite(sigma), sigma, float(np.nanmedian(sigma)))
        axis_all, ic_all = panels["idx_dates"], panels["idx_c"]
        lo_i = bisect.bisect_left(axis_all, IS_START)
        hi_i = bisect.bisect_right(axis_all, OOS_END)
        drift = float(np.mean(np.diff(ic_all[lo_i:hi_i]) / ic_all[lo_i:hi_i - 1]))
        print(f"{TAG} 基准漂移：{IS_START}~{OOS_END} 指数日均收益 = {drift:+.4%}/日")

        def rw_p(sig: np.ndarray, mu: float, n_path: int = 4000) -> np.ndarray:
            out = np.empty(len(sig))
            for i, s in enumerate(sig):
                steps = rng.normal(mu, s, size=(n_path, 3))
                rel = np.exp(np.cumsum(steps, axis=1)) - 1.0
                out[i] = float(np.mean((rel.min(axis=1) <= -0.03) & (rel.max(axis=1) < 0.01)))
            return out

        p_rw0 = rw_p(sigma, 0.0)
        p_rw = rw_p(sigma, drift)
        df["rw_p_err"] = p_rw
        df["rw_p_err0"] = p_rw0
        print(f"{TAG} 随机入场即错概率：零漂移 {p_rw0.mean():.1%}；市场漂移 {p_rw.mean():.1%}")
        close_err_rate = close_err.sum() / df["mae3_close"].notna().sum()
        print(f"{TAG} 实际：标签口径（日内高低点）{int((df['cls'] == ERR).sum()) / len(df):.1%}"
              f"；收盘价口径 {close_err_rate:.1%} → 与收盘口径基准 {p_rw.mean():.1%} 同口径可比")
        print(f"     → 实际{'高于' if close_err_rate > p_rw.mean() else '低于'}基准 "
              f"{(close_err_rate - p_rw.mean()) * 100:+.1f}pp"
              f"（市场同期确实在跌，基准已含漂移；残差部分才是「选股/入场」的净贡献）")

        # 基准偏误检查：σ 取的是「买入日前 20 日」，若买入后波动放大，基准会低估
        scale = []
        for _, r in df.iterrows():
            cdf = panels["per_code"][r["ts_code"]]
            dates = cdf["trade_date"].tolist()
            i0 = bisect.bisect_right(dates, r["buy_date"]) - 1
            seg = cdf["close"].to_numpy(float)[i0: i0 + 4]
            s = r["s_vol20"]
            if len(seg) == 4 and np.isfinite(s) and s > 0 and seg[0] > 0:
                scale.append(abs(math.log(seg[-1] / seg[0])) / (s * math.sqrt(3)))
        scale = np.array(scale)
        ratio = float(np.median(scale)) / math.sqrt(2.0 / math.pi)
        print(f"{TAG} 基准偏误检查：实际 3 日 |涨跌| / (σ20·√3) 中位 {np.median(scale):.3f}"
              f"（正态下应≈{math.sqrt(2 / math.pi):.3f}）→ 买入后实际波动 ≈ 买入前估计的 {ratio:.2f} 倍")

        df["vol_q"] = pd.qcut(df["s_vol20"], 5, labels=False, duplicates="drop")
        err_close_now = (df["mae3_close"] <= -0.03) & (df["mfe3_close"] < 0.01)
        print(f"\n{TAG} 按个股 20 日波动率五等分（这是「即错」定义自带的机械通道）：")
        print(f"{'波动率档':<10}{'n':>5}{'波动率中位':>11}{'即错率':>9}{'即错率(收盘)':>12}"
              f"{'随机基准':>10}{'顺利率':>9}{'胜率':>8}{'EV(元)':>10}")
        for q, g in df.groupby("vol_q"):
            print(f"{'Q' + str(int(q) + 1):<10}{len(g):>5}{g['s_vol20'].median():>11.4f}"
                  f"{(g['cls'] == ERR).mean():>9.1%}{err_close_now.loc[g.index].mean():>12.1%}"
                  f"{g['rw_p_err'].mean():>10.1%}"
                  f"{(g['cls'] == SMOOTH).mean():>9.1%}{(g['round_pnl'] > 0).mean():>8.1%}"
                  f"{g['round_pnl'].mean():>10.1f}")
        print("  读法：若「即错率」随波动率单调上升、且贴近随机基准，则即错更多是**波动率标签**，"
              "不是信号选错")

        # ---------- Part 5b 标签定义稳健性 ----------
        hr("Part 5b  标签定义稳健性：把「MAE/MFE」从日内高低点换成收盘价，结论会不会变？")
        df_alt = df.copy()
        df_alt["mae3"] = df["mae3_close"]
        df_alt["mfe3"] = df["mfe3_close"]
        cls_alt = base.classify(df_alt)
        print(f"{TAG} 主口径（日内高低点）即错 {int((df['cls'] == ERR).sum())} 笔；"
              f"收盘口径即错 {int((cls_alt == ERR).sum())} 笔")
        tab = pd.crosstab(df["cls"], cls_alt, rownames=["主口径"], colnames=["收盘口径"])
        print(tab.to_string())
        both = int(((df["cls"] == ERR) & (cls_alt == ERR)).sum())
        print(f"{TAG} 两种口径同判为即错 {both} 笔；主口径独有 "
              f"{int(((df['cls'] == ERR) & (cls_alt != ERR)).sum())} 笔；收盘口径独有 "
              f"{int(((df['cls'] != ERR) & (cls_alt == ERR)).sum())} 笔 → "
              f"「即错」这个标签本身对「日内高点是否摸过 +1%」很敏感")
        dfa = df.copy()
        dfa["cls"] = cls_alt
        alt_res = run_tests(dfa, "收盘口径重分类", quiet=True)
        n_alt_sig = int((alt_res["q"] < 0.05).sum())
        print(f"{TAG} 换标签后：即错(n={int((cls_alt == ERR).sum())}) vs 顺利"
              f"(n={int((cls_alt == SMOOTH).sum())})，BH-FDR q<0.05 {n_alt_sig} 个、"
              f"名义 p<0.05 {int((alt_res['p'] < 0.05).sum())} 个")
        print(f"{TAG} 换标签后 FDR 显著的特征（全部）：")
        print(f"{'特征':<26}{'p':>11}{'q(FDR)':>11}{'δ':>9}")
        for _, r in alt_res[alt_res["q"] < 0.05].iterrows():
            print(f"{r['name']:<26}{r['p']:>11.2e}{r['q']:>11.2e}{r['effect']:>9.3f}")
        print(f"{TAG} 换标签后前 5 名：")
        print(f"{'特征':<26}{'p':>11}{'q(FDR)':>11}{'δ':>9}")
        for _, r in alt_res.head(5).iterrows():
            print(f"{r['name']:<26}{r['p']:>11.2e}{r['q']:>11.2e}{r['effect']:>9.3f}")

        # ---------- Part 6 ----------
        hr("Part 6  样本内定特征 → 样本外验证（任务书规定的切分）")
        is_df = df[df["bd"] <= pd.Timestamp(IS_END)]
        oos_df = df[df["bd"] >= pd.Timestamp(OOS_START)]
        print(f"{TAG} 样本内 {IS_START}~{IS_END}：{len(is_df)} 笔 / {is_df['buy_date'].nunique()} 个买入日 "
              f"(即错 {int((is_df['cls'] == ERR).sum())}, 顺利 {int((is_df['cls'] == SMOOTH).sum())})")
        print(f"{TAG} 样本外 {OOS_START}~{OOS_END}：{len(oos_df)} 笔 / {oos_df['buy_date'].nunique()} 个买入日 "
              f"(即错 {int((oos_df['cls'] == ERR).sum())}, 顺利 {int((oos_df['cls'] == SMOOTH).sum())})")
        print(f"{TAG} 切分严重失衡（样本内仅 {len(is_df)} 笔 / {is_df['buy_date'].nunique()} 个买入日）"
              f"→ 样本内这端实质上没有挑选能力"
              f"（可检出最小 δ≈{mde_cliff(8, 12):.2f}），见 Part 6.3")
        cut = pd.cut(df["bd"], bins=[pd.Timestamp(IS_START), pd.Timestamp(IS_END),
                                     pd.Timestamp(OOS_END)],
                     labels=["样本内", "样本外"], include_lowest=True, right=True)
        print(pd.crosstab(df["ledger"], cut).to_string())
        print(f"{TAG} 信号来源×期间（**关键**：样本内根本没有 IR 信号）：")
        print(pd.crosstab(df["sig_source"], cut).to_string())

        is_res = run_tests(is_df, "样本内 2026-08-26~09-30")
        oos_res = run_tests(oos_df, "样本外 2026-10-01~11-10")
        print(f"\n{TAG} 样本内 q<0.05：{int((is_res['q'] < 0.05).sum())} 个；"
              f"样本外 q<0.05：{int((oos_res['q'] < 0.05).sum())} 个（样本外检验家族独立校正）")

        print(f"\n{TAG} 6.2 样本内最可疑 5 个特征 → 样本外复现情况：")
        merged = []
        for _, r in is_res.head(5).iterrows():
            o = oos_res[oos_res["feature"] == r["feature"]]
            if o.empty:
                continue
            o = o.iloc[0]
            same = bool(np.sign(r["effect"]) == np.sign(o["effect"]))
            merged.append({"feature": r["feature"], "dir_same": same,
                           "is_p": r["p"], "oos_p": o["p"], "oos_q": o["q"]})
            print(f"  {r['name']:<24} 内 p={r['p']:.4f} q={r['q']:.3f} δ={r['effect']:+.3f} | "
                  f"外 p={o['p']:.4f} q={o['q']:.3f} δ={o['effect']:+.3f} | "
                  f"方向{'一致' if same else '翻转'}；样本外"
                  f"{'FDR显著' if o['q'] < 0.05 else ('名义显著' if o['p'] < 0.05 else '不显著')}")

        print(f"\n{TAG} 6.3 全样本 top5 特征在各期间的效应稳定性（方向一致才可能可用）：")
        print(f"{'特征':<24}{'全样本δ':>9}{'样本内δ':>9}{'样本外δ':>9}  方向")
        stab = 0
        for k in full.head(5)["feature"]:
            d_all = full.loc[full["feature"] == k, "effect"].iloc[0]
            ri = is_res[is_res["feature"] == k]
            ro = oos_res[oos_res["feature"] == k]
            di = ri["effect"].iloc[0] if len(ri) else np.nan
            do = ro["effect"].iloc[0] if len(ro) else np.nan
            ok_dir = bool(np.sign(di) == np.sign(d_all) == np.sign(do)) if np.isfinite(di) and np.isfinite(do) else False
            stab += int(ok_dir)
            print(f"{NAME_OF[k]:<24}{d_all:>9.3f}{di:>9.3f}{do:>9.3f}  "
                  f"{'三期一致' if ok_dir else '不一致'}")
        print(f"  → top5 中三期方向一致的 {stab}/5 个")

        print(f"\n{TAG} 6.4 同账本对照（只用 main：样本内 vs 样本外，避免换账本/换策略的混杂）")
        m_is = df[(df["ledger"] == "main") & (df["bd"] <= pd.Timestamp(IS_END))]
        m_oos = df[(df["ledger"] == "main") & (df["bd"] >= pd.Timestamp(OOS_START))]
        print(f"  main 样本内 {len(m_is)} 笔 (即错 {int((m_is['cls'] == ERR).sum())}), "
              f"样本外 {len(m_oos)} 笔 (即错 {int((m_oos['cls'] == ERR).sum())})")
        if len(m_is) >= 20 and len(m_oos) >= 20:
            mi = run_tests(m_is, "main·样本内", quiet=True)
            mo = run_tests(m_oos, "main·样本外", quiet=True)
            ok_n = 0
            for k in mi.head(5)["feature"]:
                ri = mi[mi["feature"] == k].iloc[0]
                ro = mo[mo["feature"] == k]
                if ro.empty:
                    continue
                ro = ro.iloc[0]
                same = bool(np.sign(ri["effect"]) == np.sign(ro["effect"]))
                ok_n += int(same)
                print(f"    {NAME_OF[k]:<24} 内 p={ri['p']:.4f} δ={ri['effect']:+.3f} | "
                      f"外 p={ro['p']:.4f} δ={ro['effect']:+.3f} | 方向{'一致' if same else '翻转'}")
            print(f"  → main 内 top5 方向一致 {ok_n}/5")

        print(f"\n{TAG} 6.5 现实可行的切分：10-01~10-16 定 → 10-19~10-23 验（OOS 内部再切，信号来源相同）")
        h1 = df[(df["bd"] >= pd.Timestamp(OOS_START)) & (df["bd"] <= pd.Timestamp(date(2026, 10, 16)))]
        h2 = df[df["bd"] >= pd.Timestamp(date(2026, 10, 19))]
        print(f"  前半 {len(h1)} 笔 / {h1['buy_date'].nunique()} 个买入日 "
              f"(即错 {int((h1['cls'] == ERR).sum())}, 顺利 {int((h1['cls'] == SMOOTH).sum())})")
        print(f"  后半 {len(h2)} 笔 / {h2['buy_date'].nunique()} 个买入日 "
              f"(即错 {int((h2['cls'] == ERR).sum())}, 顺利 {int((h2['cls'] == SMOOTH).sum())})")
        h1_res = run_tests(h1, "前半 10-01~10-16", quiet=True) if len(h1) >= 40 else pd.DataFrame()
        h2_res = run_tests(h2, "后半 10-19~10-23", quiet=True) if len(h2) >= 30 else pd.DataFrame()
        if len(h1_res) and len(h2_res):
            print(f"  前半 q<0.05：{int((h1_res['q'] < 0.05).sum())}；"
                  f"后半 q<0.05：{int((h2_res['q'] < 0.05).sum())}；"
                  f"前半名义 p<0.05：{int((h1_res['p'] < 0.05).sum())}")
            n_ok = 0
            print(f"  前半 top5 → 后半：")
            for k in h1_res.head(5)["feature"]:
                a1 = h1_res[h1_res["feature"] == k].iloc[0]
                b2 = h2_res[h2_res["feature"] == k]
                if b2.empty:
                    continue
                b2 = b2.iloc[0]
                same = bool(np.sign(a1["effect"]) == np.sign(b2["effect"]))
                n_ok += int(same)
                print(f"    {NAME_OF[k]:<24} 前 δ={a1['effect']:+.3f} p={a1['p']:.4f} | "
                      f"后 δ={b2['effect']:+.3f} p={b2['p']:.4f} | "
                      f"{'方向一致' if same else '方向翻转'}")
            print(f"  → 方向一致 {n_ok}/{min(5, len(h1_res))}")

            bk = h1_res.iloc[0]["feature"]
            if KIND_OF[bk] == "cont":
                thr_h = float(np.nanmedian(pd.to_numeric(h1[bk], errors="coerce")))
                direction = "low" if h1_res.iloc[0]["effect"] > 0 else "high"
                print(f"  用前半 p 最小特征「{NAME_OF[bk]}」+ 前半中位数 {thr_h:.4g} 做过滤：")
                for label, sub in (("前半", h1), ("后半", h2), ("全样本", df)):
                    vals = pd.to_numeric(sub[bk], errors="coerce")
                    mask = (vals <= thr_h) if direction == "low" else (vals >= thr_h)
                    eval_filter(sub, bk, mask, f"{label}·{NAME_OF[bk]}"
                                               f"{'≤' if direction == 'low' else '≥'}{thr_h:.4g}")
            else:
                print(f"  前半最优为分类特征，跳过单特征过滤")

        # ---------- Part 7 ----------
        hr("Part 7  单特征过滤：胜率与 Wilson CI")
        base_n, base_wins = len(df), int((df["round_pnl"] > 0).sum())
        base_err = int((df["cls"] == ERR).sum())
        base_ok = int((df["cls"] == SMOOTH).sum())
        blo, bhi = wilson_ci(base_wins, base_n)
        base_wr = base_wins / base_n
        print(f"{TAG} 基准（全部 {base_n} 笔）：即错 {base_err} 笔 {base_err / base_n:.1%}；"
              f"顺利 {base_ok} 笔；买入级胜率 {base_wr:.1%} CI[{blo:.1%},{bhi:.1%}]；"
              f"EV {df['round_pnl'].mean():+.2f} 元/笔；合计 {df['round_pnl'].sum():+.0f} 元")

        print(f"\n{TAG} 7.1 按任务书口径：用样本内（34 笔）选出 p 最小特征、阈值取样本内中位数")
        results = []
        best = is_res.iloc[0] if len(is_res) else None
        if best is not None:
            key = best["feature"]
            print(f"  选中：{best['name']}（样本内 p={best['p']:.4f}, q={best['q']:.3f}, "
                  f"{best['effect_name']}={best['effect']:+.3f}）")
            if KIND_OF[key] == "cont":
                thr = float(np.nanmedian(pd.to_numeric(is_df[key], errors="coerce")))
                direction = "low" if best["effect"] > 0 else "high"
                print(f"  方向：即错组{'更大' if best['effect'] > 0 else '更小'} → 保留"
                      f"{'≤' if direction == 'low' else '≥'} {thr:.4g}")
                for label, sub in (("全样本", df), ("样本内", is_df), ("样本外", oos_df)):
                    vals = pd.to_numeric(sub[key], errors="coerce")
                    mask = (vals <= thr) if direction == "low" else (vals >= thr)
                    results.append(eval_filter(sub, key, mask,
                                               f"{label}·{best['name']}{'≤' if direction == 'low' else '≥'}{thr:.4g}"))
                vals = pd.to_numeric(df[key], errors="coerce")
                opp = (vals > thr) if direction == "low" else (vals < thr)
                print(f"\n{TAG} 7.2 反向对照（保留另一半）——反方向也「有效」就说明是噪声：")
                results.append(eval_filter(df, key, opp, "全样本·反向"))
            else:
                agg = is_df.groupby(is_df[key].astype(str))["cls"].agg(
                    lambda s: (s == ERR).mean())
                cnts = is_df[key].astype(str).value_counts()
                agg = agg[cnts[cnts >= 5].index]
                keep_cat = str(agg.idxmin()) if len(agg) else None
                print(f"  保留类别 = '{keep_cat}'（样本内即错率最低且 n≥5）")
                for label, sub in (("全样本", df), ("样本内", is_df), ("样本外", oos_df)):
                    results.append(eval_filter(sub, key, sub[key].astype(str) == keep_cat,
                                               f"{label}·{best['name']}={keep_cat}"))
            keep = results[0]
            print(f"\n{TAG} 全样本过滤后胜率 {keep['win_rate']:.1%} vs 基准 {base_wr:.1%}；"
                  f"Wilson 下界 {keep['win_lower']:.1%} → "
                  f"{'下界超过基准点估计' if (keep['win_lower'] or 0) > base_wr else '下界仍低于基准点估计（提升不显著）'}")

        print(f"\n{TAG} 7.2b 信号强度过滤（sig_score 越小=越极端超卖；仅 IR/WBR 有值，n=258）")
        ssub = df[df["sig_score"].notna()]
        s_wr = float((ssub["round_pnl"] > 0).mean())
        print(f"  子样本基准（n={len(ssub)}）：即错 {int((ssub['cls'] == ERR).sum())} 笔 "
              f"{(ssub['cls'] == ERR).mean():.1%}；胜率 {s_wr:.1%}；"
              f"EV {ssub['round_pnl'].mean():+.2f} 元")
        print(f"  注：任务书口径的「样本内」(08-26~09-30) 没有任何 IR 信号（见 6.x 交叉表），"
              f"故阈值改用 10-01~10-16 的前半中位数")
        s1 = ssub[ssub["bd"] <= pd.Timestamp(date(2026, 10, 16))]
        s2 = ssub[ssub["bd"] >= pd.Timestamp(date(2026, 10, 19))]
        thr_s = float(np.nanmedian(s1["sig_score"])) if s1["sig_score"].notna().any() else np.nan
        if np.isfinite(thr_s):
            print(f"  前半中位数阈值 thr = {thr_s:.4g} → 保留 sig_score ≤ thr（更极端超卖）")
            for label, sub in (("前半(定阈值)", s1), ("后半(验证)", s2), ("全样本(IR/WBR)", ssub)):
                eval_filter(sub, "sig_score", sub["sig_score"] <= thr_s, f"{label}·sig≤{thr_s:.4g}")
            eval_filter(ssub, "sig_score", ssub["sig_score"] > thr_s, "全样本·反向(sig>thr)")
        else:
            print("  前半无 sig_score 数据，跳过")

        print(f"\n{TAG} 7.3 全样本特征过滤排行榜（事后选择上界，**不是可用规则**）")
        rows = []
        for key, name, kind, family in FEATURES:
            if key not in df.columns:
                continue
            if kind == "cont":
                vals = pd.to_numeric(df[key], errors="coerce")
                med = float(np.nanmedian(vals))
                eff = full.loc[full["feature"] == key, "effect"]
                if eff.empty:
                    continue
                mask = (vals <= med) if eff.iloc[0] > 0 else (vals >= med)
                desc = f"{'≤' if eff.iloc[0] > 0 else '≥'}{med:.4g}" if np.isfinite(med) else "n/a"
            else:
                agg = df.groupby(df[key].astype(str))["cls"].agg(lambda s: (s == ERR).mean())
                cnts = df[key].astype(str).value_counts()
                agg = agg[cnts[cnts >= 10].index]
                if not len(agg):
                    continue
                keep_cat = str(agg.idxmin())
                mask = df[key].astype(str) == keep_cat
                desc = f"={keep_cat}"
            r = eval_filter(df, key, mask, f"{name}{desc}", quiet=True)
            rows.append({"name": name, "desc": desc, "n": r["n"], "err": r["err_rate"],
                         "win": r["win_rate"], "lo": r["win_ci"][0], "hi": r["win_ci"][1],
                         "ev": r["ev"]})
        rank = pd.DataFrame(rows).sort_values("win", ascending=False)
        print(f"{'特征':<26}{'保留条件':<16}{'n':>5}{'即错率':>8}{'胜率':>8}{'95%CI':>18}{'EV(元)':>9}")
        for _, r in rank.head(8).iterrows():
            print(f"{r['name']:<26}{r['desc']:<16}{r['n']:>5}{r['err']:>8.1%}{r['win']:>8.1%}"
                  f"  [{r['lo']:.1%},{r['hi']:.1%}]{r['ev']:>9.1f}")
        print(f"  → {len(rank)} 个特征取胜率最大者天然上偏；且这些阈值是在**含样本外**的全样本上事后挑的")

        print(f"\n{TAG} 7.4 市场闸门网格（阈值事前设定、全部列出，不在候选中挑最优）")
        print(f"{'闸门':<36}{'n':>5}{'买入日':>7}{'其中样本内':>9}{'即错率':>8}{'胜率':>8}"
              f"{'95%CI':>18}{'EV(元)':>9}")
        for feat, thrs in (("m_ret20", (-0.03, -0.04, -0.05)),
                           ("m_dist_ma20", (-0.02, -0.03, -0.04)),
                           ("m_dd60", (-0.04, -0.06, -0.08))):
            for th in thrs:
                sub = df[pd.to_numeric(df[feat], errors="coerce") <= th]
                n = len(sub)
                if n == 0:
                    continue
                wins = int((sub["round_pnl"] > 0).sum())
                lo, hi = wilson_ci(wins, n)
                nis = int((sub["bd"] <= pd.Timestamp(IS_END)).sum())
                print(f"{NAME_OF[feat] + f'≤{th:.0%}':<36}{n:>5}{sub['buy_date'].nunique():>7}"
                      f"{nis:>9}{(sub['cls'] == ERR).mean():>8.1%}{wins / n:>8.1%}"
                      f"  [{lo:.1%},{hi:.1%}]{sub['round_pnl'].mean():>9.1f}")
        deepest = df[pd.to_numeric(df["m_ret20"], errors="coerce") <= -0.05]
        print(f"  「指数近20日涨幅≤−5%」覆盖的买入日："
              f"{sorted(str(x) for x in deepest['buy_date'].unique())}")
        print(f"  基准胜率 {base_wr:.1%}（n={base_n}）；注意买入日数才是独立观测数")

        print(f"\n{TAG} 7.5 波动率(beta)过滤——本诊断里唯一「换标签后仍 FDR 显著」的维度")
        p40 = float(np.nanpercentile(df["s_vol20"], 40))
        print(f"  保留 20 日波动率 ≤ P40 = {p40:.4f} 的买入（去掉波动最高的 60%）")
        lowvol = df["s_vol20"] <= p40
        r_lv = eval_filter(df, "s_vol20", lowvol, "全样本·低波动档")
        for label, sub in (("前半(10-01~10-16)", h1), ("后半(10-19~10-23)", h2)):
            eval_filter(sub, "s_vol20",
                        pd.to_numeric(sub["s_vol20"], errors="coerce") <= p40,
                        f"{label}·低波动档")
        kept = df[lowvol]
        print(f"  同一过滤在「收盘口径」标签下：即错率 "
              f"{(dfa.loc[lowvol, 'cls'] == ERR).mean():.1%}（全样本 "
              f"{(dfa['cls'] == ERR).mean():.1%}）；被剔除档 "
              f"{(dfa.loc[~lowvol, 'cls'] == ERR).mean():.1%}")
        print(f"  真伪判断：低波动 = 低 beta，在「指数区间 −5.8%」的下跌市里天然少亏；"
              f"这不是「入场信号更强」，换到上涨区间结论可能反转 → 属仓位/风格问题，不是入场信号问题")

        # ---------- Part 8 ----------
        hr("Part 8  时间/状态集中度：即错是不是市场 beta？")
        print(f"{TAG} 各类轮次 PnL（元）：")
        print(df.groupby("cls")["round_pnl"].agg(
            笔数="count", 盈利笔数=lambda s: int((s > 0).sum()), 均值="mean", 中位="median",
            胜率=lambda s: float((s > 0).mean())).to_string(
            float_format=lambda v: f"{v:.2f}"))
        print(f"\n{TAG} 即错率 × 星期几（全样本；注意一周只有 4–5 个买入日，是日期代理）：")
        for w, g in df.groupby("weekday"):
            print(f"  周{'一二三四五'[int(w)]}: {int((g['cls'] == ERR).sum()):2d}/{len(g):3d} = "
                  f"{(g['cls'] == ERR).mean():.1%}；胜率 {(g['round_pnl'] > 0).mean():.1%}")
        ss = df[df["sig_score"].notna()].copy()
        print(f"\n{TAG} 信号分位值四分位（仅 IR/WBR，n={len(ss)}；越小越极端超卖）：")
        ss["sq"] = pd.qcut(ss["sig_score"], 4, labels=False, duplicates="drop")
        for q, g in ss.groupby("sq"):
            print(f"  Q{int(q) + 1}（sig ≤ {g['sig_score'].max():.2f}）：n={len(g):3d} "
                  f"即错 {(g['cls'] == ERR).mean():.1%} "
                  f"胜率 {(g['round_pnl'] > 0).mean():.1%} EV {g['round_pnl'].mean():+.1f} 元")
        df["month"] = df["bd"].dt.strftime("%Y-%m")
        for mth, g in df.groupby("month"):
            k = int((g["cls"] == ERR).sum())
            print(f"  {mth}: 即错 {k:2d}/{len(g):3d} = {k / len(g):.1%}")
        tab = pd.crosstab(df["month"], df["cls"] == ERR)
        if tab.shape[0] >= 2:
            print(f"  月份间差异 卡方 p={stats.chi2_contingency(tab, correction=False)[1]:.4f}")

        print(f"\n{TAG} 即错率 × 指数状态（买入日可观测）：")
        for st, g in df.groupby("m_state"):
            k = int((g["cls"] == ERR).sum())
            print(f"  {st}: {k:2d}/{len(g):3d} = {k / len(g):.1%}")
        tab = pd.crosstab(df["m_state"], df["cls"] == ERR)
        if tab.shape[0] >= 2:
            print(f"  卡方 p={stats.chi2_contingency(tab, correction=False)[1]:.4f}")
        zg = df[df["m_state"] == "震荡"]
        if len(zg):
            print(f"  「震荡」状态的买入日：{sorted(str(x) for x in zg['buy_date'].unique())}"
                  f"（样本内 {int((zg['bd'] <= pd.Timestamp(IS_END)).sum())} 笔 / "
                  f"样本外 {int((zg['bd'] >= pd.Timestamp(OOS_START)).sum())} 笔）")
        print(f"\n{TAG} 即错率 × 信号来源：")
        for st, g in df.groupby("sig_source"):
            k = int((g["cls"] == ERR).sum())
            print(f"  {st}: {k:2d}/{len(g):3d} = {k / len(g):.1%}")
        tab = pd.crosstab(df["sig_source"], df["cls"] == ERR)
        if tab.shape[0] >= 2:
            print(f"  卡方 p={stats.chi2_contingency(tab, correction=False)[1]:.4f}")

        print(f"\n{TAG} 即错率 × 账本：")
        for st, g in df.groupby("ledger"):
            k = int((g["cls"] == ERR).sum())
            print(f"  {st}: {k:2d}/{len(g):3d} = {k / len(g):.1%}")

        print(f"\n{TAG} 归因（用未来数据，仅解释、不作过滤）：买入日后 3 日指数涨跌")
        idx_dates, idx_c = panels["idx_dates"], panels["idx_c"]
        nxt3 = []
        for _, r in df.iterrows():
            j = bisect.bisect_right(idx_dates, r["buy_date"]) - 1
            nxt3.append(idx_c[j + 3] / idx_c[j] - 1.0 if j + 3 < len(idx_c) else np.nan)
        df["idx_next3"] = nxt3
        print(df.groupby("cls")["idx_next3"].agg(["count", "mean", "median"])
              .to_string(float_format=lambda v: f"{v: .4f}"))
        res = mwu(df.loc[df["cls"] == ERR, "idx_next3"],
                  df.loc[df["cls"] == SMOOTH, "idx_next3"])
        if res:
            print(f"  即错 vs 顺利：未来3日指数收益中位 {res['med1']:+.2%} vs {res['med2']:+.2%}，"
                  f"p={res['p']:.4f}, δ={res['delta']:+.3f}")
        for label in (ERR, SMOOTH):
            sub = df[df["cls"] == label]
            k = int((sub["idx_next3"] < 0).sum())
            print(f"  {label}：未来3日指数下跌 {k}/{len(sub)} = {k / len(sub):.1%}")
        print(f"\n{TAG} 判别力对照（AUC = 0.5 + |δ|/2，区分即错 vs 顺利）：")
        bf = full.iloc[0]
        print(f"  · 当日可观测最好的特征「{bf['name']}」：AUC {0.5 + abs(bf['effect']) / 2:.3f}"
              f"（δ={bf['effect']:+.3f}, q={bf['q']:.3f}）")
        if res:
            print(f"  · 【非特征/事后归因】未来 3 日指数涨跌：AUC {0.5 + abs(res['delta']) / 2:.3f}"
                  f"（δ={res['delta']:+.3f}, p={res['p']:.4f}）")
            print("    → 差异主要写在「买完之后的市场走势」里，而不在「买入当天能看到的股票特征」里")

        # ---------- Part 9 ----------
        hr("Part 9  子样本特征（换手率/市值；daily_basic 只有 09-01 起 202 只）")
        sub = df[df["has_turnover"]]
        print(f"{TAG} 子样本 {len(sub)} 笔 (即错 {int((sub['cls'] == ERR).sum())}, "
              f"顺利 {int((sub['cls'] == SMOOTH).sum())})")
        if len(sub) >= 20:
            for k, nm in (("turnover_rate", "当日换手率"), ("log_mv", "对数总市值")):
                res = mwu(sub.loc[sub["cls"] == ERR, k].to_numpy(float),
                          sub.loc[sub["cls"] == SMOOTH, k].to_numpy(float))
                if res is None:
                    print(f"  {nm}: 有效样本不足，跳过")
                    continue
                print(f"  {nm}: 即错中位 {res['med1']:.4g} vs 顺利 {res['med2']:.4g}，"
                      f"p={res['p']:.4f} δ={res['delta']:+.3f}（n={res['n1']} vs {res['n2']}，"
                      f"可检出最小 δ≈{mde_cliff(res['n1'], res['n2']):.2f}）")
        else:
            print("  样本量 < 20，不做检验（仅登记，不构成任何结论）")

        # ---------- Part 10 ----------
        hr("Part 10  关键结论数（供报告取数）")
        print(f"① 买入级 BH-FDR(q<0.05)：{n_sig_q} 个 / {len(full)}；名义 p<0.05：{n_sig_p} 个；"
              f"Bonferroni：{n_sig_bonf} 个；最小 q={full['q'].min():.4f}（{full.iloc[0]['name']}）")
        for _, r in full[full["q"] < 0.05].iterrows():
            print(f"   - {r['name']}（{r['feature']}）p={r['p']:.4f} q={r['q']:.4f} "
                  f"{r['effect_name']}={r['effect']:+.3f}")
        print(f"② 日期层（市场类 8 个特征，n_dates={int(mk_res['n_dates'].max()) if len(mk_res) else 0}）："
              f"FDR 显著 {n_mk_sig} 个；最小 p="
              f"{mk_res['p'].min():.4f}（{mk_res.iloc[0]['name']}）" if len(mk_res) else "② 日期层：样本不足")
        sign_summary = [(NAME_OF[s["feature"]], f"{s['k_pos']}/{s['n_dates']}", round(s["p"], 4))
                        for s in sign_rows]
        print(f"③ 日内配对符号检验（个股 top5）：{sign_summary}")
        print(f"④ 随机游走基准（同波动率+市场漂移）：{p_rw.mean():.1%}；实际标签口径 "
              f"{(df['cls'] == ERR).mean():.1%}、收盘口径 {close_err_rate:.1%} → "
              f"基准可解释 ≈ {min(1.0, p_rw.mean() / max(close_err_rate, 1e-9)):.0%}")
        print(f"⑤ 换标签（收盘口径重分类，即错 {int((cls_alt == ERR).sum())} 笔）后："
              f"FDR 显著 {n_alt_sig} 个、名义 {int((alt_res['p'] < 0.05).sum())} 个；"
              f"前 5 名全是波动率类：{alt_res.head(5)['name'].tolist()}")
        print(f"⑥ 低波动档过滤（s_vol20≤P40={p40:.4f}）：n={r_lv['n']} 即错率 "
              f"{r_lv['err_rate']:.1%} 胜率 {r_lv['win_rate']:.1%} "
              f"CI[{r_lv['win_ci'][0]:.1%},{r_lv['win_ci'][1]:.1%}] EV {r_lv['ev']:+.2f}")
        print(f"⑦ 任务书口径过滤（样本内选特征）：")
        for r in results:
            print(f"   {r['tag']}: n={r['n']} 即错率 {fmt_pct(r['err_rate'])} "
                  f"胜率 {fmt_pct(r['win_rate'])} "
                  f"CI[{fmt_pct(r['win_ci'][0])},{fmt_pct(r['win_ci'][1])}] EV {r['ev']:+.2f}")
        if merged:
            print(f"⑧ 样本内 top5 在样本外方向一致 {sum(1 for m in merged if m['dir_same'])}/{len(merged)}；"
                  f"样本外 q<0.05 {int((oos_res['q'] < 0.05).sum())} / 名义 p<0.05 "
                  f"{int((oos_res['p'] < 0.05).sum())}（样本外仅 "
                  f"{oos_df['buy_date'].nunique()} 个买入日）")
        if len(h1_res) and len(h2_res):
            print(f"⑨ OOS 内再切（10-01~10-16 定 → 10-19~10-23 验）：前半 FDR "
                  f"{int((h1_res['q'] < 0.05).sum())} 个 → 后半 FDR "
                  f"{int((h2_res['q'] < 0.05).sum())} 个、名义 "
                  f"{int((h2_res['p'] < 0.05).sum())} 个；方向一致 {n_ok}/5")
        print(f"\n{TAG} 脚本正常结束；以上所有数字均来自本次运行输出")
    finally:
        try:
            store.close()
        except Exception:
            pass


if __name__ == "__main__":
    main()

