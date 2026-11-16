"""H-2026-11-16-002：分批止盈是否优于一次性清仓（仓位/期限中性化后）。

为什么要中性化
--------------
原始观察（登记册 rationale）：分批止盈 73 波 +22,593 元（61.6%）vs 一次性清仓 41 波 −19,728 元（39.0%）。
但两者的**仓位和持有期完全不同**——分批波仓位大 3.1 倍、持有久 2.9 倍。
仓位大 → 盈亏绝对值天然大；持有久 → 既可能赚更多也可能亏更多。
所以「分批赚得多」很可能只是「分批投入多、拿得久」的机械结果，不是方法优劣。

中性化做法
----------
1. **比收益率而不是盈亏金额**（ret = pnl / cost），消掉仓位量级
2. **同持有期分箱内比较**（≤7 / 8~30 / >30 天），消掉期限差异
3. **OLS 控制变量**：ret ~ 分批哑变量 + log(持有天数) + log(投入成本)
   分批系数的符号与显著性才是「方法本身」的效应
4. IS/OOS 分开看

用法
----
    python scripts/analyze_batch_exit.py
"""

from __future__ import annotations

import sys
from datetime import date
from pathlib import Path

import numpy as np
import pandas as pd
from scipy import stats

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from lihu_quantify.behavior.ingest import load_fills  # noqa: E402
from lihu_quantify.behavior.wave import build_waves  # noqa: E402

FILLS = [
    "E:/Dsh_WorkSapce/Dify_Agents/投研反思/Table_1010.csv",
    "E:/Dsh_WorkSapce/Dify_Agents/投研反思/Table_1254.csv",
]
IS_END = date(2025, 12, 31)


def cliffs_delta(a: np.ndarray, b: np.ndarray) -> float:
    a = a[~np.isnan(a)]
    b = b[~np.isnan(b)]
    if len(a) < 3 or len(b) < 3:
        return float("nan")
    gt = (a[:, None] > b[None, :]).sum()
    lt = (a[:, None] < b[None, :]).sum()
    return float((gt - lt) / (len(a) * len(b)))


def ols(y: np.ndarray, X: np.ndarray):
    """最小二乘 + 系数 t 检验（不带截距的 X 需自行含常数列）。"""
    beta, *_ = np.linalg.lstsq(X, y, rcond=None)
    resid = y - X @ beta
    dof = len(y) - X.shape[1]
    if dof <= 0:
        return beta, np.full(X.shape[1], np.nan)
    sigma2 = float(resid @ resid) / dof
    try:
        cov = sigma2 * np.linalg.inv(X.T @ X)
    except np.linalg.LinAlgError:
        return beta, np.full(X.shape[1], np.nan)
    se = np.sqrt(np.diag(cov))
    with np.errstate(divide="ignore", invalid="ignore"):
        t = beta / se
    return beta, t


def main() -> int:
    fills = load_fills(FILLS)
    waves = [w for w in build_waves(fills) if not w.open]
    rows = []
    for w in waves:
        if not w.sell_amount or w.buy_amount <= 0:
            continue
        rows.append({
            "code": w.ts_code, "name": w.name,
            "buy_date": w.buy_date, "hold_days": w.hold_days,
            "cost": w.cost, "pnl": w.pnl, "ret": w.ret,
            "n_buys": w.n_buys, "n_sells": w.n_sells,
            "batch": int(w.n_sells > 1),
        })
    df = pd.DataFrame(rows).dropna(subset=["hold_days"])
    df["is_oos"] = df["buy_date"].map(lambda d: d > IS_END)

    print("=" * 84)
    print(f"样本：{len(df)} 个已清仓波（{df['code'].nunique()} 只标的）")
    g = df.groupby("batch")
    print(f"{'类别':16s}{'波数':>6s}{'总盈亏':>12s}{'胜率':>9s}{'均值收益':>10s}"
          f"{'中位持有':>10s}{'中位成本':>12s}")
    for b, label in ((0, "一次性清仓"), (1, "分批止盈")):
        s = df[df["batch"] == b]
        if not len(s):
            continue
        print(f"{label:16s}{len(s):>6d}{s['pnl'].sum():>+12,.0f}{s['win'].mean() if 'win' in s else (s['pnl']>0).mean():>9.1%}"
              f"{s['ret'].mean():>+10.2%}{s['hold_days'].median():>10.0f}{s['cost'].median():>12,.0f}")

    print("\n⚠ 注意上表：两组的**投入成本与持有期差很多**，绝对盈亏不可直接比。下面做中性化。")

    print("\n" + "=" * 84)
    print("一、同持有期分箱内比较（消掉期限差异）")
    print(f"{'持有区间':14s}{'一次性 n/均值收益/胜率':>34s}{'分批 n/均值收益/胜率':>34s}{'差':>10s}")
    for lab, lo, hi in (("≤7 天", -1, 7), ("8~30 天", 7, 30), (">30 天", 30, 10**6)):
        s = df[(df["hold_days"] > lo) & (df["hold_days"] <= hi)]
        a = s[s["batch"] == 0]
        b = s[s["batch"] == 1]
        if len(a) < 3 or len(b) < 3:
            print(f"{lab:14s}  样本不足（n={len(a)}/{len(b)}）")
            continue
        print(f"{lab:14s}{f'{len(a)} / {a.ret.mean():+.2%} / {(a.pnl>0).mean():.0%}':>34s}"
              f"{f'{len(b)} / {b.ret.mean():+.2%} / {(b.pnl>0).mean():.0%}':>34s}"
              f"{(b.ret.mean()-a.ret.mean())*100:>+9.2f}pp")

    print("\n" + "=" * 84)
    print("二、OLS：ret ~ 分批 + log(持有天数) + log(投入成本)")
    for label, sub in (("全样本", df), ("IS", df[~df["is_oos"]]), ("OOS", df[df["is_oos"]])):
        if len(sub) < 20:
            continue
        y = sub["ret"].to_numpy(float)
        X = np.column_stack([
            np.ones(len(sub)),
            sub["batch"].to_numpy(float),
            np.log1p(sub["hold_days"].to_numpy(float)),
            np.log(sub["cost"].clip(lower=1).to_numpy(float)),
        ])
        beta, t = ols(y, X)
        print(f"  {label:6s} n={len(sub):>3d}  分批系数 {beta[1]:+.4f}（{beta[1]*100:+.2f}pp）"
              f"  t={t[1]:+.2f}  {'显著' if abs(t[1]) > 1.96 else '不显著'}"
              f"  | 持有 {beta[2]:+.4f} 成本 {beta[3]:+.4f}")

    print("\n" + "=" * 84)
    print("三、收益率分布对比（Cliff's delta / Mann-Whitney）")
    a = df[df["batch"] == 0]["ret"].to_numpy(float)
    b = df[df["batch"] == 1]["ret"].to_numpy(float)
    d = cliffs_delta(b, a)
    p = stats.mannwhitneyu(a, b, alternative="two-sided").pvalue if len(a) > 2 and len(b) > 2 else np.nan
    print(f"  一次性 n={len(a)} 均值 {a.mean():+.2%} 中位 {np.median(a):+.2%}")
    print(f"  分批   n={len(b)} 均值 {b.mean():+.2%} 中位 {np.median(b):+.2%}")
    print(f"  Cliff's delta（分批 vs 一次性）= {d:+.3f}  Mann-Whitney p = {p:.4f}")

    print("\n" + "=" * 84)
    print("判读指引：分批系数为正且 |t|>1.96 才算「分批本身有效」；")
    print("          若同分箱内差值与 OLS 系数都不显著 → 原始 +22,593 只是仓位与期限的机械结果。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
