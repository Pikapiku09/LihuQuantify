"""独立刷新热力图快照（不跑整轮巡检）。

用途：行情刚补完、或每日 16:30 巡检没赶上时，把 ``data/heatmap_snapshot.json``
更新到库里的最新交易日。以前这一步只能靠跑完一整轮巡检才有，快照会长期停在旧日期
（实测停在 2026-11-10，而行情已到 11-17）。

用法::

    .venv\\Scripts\\python.exe scripts\\refresh_heatmap_snapshot.py
    .venv\\Scripts\\python.exe scripts\\refresh_heatmap_snapshot.py --min-amount 10000
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from lihu_quantify.monitor.heatmap_snapshot import (  # noqa: E402
    SNAPSHOT_FILE, build_from_db, write_snapshot,
)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=str(ROOT / "data" / "lihu_quant.duckdb"))
    ap.add_argument("--min-amount", type=float, default=0.0,
                    help="成交额下限（千元），用于滤掉流动性极差的标的")
    args = ap.parse_args()

    db = Path(args.db)
    if not db.exists():
        print(f"找不到库：{db}")
        return 2
    rows = build_from_db(db, args.min_amount)
    if not rows:
        print("库中没有可用行情（daily_quotes 为空）")
        return 2
    path = write_snapshot(ROOT, rows)
    if path is None:
        print("写入失败")
        return 3
    days = sorted({r["trade_date"] for r in rows})
    inds = {r["industry"] for r in rows}
    total = sum((r.get("amount") or 0) for r in rows) / 1e5
    print(f"已写入 {path}")
    print(f"  {len(rows)} 只 | {len(inds)} 个行业 | 成交额合计 {total:.1f} 亿")
    print(f"  交易日 {days}（只保留最新一天）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
