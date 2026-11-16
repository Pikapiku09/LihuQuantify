"""波级入场归因：基本面 / 估值 / 消息面 能否区分盈利波与亏损波。

对应预注册假设
--------------
- H-2026-11-16-003：基本面质量能区分盈利波与亏损波
  pass = OOS 段至少一个基本面因子 Cliff's delta >= 0.33 且 Mann-Whitney p < 0.05，
         且在「进场涨幅 <=2%」子样本中胜率差 >= 10pp
- H-2026-11-16-004：技术面好但基本面烂的波应回避（交互效应）
  pass = 「技术好+基本面好」与「技术好+基本面烂」的 OOS 胜率差 >= 10pp 且显著

纪律
----
- 全部 PIT 口径（见 build_wave_entry_panel.py 的铁律）
- IS/OOS 切分：IS = 2024-10-15~2025-12-31，OOS = 2026-01-01~2026-11-10
- 多因子比较做 BH-FDR 校正
- 样本量小（n=122）分箱后更小，结论只报方向与效应量，不吹显著性

用法
----
    python scripts/analyze_wave_fund.py
"""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd
from scipy import stats

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

ROOT = Path(__file__).resolve().parents[1]
PANEL = ROOT / "data" / "wave_entry_panel.parquet"

IS_END = "20251231"
FACTORS = ["entry_gain", "ret5", "roe", "np_yoy", "or_yoy", "gpm",
           "pb_pct3y", "pe_ttm", "total_mv", "fina_ann_days"]
FUNDAMENTAL = ["roe", "np_yoy", "or_yoy", "gpm", "pb_pct3y"]
NICE = {
    "entry_gain": "进场涨幅", "ret5": "买入后5日涨幅", "roe": "ROE",
    "np_yoy": "净利同比", "or_yoy": "营收同比", "gpm": "毛利率",
    "pb_pct3y": "PB三年分位", "pe_ttm": "PE_TTM", "total_mv": "总市值",
    "fina_ann_days": "财报公告后N天",
}


def read_panel(path: Path) -> pd.DataFrame:
    """本环境无 pyarrow，parquet 用 duckdb 读（与写入端一致）。"""
    if path.suffix == ".csv":
        return pd.read_csv(path)
    import duckdb

    con = duckdb.connect()
    df = con.execute(f"SELECT * FROM read_parquet('{path.as_posix()}')").df()
    con.close()
    return df


def cliffs_delta(a: np.ndarray, b: np.ndarray) -> float:
    """Cliff's delta：a 比 b 大的概率 - 小的概率。|0.147/0.33/0.474| = 小/中/大。"""
    a = a[~np.isnan(a)]
    b = b[~np.isnan(b)]
    if len(a) < 3 or len(b) < 3:
        return float("nan")
    gt = sum((a[:, None] > b[None, :]).sum(1))
    lt = sum((a[:, None] < b[None, :]).sum(1))
    return float((gt - lt) / (len(a) * len(b)))


def compare_groups(win: np.ndarray, loss: np.ndarray) -> dict:
    d = cliffs_delta(win, loss)
    try:
        p = float(stats.mannwhitneyu(win[~np.isnan(win)], loss[~np.isnan(loss)],
                                     alternative="two-sided").pvalue)
    except ValueError:
        p = float("nan")
    return {"delta": d, "p": p,
            "win_mean": float(np.nanmean(win)) if len(win) else float("nan"),
            "loss_mean": float(np.nanmean(loss)) if len(loss) else float("nan")}


def bh_fdr(pvals: list[float]) -> list[float]:
    """Benjamini-Hochberg 校正。"""
    n = len(pvals)
    order = np.argsort(pvals)
    out = np.ones(n)
    prev = 1.0
    for rank in range(n - 1, -1, -1):
        i = order[rank]
        val = min(prev, pvals[i] * n / (rank + 1))
        out[i] = val
        prev = val
    return list(out)


def factor_table(df: pd.DataFrame) -> pd.DataFrame:
    w = df[df["win"] == 1]
    l = df[df["win"] == 0]
    rows = []
    for f in FACTORS:
        r = compare_groups(w[f].to_numpy(float), l[f].to_numpy(float))
        sub = df[[f, "ret"]].dropna()
        rho = float(stats.spearmanr(sub[f], sub["ret"]).statistic) if len(sub) > 8 else float("nan")
        rows.append({"因子": NICE[f], "盈利波均值": r["win_mean"], "亏损波均值": r["loss_mean"],
                     "Cliff_delta": r["delta"], "p值": r["p"], "与波收益Spearman": rho,
                     "n": int(df[f].notna().sum())})
    t = pd.DataFrame(rows).sort_values("p值", na_position="last")
    t["p_FDR"] = bh_fdr([x if x == x else 1.0 for x in t["p值"]])
    t["效应"] = t["Cliff_delta"].map(
        lambda d: "大" if abs(d) >= 0.474 else "中" if abs(d) >= 0.33 else "小" if abs(d) >= 0.147 else "无"
        if d == d else "-")
    return t


def pct(a: float) -> str:
    return "n/a" if a != a else f"{a * 100:+.1f}%"


def main() -> int:
    if not PANEL.exists():
        print("缺少面板，先跑 scripts/build_wave_entry_panel.py")
        return 1
    df = read_panel(PANEL)
    df["buy_date"] = df["buy_date"].astype(str)
    df["is_oos"] = df["buy_date"] > IS_END.replace("1231", "1231")
    df["is_oos"] = df["buy_date"].str.replace("-", "") > IS_END

    print("=" * 78)
    print(f"样本：{len(df)} 个已清仓波｜{df['ts_code'].nunique()} 只标的"
          f"｜{df['buy_date'].min()} ~ {df['buy_date'].max()}")
    print(f"IS {int((~df['is_oos']).sum())} 波｜OOS {int(df['is_oos'].sum())} 波")
    print(f"总体：总盈亏 {df['pnl'].sum():+,.0f} 元｜胜率 {df['win'].mean():.1%}"
          f"｜均值收益 {pct(df['ret'].mean())}")

    print("\n" + "=" * 78)
    print("一、什么区分了盈利波与亏损波（全样本）")
    t = factor_table(df)
    print(t.to_string(index=False, float_format=lambda x: f"{x:.3f}"))

    print("\n" + "=" * 78)
    print("二、IS / OOS 分别看（防过拟合）")
    for label, sub in (("IS", df[~df["is_oos"]]), ("OOS", df[df["is_oos"]])):
        tt = factor_table(sub)
        keep = [f for f in NICE.values()]
        tt = tt[tt["因子"].isin(keep)]
        print(f"\n-- {label}（{len(sub)} 波，胜率 {sub['win'].mean():.1%}）")
        for _, r in tt.iterrows():
            print(f"   {r['因子']:14s} δ={r['Cliff_delta']:+.3f} p={r['p值']:.4f} "
                  f"p_FDR={r['p_FDR']:.3f} (n={r['n']}) {r['效应']}")

    print("\n" + "=" * 78)
    print("三、H-003：基本面能否区分盈亏（检验预注册门槛）")
    print("   门槛：OOS 上至少一个基本面因子 |δ|>=0.33 且 p<0.05，并在「进场<=2%」子样本胜率差>=10pp")
    oos = df[df["is_oos"]]
    passed = []
    for f in FUNDAMENTAL:
        a = oos[oos["win"] == 1][f].to_numpy(float)
        b = oos[oos["win"] == 0][f].to_numpy(float)
        r = compare_groups(a, b)
        ok = abs(r["delta"]) >= 0.33 and r["p"] < 0.05
        print(f"   {NICE[f]:10s} OOS δ={r['delta']:+.3f} p={r['p']:.4f} → {'达标' if ok else '不达标'}")
        if ok:
            passed.append(f)
    print(f"   → H-003 OOS 阶段{'有' if passed else '无'}因子达标：{passed or '无'}")

    print("\n" + "=" * 78)
    print("四、H-004：「技术面好 × 基本面」2x2 交互")
    d = df.dropna(subset=["entry_gain"]).copy()
    d["技术"] = np.where(d["entry_gain"] > 0.05, "好(>+5%)",
                np.where(d["entry_gain"] <= 0.02, "差(<=+2%)", "中(2~5%)"))
    med_roe = d["roe"].median()
    med_yoy = d["np_yoy"].median()
    for name, med, col in (("ROE", med_roe, "roe"), ("净利同比", med_yoy, "np_yoy")):
        dd = d.dropna(subset=[col]).copy()
        dd["基本面"] = np.where(dd[col] >= med, "好", "烂")
        print(f"\n-- 基本面按{name}中位数({med:.2f})二分")
        tab = dd.pivot_table(index="技术", columns="基本面", values="win",
                             aggfunc=["count", "mean"])
        print(tab.to_string(float_format=lambda x: f"{x:.3f}"))
        for tech in ["好(>+5%)", "差(<=+2%)"]:
            try:
                g = dd[dd["技术"] == tech]
                a = g[g["基本面"] == "好"]["win"].to_numpy(float)
                b = g[g["基本面"] == "烂"]["win"].to_numpy(float)
                if len(a) >= 3 and len(b) >= 3:
                    print(f"   技术{tech}: 基本面好 {a.mean():.1%}(n={len(a)}) vs "
                          f"烂 {b.mean():.1%}(n={len(b)}) → 差 {(a.mean() - b.mean()) * 100:+.1f}pp")
            except Exception:
                pass

    print("\n" + "=" * 78)
    print("五、挣钱最多的 10 个波，买的时候是什么样")
    cols = ["ts_code", "name", "buy_date", "hold_days", "pnl", "ret",
            "entry_gain", "ret5", "roe", "np_yoy", "pb_pct3y", "fc_type"]
    top = df.nlargest(10, "pnl")[cols].copy()
    top["ret"] = top["ret"].map(pct)
    top["entry_gain"] = top["entry_gain"].map(pct)
    top["ret5"] = top["ret5"].map(pct)
    print(top.to_string(index=False))

    print("\n" + "=" * 78)
    print("六、亏钱最多的 10 个波")
    bot = df.nsmallest(10, "pnl")[cols].copy()
    bot["ret"] = bot["ret"].map(pct)
    bot["entry_gain"] = bot["entry_gain"].map(pct)
    bot["ret5"] = bot["ret5"].map(pct)
    print(bot.to_string(index=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
