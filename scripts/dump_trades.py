"""导出模拟盘成交流水（第六轮清单4 可选项）。

读 data/paper_state.json 的 trades，逐笔导出：
    代码 / 方向 / 日期 / 价格 / 股数 / 盈亏 / 原因 / 持有天数
（盈亏/原因为第五轮后新增字段，旧记录为空显示 '-'；持有天数按同票
 上一笔买入日到本笔卖出日计算）

用法：
    python scripts/dump_trades.py                 # 控制台表格
    python scripts/dump_trades.py -o trades.csv   # 导出 CSV
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
STATE_FILE = ROOT / "data" / "paper_state.json"


def _as_date(v):
    if not v:
        return None
    try:
        return date.fromisoformat(str(v)[:10])
    except ValueError:
        return None


def main() -> int:
    parser = argparse.ArgumentParser(description="导出模拟盘成交流水")
    parser.add_argument("-o", "--out", default="", help="输出 CSV 路径（默认仅打印）")
    args = parser.parse_args()

    if not STATE_FILE.exists():
        print(f"状态文件不存在: {STATE_FILE}")
        return 1
    state = json.loads(STATE_FILE.read_text(encoding="utf-8"))
    trades = state.get("trades") or []

    rows = []
    last_buy: dict[str, date] = {}
    for t in trades:
        code = t.get("ts_code", "")
        d = _as_date(t.get("date"))
        side = t.get("side", "")
        if side == "buy" and d:
            last_buy[code] = d
        hold_days = ""
        if side == "sell" and d and code in last_buy:
            hold_days = (d - last_buy[code]).days
        rows.append({
            "代码": code,
            "方向": "买入" if side == "buy" else "卖出",
            "日期": str(t.get("date", ""))[:10],
            "价格": t.get("price"),
            "股数": t.get("volume"),
            "盈亏": t.get("pnl", ""),
            "原因": t.get("reason", ""),
            "持有天数": hold_days,
        })

    if not rows:
        print("无成交记录。")
        return 0

    if args.out:
        out = Path(args.out)
        with out.open("w", newline="", encoding="utf-8-sig") as f:
            writer = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
            writer.writeheader()
            writer.writerows(rows)
        print(f"已导出 {len(rows)} 笔 → {out}")
    else:
        header = list(rows[0].keys())
        print(" | ".join(header))
        print("-" * 100)
        for r in rows:
            print(" | ".join(str(r[h]) for h in header))
        print(f"\n共 {len(rows)} 笔（买入 {sum(1 for r in rows if r['方向']=='买入')} / "
              f"卖出 {sum(1 for r in rows if r['方向']=='卖出')}）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
