"""第七轮修复2：修正移动止盈公式前后的网格对比表（诊断不选参）。

对比基线（bug 口径 ×1.03，grid_v2_strat200_halt.csv）与
修正后（×0.97，grid_v2_strat200_halt_fix97.csv）。

输出：outputs/grid_v2_strat200_halt_fix97_comparison.md

用法：
    .venv/Scripts/python.exe scripts/compare_grid_fix97.py
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import pandas as pd

from grid_search_v2 import ORIG_PARAMS, robustness_analysis

OUT_DIR = ROOT / "outputs"
BASE_CSV = OUT_DIR / "grid_v2_strat200_halt.csv"          # 修正前（bug 口径 ×1.03）
FIX_CSV = OUT_DIR / "grid_v2_strat200_halt_fix97.csv"     # 修正后（×0.97）
OUT_MD = OUT_DIR / "grid_v2_strat200_halt_fix97_comparison.md"

METRICS = ["total_return", "max_drawdown", "calmar", "win_rate", "trades"]


def main() -> None:
    base = pd.read_csv(BASE_CSV)
    fix = pd.read_csv(FIX_CSV)
    key = ["ma5_dev", "freshness", "chasing"]
    merged = base.merge(fix, on=key, suffixes=("_bug103", "_fix97"), validate="1:1")

    ra_base = robustness_analysis(base, metric="total_return")
    ra_fix = robustness_analysis(fix, metric="total_return")

    def orig_row(ra: dict, label: str) -> str:
        nb = ra["orig_nb_vals"]
        nb_txt = (f"{min(nb):+.1%} ~ {max(nb):+.1%}（全正：{'是' if all(v > 0 for v in nb) else '否'}）"
                  if nb else "—")
        return (f"| {label} | {ra['orig_val']:+.1%} | {nb_txt} "
                f"| {'✅ 稳健' if ra['orig_robust'] else '⚠️ 不稳健'} "
                f"| {ra['n_robust']}/{ra['n_total']} |")

    lines = [
        "# 网格对比：移动止盈公式修正前 vs 修正后（strat200 池，训练段）",
        "",
        "> **背景**：第七轮修复1 将回测移动止盈公式由 `hwm×(1+3%)`（bug 口径，实际语义为"
        "“浮盈即次日离场”）修正为 `hwm×(1-3%)`（回撤 3% 离场）。",
        f"> 数据段：训练段 2024-07-26 ~ 2025-12-12，strat200 池（seed=42），"
        f"含连亏停手 + 板块≤40% 口径。",
        ">",
        f"> ⚠️ **诊断不选参**：本次重跑仅判定 live 参数 {ORIG_PARAMS} 在修正口径下是否存活，"
        "**不据此改 live 参数**。收益数字含幸存者偏差（乐观口径），横向比较有效、绝对值无效。",
        "",
        "## 一、原参数 (0.05, 3, 0.08) 修正前后对比（核心结论）",
        "",
        "| 口径 | 原参数收益 | 一阶邻域收益范围 | 稳健判定 | 稳健点数 |",
        "|---|---|---|---|---|",
        orig_row(ra_base, "修正前（bug ×1.03）"),
        orig_row(ra_fix, "修正后（×0.97）"),
        "",
        "## 二、全网格逐组对比",
        "",
        "| ma5_dev | fresh | chase | 收益(修正前) | 收益(修正后) | 差值(pp) | 交易数(前→后) |",
        "|---|---|---|---|---|---|---|",
    ]
    for r in merged.itertuples():
        lines.append(
            f"| {r.ma5_dev} | {r.freshness} | {r.chasing} "
            f"| {r.total_return_bug103:+.1%} | {r.total_return_fix97:+.1%} "
            f"| {(r.total_return_fix97 - r.total_return_bug103) * 100:+.1f} "
            f"| {r.trades_bug103} → {r.trades_fix97} |"
        )

    diff = (merged["total_return_fix97"] - merged["total_return_bug103"])
    lines += [
        "",
        "## 三、整体影响统计",
        "",
        f"- 36 组收益变化：均值 {diff.mean() * 100:+.1f} pp，"
        f"中位数 {diff.median() * 100:+.1f} pp，"
        f"范围 {diff.min() * 100:+.1f} ~ {diff.max() * 100:+.1f} pp",
        f"- 收益下降组数：{(diff < 0).sum()}/36；上升组数：{(diff > 0).sum()}/36",
        f"- 修正后转负组数：{(merged['total_return_fix97'] <= 0).sum()}/36",
        "",
        "---",
        "统计纪律：本轮为 bug 修复后的存活性诊断（第七轮修复2），不据此选参；"
        "live 参数/池/过滤零变更。",
        "",
        "以上内容仅供参考，不构成任何投资建议。投资有风险，入市需谨慎。",
    ]
    OUT_MD.write_text("\n".join(lines), encoding="utf-8")
    print(f"对比表已写入: {OUT_MD}")
    print(f"修正后稳健点: {ra_fix['n_robust']}/{ra_fix['n_total']}")
    print(f"原参数修正后: {ra_fix['orig_val']:+.2%}，稳健={'是' if ra_fix['orig_robust'] else '否'}")


if __name__ == "__main__":
    main()
