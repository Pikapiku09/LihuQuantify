"""跑 AI 结论影子跟踪（Step 1）。

只读 dsh-invest 的 ``conclusions.jsonl``，按显式规则在行情里走一遍，产出
``outputs/strategy_trials.json``（同时被量化策略页的契约消费）。

用法::

    .venv\\Scripts\\python.exe scripts\\run_strategy_trials.py
    .venv\\Scripts\\python.exe scripts\\run_strategy_trials.py --inbox <路径> --out <路径>
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from lihu_quantify.monitor.strategy_trials import (  # noqa: E402
    DEFAULT_INBOX, build_trials_block,
)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--inbox", default=str(DEFAULT_INBOX))
    ap.add_argument("--db", default=str(ROOT / "data" / "lihu_quant.duckdb"))
    ap.add_argument("--out", default=str(ROOT / "outputs" / "strategy_trials.json"))
    args = ap.parse_args()

    block = build_trials_block(Path(args.db), Path(args.inbox))
    if block is None:
        print(f"没有可用输入：结论文件 {args.inbox} 或行情库 {args.db} 缺失/为空")
        return 2
    block["generated_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(block, ensure_ascii=False, indent=1), encoding="utf-8")

    s = block["summary"]
    print(f"已写出 {out}")
    print(f"  结论 {s['total']} 条 | 可执行影子交易 {s['long_trials']['n']} 笔"
          f"（持有中 {s['long_trials']['open']} / 已平 {s['long_trials']['closed']}）")
    print(f"  影子交易盈亏合计 {s['long_trials']['pnl_total']:,.2f} 元"
          f" | 盈利 {s['long_trials']['wins']} 笔")
    for tag, key in (("5 日", "direction_hit_5d"), ("10 日", "direction_hit_10d")):
        up = s[key]["expected_up"]
        dn = s[key]["expected_down"]
        print(f"  方向命中（{tag}）：看多类 {up['hits']}/{up['n']} = {up['rate']}"
              f" | 看空类 {dn['hits']}/{dn['n']} = {dn['rate']}")
    if s["skipped"]:
        print(f"  跳过：{s['skipped']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
