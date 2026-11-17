"""用历史交割单重建的权益曲线补齐 data/equity_history.jsonl（一次性/可重复跑）。

数据来源：``outputs/monthly_perf.json`` 的 ``equity_curve``（504 个交易日，由 905 笔真实成交重建）。
写入时全部标 ``source = "reconstructed"``，**不会覆盖**巡检写入的 ``live`` 点。

为什么单独一个脚本而不塞进巡检：重建是回溯操作，只在需要补历史时跑一次；
巡检每天只负责追加当天真实记账值。两者混在一起，出问题时分不清是谁写的。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from lihu_quantify.monitor.equity_history import (  # noqa: E402
    EQUITY_FILE, SOURCE_RECONSTRUCTED, append_point, load_points, summarize,
)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--curve", default=str(ROOT / "outputs" / "monthly_perf.json"))
    ap.add_argument("--out", default=str(ROOT / EQUITY_FILE))
    args = ap.parse_args()

    curve_path = Path(args.curve)
    if not curve_path.exists():
        print(f"找不到 {curve_path}；先跑 scripts/monthly_perf.py")
        return 2
    data = json.loads(curve_path.read_text(encoding="utf-8"))
    curve = data.get("equity_curve") or []
    if not curve:
        print(f"{curve_path} 里没有 equity_curve")
        return 2

    out = Path(args.out)
    changed = 0
    for pt in curve:
        if append_point(out, pt.get("date"), pt.get("equity") or 0.0,
                        source=SOURCE_RECONSTRUCTED):
            changed += 1

    points = load_points(out)
    s = summarize(points)
    print(f"写入 {out}")
    print(f"  本次改动 {changed} 行 / 共 {s['n_points']} 点"
          f"（live {s['live_days']} / reconstructed {s['reconstructed_days']}）")
    print(f"  区间 {s['first_date']} ~ {s['last_date']}")
    print(f"  当前回撤 {s['current_drawdown']:.2%}" if s["current_drawdown"] is not None else "  当前回撤 —")
    print(f"  最大回撤 {s['max_drawdown']:.2%}"
          f"（{s['max_drawdown_peak_date']} → {s['max_drawdown_trough_date']}）"
          if s["max_drawdown"] is not None else "  最大回撤 —")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
