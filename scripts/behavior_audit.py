"""行为体检 CLI：把交割单变成 6 项可执行的行为指标。

用法::

    python scripts/behavior_audit.py --fills 交割单.csv [更多.csv] \\
        [--db data/lihu_quant.duckdb] [--out outputs/behavior_report.md]

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

from lihu_quantify.behavior.ingest import load_fills, to_trade_records  # noqa: E402
from lihu_quantify.behavior.metrics import (  # noqa: E402
    loss_add_ratio,
    peak_capture,
    pgr_plr,
    position_sizing,
    post_sell_upside,
    turnover,
)
from lihu_quantify.backtest.metrics import pair_round_details  # noqa: E402


def _make_high_lookup(db_path: Path):
    """返回 high_lookup(code, start, end) -> 区间最高收盘价 | None。

    DuckDB 只读连接；trade_date 兼容 DATE / VARCHAR 两种存储。
    查不到（代码不在库 / 区间无数据）一律返回 None —— 不编造 0。
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
                "WHERE ts_code = ? AND CAST(trade_date AS VARCHAR) "
                "BETWEEN ? AND ?",
                [ts_code, str(start), str(end)],
            ).fetchone()
            val = float(row[0]) if row and row[0] is not None else None
        except Exception:  # noqa: BLE001
            val = None
        cache[key] = val
        return val

    return lookup, (lambda: con.close() if con is not None else None)


def build_report(fills: list[dict], rounds: list[dict], high_lookup) -> dict:
    """汇总 6 项指标 + 覆盖率。纯计算，无 IO。"""
    codes_with_rounds = {r["ts_code"] for r in rounds}
    covered = sum(1 for c in codes_with_rounds
                  if high_lookup(c, rounds[0]["buy_date"], rounds[-1]["sell_date"])
                  is not None) if rounds else 0
    return {
        "fills": {
            "n_fills": len(fills),
            "n_buys": sum(1 for f in fills if f["side"] == "buy"),
            "n_sells": sum(1 for f in fills if f["side"] == "sell"),
            "n_codes": len({f["ts_code"] for f in fills}),
            "first_date": str(min((f["trade_date"] for f in fills), default="")),
            "last_date": str(max((f["trade_date"] for f in fills), default="")),
        },
        "rounds": {
            "n_rounds": len(rounds),
            "total_pnl": round(sum(float(r["pnl"]) for r in rounds), 2),
            "win_rate": round(
                sum(1 for r in rounds if float(r["pnl"]) > 0) / len(rounds), 4
            ) if rounds else 0.0,
        },
        "quote_coverage": {
            "codes_total": len(codes_with_rounds),
            "codes_covered": covered,
        },
        "pgr_plr": pgr_plr(rounds),
        "loss_add_ratio": loss_add_ratio(fills),
        "peak_capture": peak_capture(rounds, high_lookup),
        "post_sell_upside": post_sell_upside(rounds, high_lookup, window=20),
        "turnover": turnover(fills),
        "position_sizing": position_sizing(rounds),
    }


THRESHOLDS = {
    "loss_add_ratio.ratio": (0.10, ">", "浮亏加仓占买入额"),
    "position_sizing.loss_to_win_ratio": (1.10, ">", "盈亏单规模比"),
    "turnover.turnover_annual": (6.0, ">", "年化换手"),
    "peak_capture.median": (0.50, "<", "峰值捕获率(中位)"),
}


def _get(report: dict, dotted: str):
    cur = report
    for part in dotted.split("."):
        if not isinstance(cur, dict) or part not in cur:
            return None
        cur = cur[part]
    return cur


def format_markdown(report: dict) -> str:
    """生成行为体检 Markdown（中文、表格化）。"""
    f, r = report["fills"], report["rounds"]
    lines = [
        "# 行为体检报告", "",
        f"- 样本：{f['n_fills']} 笔成交（买 {f['n_buys']} / 卖 {f['n_sells']}）"
        f"，{f['n_codes']} 个标的",
        f"- 区间：{f['first_date']} ~ {f['last_date']}",
        f"- FIFO 配对轮次：**{r['n_rounds']}** 笔，合计 **{r['total_pnl']:+,.0f}** 元，"
        f"胜率 {r['win_rate']:.1%}",
        f"- 行情覆盖：{report['quote_coverage']['codes_covered']}/"
        f"{report['quote_coverage']['codes_total']} 个标的（未覆盖的自动跳过）",
        "",
        "## 告警判定", "",
        "| 指标 | 实测 | 告警线 | 状态 |",
        "|---|---|---|---|",
    ]
    for path, (line, op, label) in THRESHOLDS.items():
        val = _get(report, path)
        if val is None:
            continue
        hit = val > line if op == ">" else val < line
        show = f"{val:.2%}" if "ratio" in path or "median" in path else f"{val:.2f}"
        limit = f"{line:.0%}" if "ratio" in path or "median" in path else f"{line:g}"
        lines.append(f"| {label} | {show} | {op} {limit} | "
                     f"{'**告警**' if hit else '正常'} |")

    p, la, pc = report["pgr_plr"], report["loss_add_ratio"], report["peak_capture"]
    ps, pu, tu = report["position_sizing"], report["post_sell_upside"], report["turnover"]
    lines += [
        "", "## 1. 处置效应：持有期维度", "",
        "| 项 | 值 |", "|---|---|",
        f"| 盈利单持有均值 | {p['hold_win_mean']} 天 |",
        f"| 亏损单持有均值 | {p['hold_loss_mean']} 天 |",
        f"| 差值（盈利-亏损） | {p['hold_diff']:+} 天 |",
        f"| bootstrap 95% CI | [{p['diff_ci_low']:+.1f}, {p['diff_ci_high']:+.1f}] |",
        f"| 是否显著 | {'是' if p['significant'] else '**否（CI 跨 0）**'} |",
        f"| 盈利单均值盈亏 | {p['win_pnl_mean']:+,.0f} |",
        f"| 亏损单均值盈亏 | {p['loss_pnl_mean']:+,.0f} |",
        "",
        "## 2. 浮亏加仓", "",
        f"- 笔数：**{la['n_adds']}** / {la['n_buys']} 次买入",
        f"- 金额：**{la['add_amount']:,.0f}** 元",
        f"- 占全部买入额：**{la['ratio']:.1%}**",
        "",
        "## 3. 峰值捕获率", "",
        f"- 中位 **{pc['median']:.1%}**（均值 {pc['mean']:.1%}，P25 {pc['p25']:.1%} / "
        f"P75 {pc['p75']:.1%}）",
        f"- 捕获率 <50% 的占比：{pc['below_half_pct']:.1%}",
        "",
        "## 4. 卖出后走势（窗口 %d 日）" % pu["window"], "",
        f"- 继续上涨比例：**{pu['continued_up_pct']:.1%}**",
        f"- 平均还能涨：{pu['mean_gain_pct']:+.2%}（中位 {pu['median_gain_pct']:+.2%}）",
        "",
        "## 5. 换手与摩擦成本", "",
        f"- 年化换手：**{tu['turnover_annual']:.1f} 倍**",
        f"- 摩擦成本估算：**{tu['friction_cost_pct']:.2%}** / 年",
        f"- 分母（资金规模）：{tu['capital_base']:,.0f}，样本跨度 {tu['span_days']} 天",
        "",
        "## 6. 仓位规模", "",
        "| 分位 | 金额 |", "|---|---|",
        f"| P25 | {ps['p25']:,.0f} |", f"| P50 | {ps['p50']:,.0f} |",
        f"| P75 | {ps['p75']:,.0f} |", f"| P90 | {ps['p90']:,.0f} |",
        "",
        f"- 盈利单均规模 {ps['win_cost_mean']:,.0f} / 亏损单均规模 "
        f"{ps['loss_cost_mean']:,.0f} → 比 **{ps['loss_to_win_ratio']:.2f}x**",
        "",
    ]
    return "\n".join(lines)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="交割单行为体检")
    ap.add_argument("--fills", nargs="+", required=True, help="交割单 CSV 路径（GBK）")
    ap.add_argument("--db", default=str(ROOT / "data" / "lihu_quant.duckdb"))
    ap.add_argument("--out", default=None, help="输出 Markdown 路径")
    args = ap.parse_args(argv)

    fills = load_fills(args.fills)
    if not fills:
        print("[error] 没读到任何真实成交（检查 GBK 编码与「交易类别」列）",
              file=sys.stderr)
        return 2
    rounds = pair_round_details(to_trade_records(fills))
    lookup, close_db = _make_high_lookup(Path(args.db))
    try:
        report = build_report(fills, rounds, lookup)
    finally:
        close_db()

    md = format_markdown(report)
    if args.out:
        out = Path(args.out)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(md, encoding="utf-8")
        print(f"[ok] 报告已写入 {out}")
    else:
        print(md)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
