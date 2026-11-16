"""IntradayReversal（实盘主策略）的两组实验回测。

实验 A｜参数敏感性（H-2026-11-16-002 相关，仅描述性，不做参数择优）
    entry_pct ∈ {0.10, 0.20, 0.30} × trend_filter ∈ {False, True}
    ⚠ 这是**敏感性分析**，不是调参。任何「最优值」都必须再走一遍独立 OOS 才可采纳，
      不能拿本脚本的输出直接改 config/settings.yaml。

实验 B｜五日观察哨（H-2026-11-16-008）
    基线 = 原 IR 规则；实验组 = IR + FiveDayWatchStrategy(days=5, threshold=0)
    其余完全相同（同池、同窗口、同上限、同费率）。

用法
----
    python scripts/run_ir_experiments.py --exp sensitivity
    python scripts/run_ir_experiments.py --exp fiveday
    python scripts/run_ir_experiments.py --exp both
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from loguru import logger  # noqa: E402

from lihu_quantify.backtest.broker import SimulatedBroker  # noqa: E402
from lihu_quantify.backtest.engine import EventDrivenEngine  # noqa: E402
from lihu_quantify.config import get_settings  # noqa: E402
from lihu_quantify.data.duckdb_store import DuckDBStore  # noqa: E402
from lihu_quantify.data.tushare_client import TushareClient  # noqa: E402
from lihu_quantify.market import classify_market_state  # noqa: E402
from lihu_quantify.strategy.five_day_watch import FiveDayWatchStrategy  # noqa: E402
from lihu_quantify.strategy.intraday_reversal import IntradayReversal  # noqa: E402

from run_full_backtest import build_sector_map, fetch_data, fetch_universe  # noqa: E402

IS = (date(2024, 10, 14), date(2025, 12, 31))
OOS = (date(2026, 1, 1), date(2026, 11, 17))


def seg(curve: list[dict], lo: date, hi: date):
    pts = [(date.fromisoformat(x["date"]), x["equity"]) for x in curve]
    s = [(d, e) for d, e in pts if lo <= d <= hi]
    if len(s) < 2:
        return None
    base = s[0][1]
    ret = s[-1][1] / base - 1
    peak = s[0][1]
    mdd = 0.0
    for _, e in s:
        peak = max(peak, e)
        mdd = min(mdd, e / peak - 1)
    return {"ret": ret, "mdd": mdd}


def run_one(label, strategy, settings, data, idx_df, sector_map):
    s, r, b = settings.strategy, settings.risk, settings.backtest
    broker = SimulatedBroker(
        commission_rate=b.commission, stamp_tax_rate=b.stamp_tax, slippage=b.slippage,
    )
    market_states = classify_market_state(idx_df) if s.market_filter else None
    engine = EventDrivenEngine(
        strategy=strategy, broker=broker, max_single=r.max_single_position,
        market_states=market_states, market_filter_on=s.market_filter,
        market_filter_mode=s.market_filter_mode,
    )
    result = engine.run(data, init_capital=settings.init_capital, sector_by_code=sector_map)
    curve = [{"date": str(d), "equity": round(float(v), 2)} for d, v in result.equity.items()]
    m = {k: (round(v, 4) if isinstance(v, float) else v) for k, v in result.metrics.items()}
    logger.info(f"[{label}] 总收益 {m.get('total_return'):+.2%} 回撤 {m.get('max_drawdown'):.2%}")
    return {"label": label, "metrics": m, "equity_curve": curve,
            "is": seg(curve, *IS), "oos": seg(curve, *OOS)}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--exp", default="both", choices=["sensitivity", "fiveday", "both"])
    ap.add_argument("--n", type=int, default=50)
    ap.add_argument("--years", type=int, default=2)
    args = ap.parse_args()

    settings = get_settings(str(ROOT / "config" / "settings.yaml"))
    client = TushareClient(token=settings.resolved_tushare_token(),
                           cache_dir=settings.resolved_cache_dir())
    store = DuckDBStore(settings.resolved_duckdb_path())

    codes = fetch_universe(client, store, max_n=args.n)
    data, idx_df = fetch_data(client, store, codes, years=args.years)
    sector_map = build_sector_map(client, store)
    s, r = settings.strategy, settings.risk
    logger.info(f"池 {len(data)} 只｜单票上限 {r.max_single_position:.0%}｜市场过滤 {s.market_filter}")

    out = {"n_codes": len(data), "years": args.years,
           "max_single_position": r.max_single_position, "experiments": {}}

    if args.exp in ("sensitivity", "both"):
        rows = []
        for entry_pct in (0.10, 0.20, 0.30):
            for tf in (False, True):
                st = IntradayReversal(
                    entry_pct=entry_pct, trend_filter=tf,
                    max_position_pct=r.max_single_position,
                    stop_loss_force_pct=r.stop_loss_force,
                )
                rows.append(run_one(f"entry_pct={entry_pct},trend_filter={tf}", st,
                                    settings, data, idx_df, sector_map))
        out["experiments"]["sensitivity"] = rows

    if args.exp in ("fiveday", "both"):
        base = IntradayReversal(
            max_position_pct=r.max_single_position,
            stop_loss_force_pct=r.stop_loss_force,
        )
        base_r = run_one("基线 IR", base, settings, data, idx_df, sector_map)
        for days, thr in ((5, 0.0), (5, -0.02), (10, 0.0)):
            inner = IntradayReversal(
                max_position_pct=r.max_single_position,
                stop_loss_force_pct=r.stop_loss_force,
            )
            wrapped = FiveDayWatchStrategy(inner, days=days, threshold=thr)
            res = run_one(f"五日观察哨 days={days},thr={thr:+.0%}", wrapped,
                          settings, data, idx_df, sector_map)
            res["triggered"] = wrapped.n_triggered
            out["experiments"].setdefault("fiveday", []).append(res)
        out["experiments"].setdefault("fiveday", []).insert(0, base_r)

    path = ROOT / "outputs" / "ir_experiments.json"
    path.write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
    logger.info(f"写出 {path}")

    # ---- 汇总表 ----
    for name, rows in out["experiments"].items():
        print("\n" + "=" * 96)
        print(f"实验 {name}（池 {out['n_codes']} 只，{out['years']} 年，上限 {out['max_single_position']:.0%}）")
        print(f"{'配置':32s}{'总收益':>10s}{'回撤':>10s}{'夏普':>9s}"
              f"{'IS收益':>10s}{'OOS收益':>10s}{'OOS回撤':>10s}{'触发':>7s}")
        for r_ in rows:
            m = r_["metrics"]
            i = r_.get("is") or {}
            o = r_.get("oos") or {}
            # 零成交配置的 sharpe 会因除零算出 -4.6e16 这种假值，如实标「无交易」而不是报数字
            no_trade = not m.get("total_trades")
            if no_trade:
                print(f"{r_['label']:32s}{'无交易':>10s}{'-':>10s}{'-':>9s}"
                      f"{i.get('ret', 0):>10.2%}{o.get('ret', 0):>10.2%}{o.get('mdd', 0):>10.2%}"
                      f"{r_.get('triggered', '-'):>7}")
                continue
            print(f"{r_['label']:32s}"
                  f"{m.get('total_return', 0):>10.2%}"
                  f"{m.get('max_drawdown', 0):>10.2%}"
                  f"{m.get('sharpe', 0):>9.3f}"
                  f"{i.get('ret', 0):>10.2%}"
                  f"{o.get('ret', 0):>10.2%}"
                  f"{o.get('mdd', 0):>10.2%}"
                  f"{r_.get('triggered', '-'):>7}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
