"""行为体检 CLI：把交割单变成 6 项可执行的行为指标。

用法::

    python scripts/behavior_audit.py --fills 交割单.csv [更多.csv] \\
        [--db data/lihu_quant.duckdb] [--out outputs/behavior_report.md] \\
        [--json outputs/behavior_report.json] [--capital 500000]

两个输出各有用途：
    --out   Markdown，给人看
    --json  JSON 缓存，给每日日报读（``monitor.scheduler`` 只读缓存，
            巡检本身**不碰交割单也不连 DuckDB**，避免拖慢每日流程）

设计要点：
    - 只读，不连网、不调 Tushare
    - FIFO 配对复用 ``backtest.metrics.pair_round_details``（单一配对算法）
    - 行情缺失（ETF / 退市 / 未入库代码）时相关指标自动跳过并报告覆盖率，
      绝不把缺失当 0
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))

# Windows 控制台默认 GBK，中文/数学符号会炸；统一走 UTF-8
for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        try:
            _stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:  # noqa: BLE001
            pass

from lihu_quantify.backtest.metrics import pair_round_details  # noqa: E402
from lihu_quantify.behavior.ingest import load_fills, to_trade_records  # noqa: E402
from lihu_quantify.behavior.report import (  # noqa: E402
    build_report,
    format_markdown,
    write_json,
)


def _make_high_lookup(db_path: Path):
    """返回 (high_lookup, close_fn)。

    high_lookup(code, start, end) -> 区间最高收盘价 | None。
    查不到（代码不在库 / 区间无数据）一律返回 None —— 不编造 0。
    用 MAX(close) 而非 MAX(high)：与"持有期最高收盘价"口径一致，
    也避免日内插针把捕获率压低。
    """
    cache: dict[tuple, float | None] = {}
    con = None
    if db_path and Path(db_path).exists():
        try:
            import duckdb
            con = duckdb.connect(str(db_path), read_only=True)
        except Exception as exc:  # noqa: BLE001 - 无库也应能出报告
            print(f"[warn] DuckDB 打开失败，行情指标将跳过: {exc}", file=sys.stderr)
            con = None

    def lookup(ts_code: str, start, end) -> float | None:
        if con is None:
            return None
        key = (ts_code, str(start), str(end))
        if key in cache:
            return cache[key]
        try:
            row = con.execute(
                "SELECT MAX(close) FROM daily_quotes "
                "WHERE ts_code = ? AND trade_date BETWEEN ? AND ?",
                [ts_code, start, end],
            ).fetchone()
            val = float(row[0]) if row and row[0] is not None else None
        except Exception:  # noqa: BLE001
            val = None
        cache[key] = val
        return val

    def close() -> None:
        if con is not None:
            con.close()

    return lookup, close


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="交割单行为体检")
    ap.add_argument("--fills", nargs="+", required=True, help="交割单 CSV（GBK 编码）")
    ap.add_argument("--db", default=str(ROOT / "data" / "lihu_quant.duckdb"),
                    help="DuckDB 路径（缺省项目内 data/lihu_quant.duckdb）")
    ap.add_argument("--out", default=None, help="Markdown 输出路径")
    ap.add_argument("--json", dest="json_out", default=None,
                    help="JSON 缓存输出路径（供每日日报读取）")
    ap.add_argument("--capital", type=float, default=None,
                    help="账户资金规模（换手率分母）；不传则用资金余额，会高估换手")
    args = ap.parse_args(argv)

    fills = load_fills(args.fills)
    if not fills:
        print("[error] 没读到任何真实成交（检查 GBK 编码与「交易类别」列）",
              file=sys.stderr)
        return 2

    rounds = pair_round_details(to_trade_records(fills))
    lookup, close_db = _make_high_lookup(Path(args.db))
    try:
        report = build_report(fills, rounds, lookup, capital_base=args.capital)
    finally:
        close_db()

    wrote = False
    if args.json_out:
        p = write_json(report, args.json_out)
        print(f"[ok] JSON 缓存已写入 {p}")
        wrote = True
    if args.out:
        p = Path(args.out)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(format_markdown(report), encoding="utf-8")
        print(f"[ok] Markdown 报告已写入 {p}")
        wrote = True
    if not wrote:
        print(format_markdown(report))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
