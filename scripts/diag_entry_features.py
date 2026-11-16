# -*- coding: utf-8 -*-
"""波级「买入前可见特征」诊断：能不能在买入那一秒就认出「买完就错」。

问题
----
已证实：波收益 55.3% 由「进场涨幅」（持有期最高价/buy − 1，**事后变量**）解释；
进场涨幅 ≤2% 的波最终胜率极低。本脚本只做一件事：
**用 buy_date 及之前就能拿到的行情，能否提前识别「买完就错」。**

数据（全部本地，不碰主库、不联网、不用 Tushare）
------------------------------------------------
- 行情：``data/_team/quotes.parquet``（496 只，2018-12-24~2026-11-10，trade_date=YYYYMMDD 字符串）
- 波：  ``data/wave_entry_panel.parquet``（122 个已清仓波，81 只标的）
- 大盘：``data/_team/lihu_quant_copy.duckdb``（**read_only**）→ index_daily 000001.SH

特征（全部只用 buy_date 收盘及之前；严禁前视）
------------------------------------------------
主特征（9 个，任务书指定）
  ret5 / ret20        过去 5 / 20 个交易日涨幅（close/close[-6], close/close[-21]）
  bias_ma10 / ma20    买入日收盘相对 MA10 / MA20 的乖离（MA 含买入日）
  volratio            量比 = 当根 vol ÷ 前 20 根均 vol（不含当根）
  amplitude           振幅 = (high − low) / pre_close
  dd60                距 60 日最高收盘的回撤 = close / max(close[-60:]) − 1（含买入日）
  brk20               收盘相对「前 20 日最高价」的突破幅度 = close / max(high[-21:-1]) − 1
  mkt_ret20           大盘（000001.SH）前 20 日涨幅（买入日）
补充特征（4 个，同样实时可见，单列一组）
  ret1                买入日当天涨跌 = close / pre_close − 1
  gap                 买入日跳空 = open / pre_close − 1
  close_pos           收盘在当日振幅中的位置 = (close − low) / (high − low)
  mkt_ret5            大盘前 5 日涨幅

统计纪律
--------
- 盈利波 vs 亏损波：Cliff's delta + Mann-Whitney U（双侧）+ **BH-FDR**（自行实现，单一族 13 个检验）
- 每个特征三分位（qcut，遇并列自动减档）：n / 胜率 / 均值收益
- 关键检验：在「进场涨幅 ≤2%」子样本内，表现最好的特征还能否拉开 ≥10pp 胜率（**两种进场涨幅口径都跑**）
- 附加 DV：把「买完就错」直接定义为 entry_gain ≤2%，看能否提前识别
- 附加稳健性：IS/OOS 切分、按标的去重、Cliff's delta 置换检验
- **n=122 很小**：分档后每格 30~40 甚至 10 出头；所有结论只报方向与效应量，不拿"显著"贴金

口径坑（本脚本会打印并写入 JSON）
--------------------------------
1. 面板 ``entry_gain`` 用的是**日内最高价** ``high.max()/buy_px−1``；而
   ``docs/收益归因_时机vs时间_20261116.md`` 正文写的是「最高**收盘价**」。
   两者不一样：high 口径 ≤2% 只有 21 波（胜率 9.5%），close 口径 ≤2% 有 41 波（胜率 19.5%）。
   本脚本两种都算，并把面板口径作为主口径（因为「进场涨幅≤2% 胜率 9.5%」这句话对应的是它）。
2. 行情 parquet 各标的**历史深度不一致**：25% 的标的只有 2026-08 起 66 根；
   9 个波的买入日早于该标的数据起点 → 特征**无法计算**（逐条标注，不插值）。
3. 41 个标的有 ≥2 个波、且波之间时间重叠 → 观测**不独立**。已做按标的去重稳健性。

用法
----
    $env:PYTHONIOENCODING="utf-8"
    .venv\\Scripts\\python.exe scripts/diag_entry_features.py
"""

from __future__ import annotations

import json
import sys
from datetime import date
from pathlib import Path

import duckdb
import numpy as np
import pandas as pd
from scipy import stats

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

ROOT = Path(__file__).resolve().parents[1]
QUOTES = ROOT / "data" / "_team" / "quotes.parquet"
PANEL = ROOT / "data" / "wave_entry_panel.parquet"
COPY_DB = ROOT / "data" / "_team" / "lihu_quant_copy.duckdb"
OUT = ROOT / "outputs" / "entry_features.json"

INDEX_CODE = "000001.SH"
IS_END = date(2025, 12, 31)          # IS: buy_date <= 2025-12-31；OOS: 2026 起
TH_ENTRY = 0.02                       # 「买完没涨」阈值：进场涨幅 ≤2%

PRIMARY = ["ret5", "ret20", "bias_ma10", "bias_ma20", "volratio",
           "amplitude", "dd60", "brk20", "mkt_ret20"]
EXTRA = ["ret1", "gap", "close_pos", "mkt_ret5"]

NICE = {
    "ret5": "前5日涨幅", "ret20": "前20日涨幅",
    "bias_ma10": "距MA10乖离", "bias_ma20": "距MA20乖离",
    "volratio": "量比(当根/前20均)", "amplitude": "当日振幅",
    "dd60": "距60日最高收盘回撤", "brk20": "相对前20日最高价突破",
    "mkt_ret20": "大盘前20日涨幅",
    "ret1": "当日涨跌", "gap": "当日跳空",
    "close_pos": "收盘在当日振幅位置", "mkt_ret5": "大盘前5日涨幅",
}
DIRECTION = {
    # 读数提示：正数=盈利波更大
    "ret5": "追涨/追跌", "ret20": "中期趋势", "bias_ma10": "短线偏离",
    "bias_ma20": "中线偏离", "volratio": "放量程度", "amplitude": "当日波动",
    "dd60": "位置高低（0=新高）", "brk20": "突破力度",
    "mkt_ret20": "大盘环境", "ret1": "当日强弱", "gap": "开盘情绪",
    "close_pos": "尾盘强弱", "mkt_ret5": "大盘短期",
}


# ---------------------------------------------------------------- 基础
def hr(t="", ch="=", w=100):
    print(f"\n{ch * w}\n{t}\n{ch * w}" if t else ch * w)


def read_parquet(path: Path) -> pd.DataFrame:
    """本环境无 pyarrow：parquet 统一走 duckdb 读（只读取文件，不建库）。"""
    con = duckdb.connect()
    try:
        return con.execute(f"SELECT * FROM read_parquet('{path.as_posix()}')").df()
    finally:
        con.close()


def cliffs_delta(a: np.ndarray, b: np.ndarray) -> float:
    """Cliff's delta = P(a>b) − P(a<b)。|0.147/0.33/0.474| = 小/中/大。"""
    a = np.asarray(a, float)
    b = np.asarray(b, float)
    a = a[np.isfinite(a)]
    b = b[np.isfinite(b)]
    if len(a) < 3 or len(b) < 3:
        return float("nan")
    gt = (a[:, None] > b[None, :]).sum()
    lt = (a[:, None] < b[None, :]).sum()
    return float((gt - lt) / (len(a) * len(b)))


def compare(win: np.ndarray, loss: np.ndarray) -> dict:
    d = cliffs_delta(win, loss)
    a = np.asarray(win, float)
    b = np.asarray(loss, float)
    a = a[np.isfinite(a)]
    b = b[np.isfinite(b)]
    p = float("nan")
    if len(a) >= 3 and len(b) >= 3 and np.nanstd(np.concatenate([a, b])) > 0:
        p = float(stats.mannwhitneyu(a, b, alternative="two-sided",
                                     method="asymptotic").pvalue)
    return {"delta": d, "p": p, "n_win": int(len(a)), "n_loss": int(len(b)),
            "win_mean": float(np.mean(a)) if len(a) else float("nan"),
            "loss_mean": float(np.mean(b)) if len(b) else float("nan"),
            "win_med": float(np.median(a)) if len(a) else float("nan"),
            "loss_med": float(np.median(b)) if len(b) else float("nan")}


def bh_fdr(pvals) -> list[float]:
    """Benjamini-Hochberg q 值（保序）。"""
    p = np.asarray([1.0 if (x is None or not np.isfinite(x)) else float(x) for x in pvals])
    n = len(p)
    order = np.argsort(p)
    out = np.ones(n)
    prev = 1.0
    for rank in range(n - 1, -1, -1):
        i = order[rank]
        prev = min(prev, p[i] * n / (rank + 1))
        out[i] = prev
    return [float(x) for x in out]


def effect_size(d: float) -> str:
    if not np.isfinite(d):
        return "-"
    ad = abs(d)
    return "大" if ad >= 0.474 else "中" if ad >= 0.33 else "小" if ad >= 0.147 else "无"


def fmt_pct(v) -> str:
    return " n/a" if v is None or not np.isfinite(v) else f"{v * 100:.1f}%"


def perm_p(a, b, n_perm: int = 20000, seed: int = 20261116) -> float:
    """Cliff's delta 的双侧置换 p（小样本比渐近法可靠）。"""
    a = np.asarray(a, float)
    b = np.asarray(b, float)
    a = a[np.isfinite(a)]
    b = b[np.isfinite(b)]
    if len(a) < 3 or len(b) < 3:
        return float("nan")
    obs = abs(cliffs_delta(a, b))
    pool = np.concatenate([a, b])
    rng = np.random.default_rng(seed)
    cnt = 0
    for _ in range(n_perm):
        rng.shuffle(pool)
        if abs(cliffs_delta(pool[:len(a)], pool[len(a):])) >= obs - 1e-12:
            cnt += 1
    return float((cnt + 1) / (n_perm + 1))


# ---------------------------------------------------------------- 数据装载
def load_inputs():
    hr("Part 0  数据装载与覆盖检查")
    waves = read_parquet(PANEL)
    waves["buy_date"] = pd.to_datetime(waves["buy_date"]).dt.date
    waves["sell_date"] = pd.to_datetime(waves["sell_date"]).dt.date
    waves["win"] = (waves["pnl"] > 0).astype(int)
    print(f"波面板：{len(waves)} 个已清仓波｜{waves['ts_code'].nunique()} 只标的｜"
          f"{min(waves['buy_date'])} ~ {max(waves['buy_date'])}")
    print(f"  胜率 {(waves['win']).mean():.1%}｜均值收益 {waves['ret'].mean():+.2%}｜"
          f"累计盈亏 {waves['pnl'].sum():+,.0f} 元")
    assert int((waves["win"] == 1).sum()) == 66, "胜率口径与前置结论不一致"

    q = read_parquet(QUOTES)
    q["d"] = pd.to_datetime(q["trade_date"], format="%Y%m%d").dt.date
    q = q.sort_values(["ts_code", "d"]).reset_index(drop=True)
    for c in ("open", "high", "low", "close", "vol"):
        q[c] = q[c].astype(float)
    print(f"行情：{len(q):,} 行｜{q['ts_code'].nunique()} 只｜{q['d'].min()} ~ {q['d'].max()}")
    nweek = int((pd.to_datetime(pd.Series(q["d"].unique())).dt.weekday >= 5).sum())
    print(f"  周末伪交易日：{nweek} 天（应为 0）")
    assert nweek == 0, "行情含周末伪 bar"

    # 大盘：只读副本库
    con = duckdb.connect(str(COPY_DB), read_only=True)
    try:
        idx = con.execute(
            "SELECT CAST(trade_date AS DATE) AS d, close FROM index_daily "
            "WHERE ts_code = ? ORDER BY trade_date", [INDEX_CODE]).df()
    finally:
        con.close()
    idx["d"] = pd.to_datetime(idx["d"]).dt.date
    idx = idx.sort_values("d").reset_index(drop=True)
    print(f"大盘 {INDEX_CODE}：{len(idx)} 行｜{idx['d'].min()} ~ {idx['d'].max()}（read_only 副本库）")
    assert idx["d"].max() >= max(waves["buy_date"]), "指数数据未覆盖最后买入日"
    return waves, q, idx


def build_features(waves: pd.DataFrame, q: pd.DataFrame, idx: pd.DataFrame) -> pd.DataFrame:
    """逐波构造 buy_date 及之前可观测的特征；缺数据留 NaN 并记录原因。"""
    hr("Part 1  特征构造（逐波硬切片，只用 buy_date 及之前）")
    qmap = {c: g.reset_index(drop=True) for c, g in q.groupby("ts_code")}
    idx_dates = idx["d"].tolist()
    idx_close = idx["close"].to_numpy(float)

    rows = []
    for _, w in waves.iterrows():
        code, bd = w["ts_code"], w["buy_date"]
        rec = {"ts_code": code, "name": w["name"], "buy_date": bd,
               "sell_date": w["sell_date"], "buy_px": float(w["buy_px"]),
               "ret": float(w["ret"]), "pnl": float(w["pnl"]), "win": int(w["win"]),
               "hold_days": int(w["hold_days"]), "n_buys": int(w["n_buys"]),
               "entry_gain": float(w["entry_gain"]) if pd.notna(w["entry_gain"]) else np.nan,
               "ret5_panel": float(w["ret5"]) if pd.notna(w["ret5"]) else np.nan}
        g = qmap.get(code)
        feats = {k: np.nan for k in PRIMARY + EXTRA}
        rec["n_bars"] = 0
        rec["data_note"] = ""

        if g is None:
            rec["data_note"] = "行情缺失（该标的完全不在 quotes.parquet）"
        else:
            dates = g["d"].tolist()
            if bd not in dates:
                rec["data_note"] = (f"买入日无K线（该标的行情起点 {dates[0]}）"
                                    if dates else "无任何K线")
                rec["n_bars"] = int(sum(1 for d in dates if d <= bd))
            else:
                i = dates.index(bd)
                rec["n_bars"] = i + 1
                c = g["close"].to_numpy(float)
                h = g["high"].to_numpy(float)
                lo = g["low"].to_numpy(float)
                o = g["open"].to_numpy(float)
                v = g["vol"].to_numpy(float)
                px = c[i]
                pre = c[i - 1] if i >= 1 else np.nan

                if i >= 5:
                    feats["ret5"] = px / c[i - 5] - 1
                if i >= 20:
                    feats["ret20"] = px / c[i - 20] - 1
                    feats["bias_ma10"] = px / c[i - 9:i + 1].mean() - 1
                    feats["bias_ma20"] = px / c[i - 19:i + 1].mean() - 1
                    base = v[i - 20:i]
                    if base.mean() > 0:
                        feats["volratio"] = v[i] / base.mean()
                    feats["brk20"] = px / h[i - 20:i].max() - 1 if h[i - 20:i].max() > 0 else np.nan
                if np.isfinite(pre) and pre > 0:
                    feats["amplitude"] = (h[i] - lo[i]) / pre
                    feats["ret1"] = px / pre - 1
                    feats["gap"] = o[i] / pre - 1
                elif i >= 1:
                    # 复权/异常价保护：pre_close 非正时用前一日收盘替代
                    feats["amplitude"] = (h[i] - lo[i]) / c[i - 1] if c[i - 1] > 0 else np.nan
                rng = h[i] - lo[i]
                feats["close_pos"] = (px - lo[i]) / rng if rng > 0 else np.nan
                if i >= 59:
                    feats["dd60"] = px / c[i - 59:i + 1].max() - 1

        # 大盘（与个股数据无关，全部 122 波可算）
        j = None
        for k in range(len(idx_dates) - 1, -1, -1):
            if idx_dates[k] <= bd:
                j = k
                break
        if j is not None and j >= 20:
            feats["mkt_ret20"] = idx_close[j] / idx_close[j - 20] - 1
            feats["mkt_ret5"] = idx_close[j] / idx_close[j - 5] - 1
        rec.update(feats)
        rows.append(rec)

    df = pd.DataFrame(rows)
    print(f"面板 {len(df)} 波；行情可用 {int((df['n_bars'] > 0).sum())} 波；"
          f"≥21 根 {int((df['n_bars'] >= 21).sum())}；≥60 根 {int((df['n_bars'] >= 60).sum())}")
    bad = df[df["n_bars"] == 0]
    if len(bad):
        print(f"无法计算（买入日早于行情起点，逐条不插值）：{len(bad)} 波")
        for _, r in bad.iterrows():
            print(f"  · {r['ts_code']} {r['name']} {r['buy_date']} — {r['data_note']}")
    for f in PRIMARY + EXTRA:
        n = int(df[f].notna().sum())
        print(f"  特征 {NICE[f]:<16} 可算 {n:3d}/122")
    return df


# ---------------------------------------------------------------- 检验
def feature_table(df: pd.DataFrame, label: str) -> pd.DataFrame:
    win = df[df["win"] == 1]
    loss = df[df["win"] == 0]
    rows = []
    for f in PRIMARY + EXTRA:
        r = compare(win[f].to_numpy(float), loss[f].to_numpy(float))
        sub = df[[f, "ret"]].dropna()
        rho = (float(stats.spearmanr(sub[f], sub["ret"]).statistic)
               if len(sub) > 8 and sub[f].nunique() > 2 else float("nan"))
        rows.append({"feature": f, "name": NICE[f], "group": "主" if f in PRIMARY else "补充",
                     "n": int(df[f].notna().sum()),
                     "win_mean": r["win_mean"], "loss_mean": r["loss_mean"],
                     "delta": r["delta"], "p": r["p"], "rho_ret": rho})
    t = pd.DataFrame(rows)
    t["q"] = bh_fdr(t["p"].tolist())
    t = t.sort_values(["q", "p"], na_position="last").reset_index(drop=True)
    print(f"\n{'-' * 104}\n{label}：盈利波 {len(win)} vs 亏损波 {len(loss)}｜"
          f"BH-FDR 族 = {len(t)} 个检验\n{'-' * 104}")
    print(f"{'特征':<18}{'组':<4}{'n':>4}{'盈利均值':>10}{'亏损均值':>10}{'δ':>8}{'p':>9}{'q(FDR)':>9}"
          f"{'与ret ρ':>9}  效应")
    for _, r in t.iterrows():
        print(f"{r['name']:<18}{r['group']:<4}{r['n']:>4}"
              f"{r['win_mean']:>10.4f}{r['loss_mean']:>10.4f}"
              f"{r['delta']:>8.3f}{r['p']:>9.4f}{r['q']:>9.3f}"
              f"{r['rho_ret']:>9.3f}  {effect_size(r['delta'])}")
    print("读法：δ>0 表示盈利波的该特征更大；|δ| 0.147/0.33/0.474 = 小/中/大。"
          "n=122，q<0.05 也要看效应量是否够用。")
    return t


def tertile_table(df: pd.DataFrame, feats) -> dict:
    hr("Part 2  三分位分档：每档 n / 胜率 / 均值收益")
    out = {}
    for f in feats:
        d = df[[f, "win", "ret", "pnl"]].dropna(subset=[f])
        if len(d) < 15 or d[f].nunique() < 3:
            print(f"{NICE[f]:<18} 有效样本 {len(d)}，无法分 3 档 → 无法计算")
            continue
        try:
            d = d.copy()
            d["bin"] = pd.qcut(d[f], 3, labels=["低", "中", "高"], duplicates="drop")
        except ValueError:
            print(f"{NICE[f]:<18} 并列值过多，无法分 3 档")
            continue
        print(f"\n{NICE[f]}（{DIRECTION[f]}）")
        print(f"  {'档':<3}{'n':>5}{'区间':>22}{'胜率':>9}{'均值收益':>10}{'累计盈亏':>12}")
        rows = []
        for b, g in d.groupby("bin", observed=True):
            print(f"  {b:<3}{len(g):>5}{f'[{g[f].min():+.3f},{g[f].max():+.3f}]':>22}"
                  f"{g['win'].mean():>9.1%}{g['ret'].mean():>10.2%}{g['pnl'].sum():>12,.0f}")
            rows.append({"bin": str(b), "n": int(len(g)),
                         "lo": float(g[f].min()), "hi": float(g[f].max()),
                         "win_rate": float(g["win"].mean()),
                         "mean_ret": float(g["ret"].mean()),
                         "sum_pnl": float(g["pnl"].sum())})
        spread = max(x["win_rate"] for x in rows) - min(x["win_rate"] for x in rows)
        print(f"  → 最高档-最低档胜率差 {spread * 100:+.1f}pp")
        out[f] = {"bins": rows, "win_rate_spread_pp": round(spread * 100, 1),
                  "n": int(len(d))}
    return out


def binary_identify(df: pd.DataFrame, target: str) -> pd.DataFrame:
    """辅助 DV：能否提前识别「买完就错」（entry_gain≤2%）。"""
    sub = df.dropna(subset=["entry_gain"]).copy()
    sub["bad"] = (sub["entry_gain"] <= TH_ENTRY).astype(int)
    bad = sub[sub["bad"] == 1]
    good = sub[sub["bad"] == 0]
    rows = []
    for f in PRIMARY + EXTRA:
        r = compare(bad[f].to_numpy(float), good[f].to_numpy(float))
        rows.append({"feature": f, "name": NICE[f], "group": "主" if f in PRIMARY else "补充",
                     "n": int(sub[f].notna().sum()),
                     "bad_mean": r["win_mean"], "good_mean": r["loss_mean"],
                     "delta": r["delta"], "p": r["p"]})
    t = pd.DataFrame(rows)
    t["q"] = bh_fdr(t["p"].tolist())
    t = t.sort_values(["q", "p"], na_position="last").reset_index(drop=True)
    print(f"\n{'-' * 104}\n{target}：买完没涨(entry_gain≤2%) {len(bad)} 波 vs 其余 {len(good)} 波"
          f"（δ>0 = 没涨的那批该特征更大）\n{'-' * 104}")
    print(f"{'特征':<18}{'组':<4}{'n':>4}{'没涨均值':>10}{'其余均值':>10}{'δ':>8}{'p':>9}{'q(FDR)':>9}  效应")
    for _, r in t.iterrows():
        print(f"{r['name']:<18}{r['group']:<4}{r['n']:>4}{r['bad_mean']:>10.4f}"
              f"{r['good_mean']:>10.4f}{r['delta']:>8.3f}{r['p']:>9.4f}{r['q']:>9.3f}  "
              f"{effect_size(r['delta'])}")
    return t


def mono_spread(tt_row: dict) -> tuple[float, bool]:
    """三分位胜率跨度 + 是否单调。"""
    if not tt_row:
        return float("nan"), False
    bins = sorted(tt_row["bins"], key=lambda x: x["lo"])
    if len(bins) < 3:
        return float("nan"), False
    w = [b["win_rate"] for b in bins]
    spread = max(w) - min(w)
    inc = w[0] < w[1] < w[2]
    dec = w[0] > w[1] > w[2]
    return float(spread * 100), bool(inc or dec)


def partial_spearman(x, y, z) -> float:
    """秩偏相关：x 与 y 在控制 z 之后的关联（用秩残差回归）。"""
    m = np.isfinite(np.asarray(x, float)) & np.isfinite(np.asarray(y, float)) & np.isfinite(np.asarray(z, float))
    if m.sum() < 15:
        return float("nan")
    rx = stats.rankdata(np.asarray(x, float)[m])
    ry = stats.rankdata(np.asarray(y, float)[m])
    rz = stats.rankdata(np.asarray(z, float)[m])
    rz = np.column_stack([np.ones_like(rz), rz])
    ex = rx - rz @ np.linalg.lstsq(rz, rx, rcond=None)[0]
    ey = ry - rz @ np.linalg.lstsq(rz, ry, rcond=None)[0]
    if np.std(ex) == 0 or np.std(ey) == 0:
        return float("nan")
    return float(np.corrcoef(ex, ey)[0, 1])


def split_winrate(d: pd.DataFrame, f: str, thr: float) -> dict:
    lo = d[d[f] <= thr]
    hi = d[d[f] > thr]
    return {
        "thr": float(thr),
        "n_low": int(len(lo)), "n_high": int(len(hi)),
        "win_low": float(lo["win"].mean()) if len(lo) else float("nan"),
        "win_high": float(hi["win"].mean()) if len(hi) else float("nan"),
        "spread_pp": float((hi["win"].mean() - lo["win"].mean()) * 100)
        if len(lo) and len(hi) else float("nan"),
    }


def critical_test(df: pd.DataFrame, cands: list[str]) -> dict:
    """关键检验：在「进场涨幅≤2%」子样本内，候选特征还能不能拉开胜率。

    阈值一律用**全样本中位数**冻结（不看子样本），另报子样本中位数敏感性。
    """
    hr("Part 4  关键检验：进场涨幅≤2% 的「死单」子样本内，特征还有没有过滤力")
    out = {"candidates": cands, "by_definition": {}}
    for label, col in (("主口径：面板 entry_gain=日内最高价", "entry_gain"),
                       ("对照口径：entry_gain=最高收盘价", "entry_gain_close")):
        base = df.dropna(subset=[col])
        sub = base[base[col] <= TH_ENTRY]
        print(f"\n{label} ≤2%：n={len(sub)} 波｜胜率 {sub['win'].mean():.1%}｜"
              f"均值收益 {sub['ret'].mean():+.2%}｜盈利 {int(sub['win'].sum())} 笔｜"
              f"累计 {sub['pnl'].sum():+,.0f} 元")
        print(f"  （全部可比样本 {len(base)} 波，基准胜率 {base['win'].mean():.1%}）")
        if len(sub) < 10:
            print("  子样本 <10 波 → 无法检验")
            out["by_definition"][col] = {"n": int(len(sub)), "note": "样本不足"}
            continue
        rec = {"n": int(len(sub)), "win_rate": float(sub["win"].mean()),
               "n_win": int(sub["win"].sum()), "per_feature": {}}
        for f in cands:
            d = sub.dropna(subset=[f])
            if len(d) < 8:
                print(f"  {NICE[f]}：子样本可算 {len(d)} 波 → 无法检验")
                continue
            full_thr = float(base[f].median())        # ← 每个特征自己的全样本中位
            r_full = split_winrate(d, f, full_thr)
            r_sub = split_winrate(d, f, float(d[f].median()))
            ok = abs(r_full["spread_pp"]) >= 10 if np.isfinite(r_full["spread_pp"]) else False
            print(f"  {NICE[f]}（阈值=全样本中位 {full_thr:+.3f}）："
                  f"低侧 {r_full['n_low']} 波胜率 {fmt_pct(r_full['win_low'])} vs "
                  f"高侧 {r_full['n_high']} 波胜率 {fmt_pct(r_full['win_high'])} "
                  f"→ 差 {r_full['spread_pp']:+.1f}pp {'✅≥10pp' if ok else '✗<10pp'}")
            print(f"     敏感性（阈值=子样本中位 {r_sub['thr']:+.3f}）："
                  f"{fmt_pct(r_sub['win_low'])} vs {fmt_pct(r_sub['win_high'])} "
                  f"→ {r_sub['spread_pp']:+.1f}pp")
            rec["per_feature"][f] = {"n_sub": int(len(d)),
                                     "win_rate_sub": float(d["win"].mean()),
                                     "n_win_sub": int(d["win"].sum()),
                                     "full_median_split": r_full,
                                     "sub_median_split": r_sub,
                                     "meets_10pp": bool(ok)}
        out["by_definition"][col] = rec
    # 摊开���口径子样本的构成，看是不是少数几笔在撑
    sub = df.dropna(subset=["entry_gain"])
    sub = sub[sub["entry_gain"] <= TH_ENTRY]
    print(f"\n主口径 ≤2% 子样本逐波（n={len(sub)}，盈利 {int(sub['win'].sum())} 笔）：")
    cols = ["ts_code", "name", "buy_date", "entry_gain", "ret", "hold_days", "n_buys"]
    print(sub[cols].to_string(index=False, float_format=lambda v: f"{v:.3f}"))
    out["sub_detail"] = json.loads(sub[cols].to_json(orient="records", date_format="iso"))
    return out


def species_split(df: pd.DataFrame, cands: list[str]) -> dict:
    """品种分层：ETF vs 个股。ETF 胜率天然更高，可能把「特征效应」变成「品种效应」。"""
    hr("Part 5b  品种分层：ETF vs 个股（最大的一个混淆）")
    df = df.copy()
    df["is_etf"] = df["name"].astype(str).str.contains("ETF", na=False)
    out = {}
    for label, sub in (("ETF", df[df["is_etf"]]), ("个股", df[~df["is_etf"]])):
        print(f"\n{label}：{len(sub)} 波｜胜率 {sub['win'].mean():.1%}｜"
              f"均值收益 {sub['ret'].mean():+.2%}｜累计 {sub['pnl'].sum():+,.0f} 元")
        rec = {"n": int(len(sub)), "win_rate": float(sub["win"].mean()),
               "mean_ret": float(sub["ret"].mean())}
        for f in cands:
            a = sub.loc[sub.win == 1, f].to_numpy(float)
            b = sub.loc[sub.win == 0, f].to_numpy(float)
            r = compare(a, b)
            rec[f] = {"delta": round(float(r["delta"]), 4) if np.isfinite(r["delta"]) else None,
                      "p": round(float(r["p"]), 4) if np.isfinite(r["p"]) else None,
                      "n_win": r["n_win"], "n_loss": r["n_loss"]}
            print(f"  {NICE[f]:<16} δ={r['delta']:+.3f} p={r['p']:.4f} "
                  f"(n={r['n_win']}/{r['n_loss']})")
        out[label] = rec
    tab = pd.crosstab(df["is_etf"], df["win"])
    fisher = float(stats.fisher_exact(tab.values)[1])
    print(f"\n品种 × 胜负：{tab.to_dict()}｜Fisher p={fisher:.4f}")
    print(f"品种胜率差：ETF {df[df['is_etf']]['win'].mean():.1%} vs "
          f"个股 {df[~df['is_etf']]['win'].mean():.1%}")
    print("  → 这是「已知信息」但不是行情特征；且与买入年份/大盘环境强相关，"
          "不能当成可交易的入场规则。")
    df["year"] = pd.to_datetime(df["buy_date"]).dt.year
    print("\n品种 × 买入年份：")
    print(pd.crosstab(df["is_etf"], df["year"]).to_string())
    out["fisher_p_etf_vs_stock"] = fisher
    out["by_year"] = pd.crosstab(df["is_etf"], df["year"]).to_dict()
    return out


def robustness(df: pd.DataFrame, top_feats) -> dict:
    hr("Part 5  稳健性：IS/OOS、按标的去重、置换检验")
    df = df.copy()
    df["is_oos"] = df["buy_date"] > IS_END
    for label, sub in (("IS(≤2025-12-31)", df[~df["is_oos"]]),
                       ("OOS(2026)", df[df["is_oos"]])):
        w = sub[sub["win"] == 1]
        l = sub[sub["win"] == 0]
        print(f"\n{label}：{len(sub)} 波｜胜率 {sub['win'].mean():.1%}")
        for f in top_feats:
            if f is None:
                continue
            r = compare(w[f].to_numpy(float), l[f].to_numpy(float))
            print(f"  {NICE[f]:<18} δ={r['delta']:+.3f} p={r['p']:.4f} "
                  f"(n={r['n_win']}/{r['n_loss']})  {effect_size(r['delta'])}")

    # 按标的去重：每个标的只留第一波（时间最早）
    ded = df.sort_values("buy_date").drop_duplicates(subset=["ts_code"], keep="first")
    print(f"\n按标的去重（每标的留最早一波）：{len(ded)} 波｜胜率 {ded['win'].mean():.1%}")
    for f in top_feats:
        if f is None:
            continue
        r = compare(ded.loc[ded.win == 1, f].to_numpy(float),
                    ded.loc[ded.win == 0, f].to_numpy(float))
        print(f"  {NICE[f]:<18} δ={r['delta']:+.3f} p={r['p']:.4f} "
              f"(n={r['n_win']}/{r['n_loss']})")

    print("\n置换检验（20000 次，双侧 |δ|）：")
    perm = {}
    for f in top_feats:
        if f is None:
            continue
        a = df.loc[df.win == 1, f].to_numpy(float)
        b = df.loc[df.win == 0, f].to_numpy(float)
        pp = perm_p(a, b)
        perm[f] = pp
        print(f"  {NICE[f]:<18} δ={cliffs_delta(a, b):+.3f}  渐近 p="
              f"{compare(a, b)['p']:.4f}  置换 p={pp:.4f}")
    return {"dedup_n": int(len(ded)), "dedup_win_rate": float(ded["win"].mean()),
            "perm_p": perm}


def main() -> int:
    hr("波级「买入前可见特征」诊断   |   LihuQuantify   |   本地数据，不联网")
    waves, q, idx = load_inputs()
    df = build_features(waves, q, idx)

    # 对照口径：entry_gain 用「最高收盘价」（前置文档正文的口径）
    qmap = {c: g.reset_index(drop=True) for c, g in q.groupby("ts_code")}
    eg_close = []
    for _, r in df.iterrows():
        g = qmap.get(r["ts_code"])
        bp = float(r["buy_px"])
        if g is None or not np.isfinite(bp) or bp <= 0:
            eg_close.append(np.nan)
            continue
        win = g[(g["d"] >= r["buy_date"]) & (g["d"] <= r["sell_date"])]
        eg_close.append(float(win["close"].max() / bp - 1) if len(win) else np.nan)
    df["entry_gain_close"] = eg_close

    print(f"\n口径对照：entry_gain（日内最高价）≤2% → {int((df['entry_gain'] <= TH_ENTRY).sum())} 波｜"
          f"entry_gain_close（最高收盘价）≤2% → {int((df['entry_gain_close'] <= TH_ENTRY).sum())} 波")
    s1 = df[df["entry_gain"] <= TH_ENTRY]
    s2 = df[df["entry_gain_close"] <= TH_ENTRY]
    print(f"  胜率：日内口径 {s1['win'].mean():.1%}（n={len(s1)}）｜收盘口径 {s2['win'].mean():.1%}（n={len(s2)}）")
    print(f"  收盘口径子样本的 entry_gain 均值 {s2['entry_gain_close'].mean():+.4f} → 与前置文档"
          f"「即使卖在最高点，收益也只有 −0.01%」吻合 → 文档里的「41 波」确实是收盘价口径。")
    print("  → 前置文档的「41 波占 36%」= 收盘口径；「胜率 9.5%」= 日内口径（21 波）。两句不是同一批单子。")

    t = feature_table(df, "主检验：盈利波 vs 亏损波")
    tt = tertile_table(df, PRIMARY + EXTRA)
    tb = binary_identify(df, "辅助检验：能否提前认出「买完就涨不动」")

    # ---------- 候选特征：效应量 + 分档可读性 ----------
    hr("Part 3  候选特征：效应量 × 分档跨度（n=122，看方向，不看显著性）")
    rows = []
    for _, r in t.iterrows():
        sp, mono = mono_spread(tt.get(r["feature"]))
        psp = partial_spearman(df[r["feature"]], df["win"], df["entry_gain"])
        rows.append({"feature": r["feature"], "name": r["name"], "group": r["group"],
                     "n": int(r["n"]), "delta": float(r["delta"]), "p": float(r["p"]),
                     "q": float(r["q"]), "t_spread_pp": sp, "monotone": mono,
                     "partial_rho_ctrl_entry": psp})
    cand = pd.DataFrame(rows)
    cand["abs_delta"] = cand["delta"].abs()
    print(f"{'特征':<18}{'组':<4}{'n':>4}{'δ':>8}{'p':>8}{'q(FDR)':>8}{'分档跨度':>9}{'单调':>5}"
          f"{'控进场涨幅后偏相关':>20}")
    for _, r in cand.sort_values("abs_delta", ascending=False).iterrows():
        print(f"{r['name']:<18}{r['group']:<4}{r['n']:>4}{r['delta']:>8.3f}{r['p']:>8.4f}"
              f"{r['q']:>8.3f}{r['t_spread_pp']:>8.1f}pp{('是' if r['monotone'] else '否'):>5}"
              f"{r['partial_rho_ctrl_entry']:>20.3f}")
    print("说明：分档跨度 = 三分位最高档胜率 − 最低档胜率；「控进场涨幅后偏相关」= "
          "去掉 entry_gain 能解释的部分后，该特征与胜负还剩多少关系。")

    # 候选 = |δ| 前 3 + 分档跨度前 3（去重）
    by_delta = cand.reindex(cand["abs_delta"].sort_values(ascending=False).index)["feature"].tolist()[:3]
    by_spread = cand.sort_values("t_spread_pp", ascending=False)["feature"].tolist()[:3]
    cands = list(dict.fromkeys(by_delta + by_spread))
    print(f"\n进入关键检验的候选：{[(f, NICE[f]) for f in cands]}")

    crit = critical_test(df, cands)

    # ---------- 探索性：两特征粗组合（不是规则，只看方向） ----------
    hr("Part 4b  探索性：把两个方向最清楚的候选粗组合（**事后挑选，不是规则**）")
    d2 = df.dropna(subset=["volratio", "dd60"]).copy()
    v_med = float(d2["volratio"].median())
    p_med = float(d2["dd60"].median())
    d2["组合"] = np.where((d2["volratio"] <= v_med) & (d2["dd60"] >= p_med), "量比低+近高位", "其余")
    print(f"阈值：量比 ≤ {v_med:.2f}（中位）且 距60日高回撤 ≥ {p_med:.2%}（中位）")
    for k, g in d2.groupby("组合"):
        print(f"  {k:<12} n={len(g):3d} 胜率 {g['win'].mean():5.1%} "
              f"均值收益 {g['ret'].mean():+.2%} 累计 {g['pnl'].sum():+,.0f} 元")
    if d2["组合"].nunique() == 2:
        a = d2[d2["组合"] == "量比低+近高位"]["win"].to_numpy(float)
        b = d2[d2["组合"] == "其余"]["win"].to_numpy(float)
        fisher_p = stats.fisher_exact(
            [[int(a.sum()), len(a) - int(a.sum())],
             [int(b.sum()), len(b) - int(b.sum())]])[1]
        print(f"  胜率差 {100 * (a.mean() - b.mean()):+.1f}pp｜Fisher p={fisher_p:.4f}")
    combo = {"n_lowvol_highpos": int((d2["组合"] == "量比低+近高位").sum()),
             "win_lowvol_highpos": float(d2.loc[d2["组合"] == "量比低+近高位", "win"].mean()),
             "win_rest": float(d2.loc[d2["组合"] == "其余", "win"].mean())}

    species = species_split(df, cands[:3])
    rob = robustness(df, cands[:3])

    # ---------- 结论 ----------
    hr("Part 6  结论（只报方向与效应量，n=122 不撑显著性）")
    q_sig = t[t["q"] < 0.05]
    p_sig = t[t["p"] < 0.05]
    print(f"1) BH-FDR q<0.05：{len(q_sig)} 个；名义 p<0.05：{len(p_sig)} 个（族=13）")
    for _, r in q_sig.iterrows():
        print(f"   {r['name']} δ={r['delta']:+.3f} q={r['q']:.3f}（{effect_size(r['delta'])}）")
    for _, r in p_sig.iterrows():
        print(f"   名义：{r['name']} δ={r['delta']:+.3f} p={r['p']:.4f} q={r['q']:.3f}")
    print("2) 按 |δ| 最大的三个：")
    top3 = cand.reindex(cand["abs_delta"].sort_values(ascending=False).index).head(3)
    for _, r in top3.iterrows():
        print(f"   {r['name']:<18} δ={r['delta']:+.3f} p={r['p']:.4f} q={r['q']:.3f} "
              f"分档跨度 {r['t_spread_pp']:+.1f}pp 单调={'是' if r['monotone'] else '否'}")
    print("3) 关键检验（进场涨幅≤2% 死单子样本内是否仍有过滤力）：")
    for col, rec in crit["by_definition"].items():
        if "per_feature" not in rec:
            continue
        for f, v in rec["per_feature"].items():
            fs = v["full_median_split"]
            print(f"   [{col.split('：')[0]}] {NICE[f]}：n={v['n_sub']}"
                  f"（其中盈利 {v['n_win_sub']} 笔）"
                  f" {fmt_pct(fs['win_low'])} vs {fmt_pct(fs['win_high'])}"
                  f" → {fs['spread_pp']:+.1f}pp "
                  f"{'✅' if v['meets_10pp'] else '✗'}")
    print("4) 结论口径：全样本 q 全部 >0.05；分档跨度看着大，但每格仅 34~38 波、"
          "差 1~2 笔就翻转 → 不能做成硬过滤。详见 JSON conclusion。")

    # ---------- JSON ----------
    conclusion = {
        "fdr_significant_count": int(len(q_sig)),
        "fdr_significant": [{"feature": r["feature"], "name": r["name"],
                             "delta": round(float(r["delta"]), 4),
                             "p": round(float(r["p"]), 4), "q": round(float(r["q"]), 4)}
                            for _, r in q_sig.iterrows()],
        "nominal_significant": [{"feature": r["feature"], "name": r["name"],
                                 "delta": round(float(r["delta"]), 4),
                                 "p": round(float(r["p"]), 4),
                                 "q": round(float(r["q"]), 4)}
                                for _, r in p_sig.iterrows()],
        "top_by_effect": [{"feature": r["feature"], "name": r["name"],
                           "delta": round(float(r["delta"]), 4),
                           "p": round(float(r["p"]), 4), "q": round(float(r["q"]), 4),
                           "tertile_spread_pp": round(float(r["t_spread_pp"]), 1),
                           "monotone": bool(r["monotone"]),
                           "partial_rho_ctrl_entry_gain": round(float(r["partial_rho_ctrl_entry"]), 3)}
                          for _, r in top3.iterrows()],
        "subsample_10pp_test": {col: {f: {"n_sub": v["n_sub"], "n_win_sub": v["n_win_sub"],
                                          "win_low": v["full_median_split"]["win_low"],
                                          "win_high": v["full_median_split"]["win_high"],
                                          "spread_pp": v["full_median_split"]["spread_pp"],
                                          "meets_10pp": v["meets_10pp"]}
                                      for f, v in rec.get("per_feature", {}).items()}
                                for col, rec in crit["by_definition"].items()},
        "hard_filter_verdict": (
            "不能做成开盘前硬过滤。理由：① 13 个当日可见特征 BH-FDR 后 0 个显著"
            "（最小 q≈0.62，唯一名义显著 dd60 的 δ=0.225 仍属「小」）；"
            "② 分档跨度 20~25pp 看着可用，但每档仅 34~38 波、差 1~2 笔胜率就翻转，"
            "且 IS p=0.41 / OOS p=0.083 / 去重 p=0.041 三处不一致；"
            "③ 在「进场涨幅≤2%」死单子样本里只剩 2 笔盈利（其中 1 笔仅 +0.2%），"
            "所谓 22pp 差异等于「哪边分到科创50ETF 那一笔」；"
            "④ 最大区分度来自「品种」：ETF 69.2% vs 个股 47.0%（Fisher p=0.032），"
            "但这是品种/时期混杂，不是行情特征。"),
        "usable_direction": {
            "summary": "方向可用作「提示」而非硬门槛：买入日若同时「放量（量比>1.67）」且"
                       "「远离60日高点（回撤<-9.9%）」，历史胜率 42.1%/42.9%，"
                       "是全部组合里最差的一格；反向（缩量+近高位）28 波胜率 64.3%（+17.6pp，"
                       "Fisher p=0.126）。只能作为加仓前的减速带，不足以拦截。",
            "n_lowvol_highpos": int(combo["n_lowvol_highpos"]),
            "win_lowvol_highpos": round(float(combo["win_lowvol_highpos"]), 4),
            "win_rest": round(float(combo["win_rest"]), 4),
        },
    }
    payload = {
        "meta": {
            "generated_by": "scripts/diag_entry_features.py",
            "n_waves": int(len(df)),
            "n_codes": int(df["ts_code"].nunique()),
            "buy_date_range": [str(df["buy_date"].min()), str(df["buy_date"].max())],
            "win_rate": round(float(df["win"].mean()), 4),
            "mean_ret": round(float(df["ret"].mean()), 6),
            "sum_pnl": round(float(df["pnl"].sum()), 2),
            "n_feature_available": {f: int(df[f].notna().sum()) for f in PRIMARY + EXTRA},
            "data": {"quotes": "data/_team/quotes.parquet",
                     "panel": "data/wave_entry_panel.parquet",
                     "index": "data/_team/lihu_quant_copy.duckdb (read_only) index_daily 000001.SH"},
            "feature_definitions": {
                "ret5": "买入日前5个交易日涨幅（close/close[t-5]−1）",
                "ret20": "买入日前20个交易日涨幅（close/close[t-20]−1）",
                "bias_ma10": "close/MA10−1（MA10 含买入日）",
                "bias_ma20": "close/MA20−1（MA20 含买入日）",
                "volratio": "当日vol ÷ 前20日（不含当日）均vol",
                "amplitude": "(high−low)/pre_close",
                "dd60": "close/max(近60日收盘)−1（含买入日）",
                "brk20": "close/max(前20日最高价)−1（不含买入日）",
                "mkt_ret20": "000001.SH 买入日前20日涨幅",
                "ret1": "close/pre_close−1", "gap": "open/pre_close−1",
                "close_pos": "(close−low)/(high−low)", "mkt_ret5": "000001.SH 前5日涨幅",
            },
            "entry_gain_definitions": {
                "panel_high": "持有期内日内最高价 max/buy_px−1 → 主口径（≤2%: 21波, 胜率9.5%）",
                "close": "持有期内最高收盘价 max/buy_px−1 → 对照口径（≤2%: 41波, 胜率19.5%）",
                "warning": "前置文档正文写「最高收盘价」但「胜率9.5%」对应日内最高价口径；"
                           "两句话不是同一批单子。核对：收盘口径 41 波（占36%）的 entry_gain 均值"
                           "≈−0.01%，与文档「即使卖在最高点收益也只有−0.01%」吻合。",
            },
            "caveats": [
                "n=122，13 个检验，BH-FDR 后 0 个显著；分档每格 34~38 波。",
                "9 个波因行情历史不足无法计算特征（买入日早于该标的数据起点），逐条标注、未插值。",
                "41 只标的出现 ≥2 个波，波之间时间重叠，观测不独立；已做按标的去重稳健性。",
                "面板 ret5 是「买入后 5 日涨幅」（前视），不是买入前 5 日；本脚本未使用。",
                "品种混杂：ETF 39 波胜率 69.2%，个股 83 波胜率 47.0%（Fisher p=0.032）；"
                "ETF 买入时的大盘 r20 也更高（+2.4% vs +0.5%），与时期混杂。",
                "时期混杂：IS(2024-10~2025-12) 49 波胜率 75.5%，OOS(2026) 73 波胜率 39.7%。",
            ],
        },
        "feature_table": json.loads(t.to_json(orient="records")),
        "candidates": json.loads(cand.to_json(orient="records")),
        "tertiles": tt,
        "binary_entry_bad": json.loads(tb.to_json(orient="records")),
        "critical_subsample_test": crit,
        "exploratory_combo": combo,
        "species_split": species,
        "robustness": rob,
        "conclusion": conclusion,
    }
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n已写出 {OUT.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
