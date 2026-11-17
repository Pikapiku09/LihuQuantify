"""用历史日报重建**模拟盘**权益曲线，写入 data/equity_history.jsonl。

⚠️ 2026-11-17 修正记录（必读）
-----------------------------
本脚本最初从 ``outputs/monthly_perf.json`` 的 ``equity_curve`` 回填，那份曲线是**实盘**
交割单重建出来的（initial_capital=110,000、898 笔真实成交、期末 123,656.63）。
而模拟盘是另一个账户：``paper_state.init_capital=100,000``、197 笔纸面成交、期末 88,747.61。
把实盘曲线写进模拟盘的契约并标 ``account_type="paper"``，正是本项目一直在防的「混账」。
误用的那份数据已移到 ``data/equity_real_reconstructed.jsonl`` 留档，不再参与契约。

现在改为**从模拟盘自己的日报**重建：``outputs/reports/YYYY-MM-DD.md`` 里
``| 总资产 | 88,748 |`` 一行就是当日收盘权益。这些日报由巡检每日生成，是模拟盘的自有记录。

校验点：重建出的最后一个点必须与 ``data/paper_state.json`` 的
``asset.total_asset`` 一致（误差 <1 元），否则说明日报与状态文件不同源，直接报错退出。

只认严格 ``YYYY-MM-DD.md`` 文件名：reports/ 下还有 ``monthly_202608``、
``20261020_波段池回测报告_*`` 之类的非日报文件，混进来会污染曲线。
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import date as _date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from lihu_quantify.monitor.equity_history import (  # noqa: E402
    EQUITY_FILE, SOURCE_LIVE, SOURCE_RECONSTRUCTED,
    append_point, load_points, summarize,
)

REPORTS = Path("outputs") / "reports"
_NAME_RE = re.compile(r"^(\d{4}-\d{2}-\d{2})\.md$")
_TOTAL_RE = re.compile(r"\|\s*总资产\s*\|\s*([\d,]+(?:\.\d+)?)\s*\|")


def _parse_reports(reports_dir: Path) -> list[tuple[str, float]]:
    rows: list[tuple[str, float]] = []
    for p in sorted(reports_dir.glob("*.md")):
        m = _NAME_RE.match(p.name)
        if not m:
            continue                      # 非日报（月度汇总、波段池报告等）一律跳过
        day = m.group(1)
        try:
            _date.fromisoformat(day)
        except ValueError:
            continue
        try:
            text = p.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        hit = _TOTAL_RE.search(text)
        if not hit:
            continue
        rows.append((day, float(hit.group(1).replace(",", ""))))
    return rows


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--reports", default=str(ROOT / REPORTS))
    ap.add_argument("--out", default=str(ROOT / EQUITY_FILE))
    ap.add_argument("--state", default=str(ROOT / "data" / "paper_state.json"))
    ap.add_argument("--allow-mismatch", action="store_true",
                    help="仅调试用：跳过「末点必须等于 paper_state」的一致性校验")
    args = ap.parse_args()

    rows = _parse_reports(Path(args.reports))
    if not rows:
        print("没有解析到任何日报总资产；先确认 outputs/reports 下有 YYYY-MM-DD.md")
        return 2

    out = Path(args.out)
    changed = 0
    for day, total in rows:
        if append_point(out, day, total, source=SOURCE_RECONSTRUCTED):
            changed += 1

    points = load_points(out)
    s = summarize(points)

    # 一致性校验：重建末点必须与状态文件同源
    state_path = Path(args.state)
    state_total = None
    if state_path.exists():
        try:
            state_total = float((json.loads(state_path.read_text(encoding="utf-8"))
                                 .get("asset") or {}).get("total_asset"))
        except (OSError, json.JSONDecodeError, TypeError, ValueError):
            state_total = None
    print(f"写入 {out}")
    print(f"  本次改动 {changed} 行 / 共 {s['n_points']} 点"
          f"（live {s['live_days']} / reconstructed {s['reconstructed_days']}）")
    print(f"  区间 {s['first_date']} ~ {s['last_date']}")
    if s["current_drawdown"] is not None:
        print(f"  当前回撤 {s['current_drawdown']:.2%}"
              f" | 最大回撤 {s['max_drawdown']:.2%}"
              f"（{s['max_drawdown_peak_date']} → {s['max_drawdown_trough_date']}）")
    if state_total is not None and s["latest_equity"] is not None:
        diff = abs(s["latest_equity"] - state_total)
        print(f"  paper_state.total_asset = {state_total:,.2f} | 差 {diff:,.2f} 元")
        if diff > 1.0 and not args.allow_mismatch:
            print("  ❌ 重建末点与 paper_state 不一致 —— 说明数据不同源，拒绝继续。"
                  "（确认无误可加 --allow-mismatch 调试）")
            return 3
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
