"""trend_filter 独立验证（H-2026-11-16-009）。

背景
----
IR 参数敏感性扫描里，entry_pct=0.20 时 trend_filter=True 总收益 +10.60%（OOS +9.03%），
而当前实盘 trend_filter=False 是 -4.06%（OOS -2.34%）。但那是 6 个配置里挑出来的最大值，
属于扫描结果，不能直接采用。

本脚本做**换池检验**：在完全不同的股票池（市值三分位三池，各 50 只）上重跑同一对比。
如果 tf=True 在三个池上都赢，才算有信号；只在某一个池上赢就是噪声。

用法
----
    python scripts/verify_trend_filter.py
"""

from __future__ import annotations

import json
import sys
from datetime import date, timedelta
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
from lihu_quantify.strategy.intraday_reversal import IntradayReversal  # noqa: E402

from run_full_backtest import fetch_data  # noqa: E402
from scripts.run_marketcap_tercile_bt import (  # noqa: E402
    POOLS, SEED, build_pools, build_sector_map, eligible_codes, mv_at,
)

IS = (date(2024, 10, 14), date(2025, 12, 31))
OOS = (date(2026, 1, 1), date(2026, 11, 17))


def seg(curve, lo, hi):
    pts = [(date.fromisoformat(x["date"]), x["equity"]) for x in curve]
    s = [(d, e) for d, e in pts if lo <= d <= hi]
    if len(s) < 2:
        return None
    base = s[0][1]
    ret = s[-1][1] / base - 1
    peak, mdd = s[0][1], 0.0
    for _, e in s:
        peak = max(peak, e)
        mdd = min(mdd, e / peak - 1)
    return {"ret": ret, "mdd": mdd}


def run(codes, settings, client, store, sector_map, tf: bool):
    data, idx_df = fetch_data(client, store, codes, years=2)
    s, r, b = settings.strategy, settings.risk, settings.backtest
    strat = IntradayReversal(entry_pct=0.20, trend_filter=tf,
                             max_position_pct=r.max_single_position,
                             stop_loss_force_pct=r.stop_loss_force)
    broker = SimulatedBroker(commission_rate=b.commission, stamp_tax_rate=b.stamp_tax,
                             slippage=b.slippage)
    ms = classify_market_state(idx_df) if s.market_filter else None
    eng = EventDrivenEngine(strategy=strat, broker=broker, max_single=r.max_single_position,
                            market_states=ms, market_filter_on=s.market_filter,
                            market_filter_mode=s.market_filter_mode)
    res = eng.run(data, init_capital=settings.init_capital, sector_by_code=sector_map)
    curve = [{"date": str(d), "equity": round(float(v), 2)} for d, v in res.equity.items()]
    m = {k: (round(v, 4) if isinstance(v, float) else v) for k, v in res.metrics.items()}
    return {"n_with_data": len(data), "metrics": m,
            "is": seg(curve, *IS), "oos": seg(curve, *OOS)}


def main() -> int:
    settings = get_settings(str(ROOT / "config" / "settings.yaml"))
    client = TushareClient(token=settings.resolved_tushare_token(),
                           cache_dir=settings.resolved_cache_dir())
    store = DuckDBStore(settings.resolved_duckdb_path())

    latest = store.get_latest_trade_date()
    anchor = (latest - timedelta(days=2 * 365)).strftime("%Y%m%d")
    codes = eligible_codes(client, store)
    pools = build_pools(mv_at(client, anchor), codes, 50, seed=SEED)
    sector_map = build_sector_map(client, store)
    logger.info(f"锚定日 {anchor}｜池 {[(k, len(v)) for k, v in pools.items()]}")

    rows = []
    for name in POOLS:
        pair = {}
        for tf in (False, True):
            r = run(pools[name], settings, client, store, sector_map, tf)
            pair[f"tf={tf}"] = r
            logger.info(f"[{name}] tf={tf} 总收益 {r['metrics'].get('total_return'):+.2%} "
                        f"OOS {(r['oos'] or {}).get('ret', 0):+.2%}")
        rows.append({"pool": name, **pair})

    out = {"mv_anchor_date": anchor, "seed": SEED, "entry_pct": 0.20,
           "max_single_position": settings.risk.max_single_position, "rows": rows}
    p = ROOT / "outputs" / "trend_filter_oos.json"
    p.write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
    logger.info(f"写出 {p}")

    print("\n" + "=" * 100)
    print("trend_filter 换池验证（entry_pct=0.20，各池 50 只，2 年）")
    print(f"{'池':8s}{'tf=False 总收益':>18s}{'tf=True 总收益':>17s}{'差':>10s}"
          f"{'tf=F OOS':>11s}{'tf=T OOS':>11s}{'OOS差':>10s}{'tf=T OOS回撤':>14s}")
    wins = 0
    for r in rows:
        a, b = r["tf=False"], r["tf=True"]
        ma, mb = a["metrics"], b["metrics"]
        oa = (a["oos"] or {}).get("ret", 0)
        ob = (b["oos"] or {}).get("ret", 0)
        d = mb.get("total_return", 0) - ma.get("total_return", 0)
        if ob - oa > 0.02:
            wins += 1
        print(f"{r['pool']:8s}{ma.get('total_return', 0):>18.2%}{mb.get('total_return', 0):>17.2%}"
              f"{d:>+10.2%}{oa:>11.2%}{ob:>11.2%}{ob - oa:>+10.2%}"
              f"{(b['oos'] or {}).get('mdd', 0):>14.2%}")
    print(f"\n三个池里 tf=True 的 OOS 超额 >+2pp 的有 {wins}/3 个")
    print("判读：3/3 才叫信号；1~2 个是噪声；0 个直接否掉。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
