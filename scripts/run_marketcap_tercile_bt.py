"""市值三分位池对比回测（H-2026-11-16-006）。

回答的问题
----------
用户买入时市值分位中位 **93.3%**（只买全市场市值前 7%）。A 股历史上有小市值效应，
那么「把股票池换成小盘」会不会更好？用户自己的 83 波样本回答不了这个反事实
（全是他自己下的单），必须做独立回测。

设计（防自欺的三条硬约束）
--------------------------
1. **市值按回测窗口起点取**，不是按今天取。用今天的市值切池 = 前视偏差：
   今天市值大的票，恰恰是这两年涨上去的，用它切池会人为把「赢家」塞进大盘池。
2. **三个池股票数相同**（默认各 50），否则池子大的一方只是撞上更多机会，不是「小盘更好」。
3. **只改股票池**：同一策略、同一窗口、同一初始资金、同一单票上限、同一板块映射。

输出
----
`outputs/marketcap_tercile_bt.json`：三个池的 metrics + 池构成摘要。

用法
----
    python scripts/run_marketcap_tercile_bt.py --n 50 --years 2
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import timedelta
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from loguru import logger  # noqa: E402

from lihu_quantify.config import get_settings  # noqa: E402
from lihu_quantify.data.duckdb_store import DuckDBStore  # noqa: E402
from lihu_quantify.data.tushare_client import TushareClient  # noqa: E402
from lihu_quantify.strategy.cherry_claw import CherryClaw  # noqa: E402
from lihu_quantify.backtest.broker import SimulatedBroker  # noqa: E402
from lihu_quantify.backtest.engine import EventDrivenEngine  # noqa: E402
from lihu_quantify.market import classify_market_state  # noqa: E402

from run_full_backtest import build_sector_map, fetch_data  # noqa: E402

SEED = 42
POOLS = ("大盘", "中盘", "小盘")


def eligible_codes(client: TushareClient, store: DuckDBStore) -> list[str]:
    """主板池：剔除 688/300/301/北交所/ST/已退市。"""
    basic = client.query("stock_basic", {"list_status": "L"})
    if basic.empty:
        return []
    store.upsert("stock_basic", basic, date_cols=("list_date", "delist_date"))
    df = basic[basic["ts_code"].notna()].copy()
    code = df["ts_code"].astype(str)
    mask = ~code.str.startswith(("688", "300", "301", "4", "8"))
    if "name" in df.columns:
        mask &= ~df["name"].astype(str).str.contains("ST", na=False)
    if "delist_date" in df.columns:
        mask &= df["delist_date"].isna()
    return df.loc[mask, "ts_code"].tolist()


def mv_at(client: TushareClient, anchor: str) -> pd.DataFrame:
    """锚定日的全市场市值横截面（非交易日往前找最多 8 天）。"""
    from datetime import date

    cur = date(int(anchor[:4]), int(anchor[4:6]), int(anchor[6:]))
    for _ in range(8):
        key = cur.strftime("%Y%m%d")
        try:
            df = client.query("daily_basic", {"trade_date": key}, fields=["ts_code", "total_mv"])
        except Exception:
            df = pd.DataFrame()
        if not df.empty:
            logger.info(f"市值锚定日 {key}：{len(df):,} 只")
            return df
        cur -= timedelta(days=1)
    return pd.DataFrame()


def build_pools(mv: pd.DataFrame, codes: list[str], n: int, seed: int = SEED) -> dict[str, list[str]]:
    """按市值三分位切池，各抽 n 只（固定种子，可复现）。"""
    d = mv[mv["ts_code"].isin(codes) & mv["total_mv"].notna()].copy()
    d = d[d["total_mv"] > 0].sort_values("total_mv")
    if len(d) < 60:
        raise SystemExit(f"市值数据不足（{len(d)} 只），无法三分位")
    # 注意：pd.qcut 的 labels 是**按升序**分配的。POOLS 写成 ("大盘","中盘","小盘")
    # 会让最小市值池挂上「大盘」这个名字（2026-11-16 首次跑就踩了，整张表列反）。
    # 这里显式按市值升序给标签：小盘 -> 中盘 -> 大盘。
    d["tercile"] = pd.qcut(d["total_mv"], 3, labels=["小盘", "中盘", "大盘"])
    rng = np.random.default_rng(seed)
    out: dict[str, list[str]] = {}
    for p in POOLS:
        pool = d[d["tercile"] == p]["ts_code"].tolist()
        pick = rng.choice(sorted(pool), size=min(n, len(pool)), replace=False)
        out[p] = sorted(pick.tolist())
        mv_med = d[d["tercile"] == p]["total_mv"].median() / 1e4  # 万元 → 亿元
        logger.info(f"{p}池：候选 {len(pool)} 只，抽 {len(out[p])} 只，市值中位 {mv_med:,.0f} 亿")
    # 自检：大盘池市值中位必须大于中盘，中盘必须大于小盘（防止标签再次挂反）
    meds = {p: d[d["tercile"] == p]["total_mv"].median() for p in POOLS}
    if not (meds["大盘"] > meds["中盘"] > meds["小盘"]):
        raise SystemExit(f"分池自检失败，标签可能挂反：{meds}")
    logger.info("分池自检通过：大盘 > 中盘 > 小盘")
    return out


def make_strategy(name: str, settings):
    """按名字构造策略。intraday_reversal 才是实盘主策略（settings.main_strategy）。"""
    s, r = settings.strategy, settings.risk
    if name == "intraday_reversal":
        from lihu_quantify.strategy.intraday_reversal import IntradayReversal

        return IntradayReversal(
            max_position_pct=r.max_single_position,
            stop_loss_force_pct=r.stop_loss_force,
        )
    return CherryClaw(
        ma_periods=tuple(s.ma_periods),
        golden_cross_max_freshness=s.golden_cross_max_freshness_days,
        volume_ratio_threshold=s.volume_ratio_threshold,
        entity_ratio_threshold=s.entity_ratio_threshold,
        close_to_ma5_max_dev=s.close_to_ma5_max_dev,
        max_position_pct=r.max_single_position,
        stop_loss_force_pct=r.stop_loss_force,
    )


def run_pool(name: str, codes: list[str], settings, client, store, sector_map, years: int,
             strategy_name: str = "cherry_claw") -> dict:
    data, idx_df = fetch_data(client, store, codes, years=years)
    logger.info(f"[{name}] 实际取到数据 {len(data)} 只")
    if not data:
        return {"pool": name, "error": "无数据"}

    s, r, b = settings.strategy, settings.risk, settings.backtest
    strategy = make_strategy(strategy_name, settings)
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
    return {
        "pool": name,
        "n_codes": len(codes),
        "n_with_data": len(data),
        "metrics": {k: (round(v, 4) if isinstance(v, float) else v)
                    for k, v in result.metrics.items()},
        "equity_curve": curve,
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=50, help="每个池的股票数")
    ap.add_argument("--years", type=int, default=2)
    ap.add_argument("--seed", type=int, default=SEED, help="抽样种子（换种子做稳健性检验）")
    ap.add_argument("--strategy", default="cherry_claw", choices=["cherry_claw", "intraday_reversal"],
                    help="回测策略；intraday_reversal 才是实盘主策略")
    ap.add_argument("--out", default="outputs/marketcap_tercile_bt.json")
    args = ap.parse_args()

    settings = get_settings(str(ROOT / "config" / "settings.yaml"))
    token = settings.resolved_tushare_token()
    client = TushareClient(token=token, cache_dir=settings.resolved_cache_dir())
    store = DuckDBStore(settings.resolved_duckdb_path())

    latest = store.get_latest_trade_date()
    anchor = (latest - timedelta(days=args.years * 365)).strftime("%Y%m%d")
    logger.info(f"回测区间起点约 {anchor}，终点 {latest}；市值按起点取（避免前视偏差）")

    codes = eligible_codes(client, store)
    mv = mv_at(client, anchor)
    if not codes or mv.empty:
        logger.error("池或市值不可用，退出")
        return 1
    pools = build_pools(mv, codes, args.n, seed=args.seed)
    sector_map = build_sector_map(client, store)

    results = []
    for name in POOLS:
        logger.info("=" * 60)
        logger.info(f"跑 {name}池：{len(pools[name])} 只")
        results.append(run_pool(name, pools[name], settings, client, store, sector_map,
                                args.years, strategy_name=args.strategy))

    out = {
        "generated_at": str(latest),
        "seed": args.seed,
        "n_per_pool": args.n,
        "years": args.years,
        "mv_anchor_date": anchor,
        "strategy": args.strategy,
        "max_single_position": settings.risk.max_single_position,
        "init_capital": settings.init_capital,
        "survivorship_bias": "按当前上市股票构建，历史回测收益系统性偏高（乐观口径，三个池同等受影响）",
        "pools": {k: v for k, v in pools.items()},
        "results": results,
    }
    path = ROOT / args.out
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
    logger.info(f"写出 {path}")

    # ---- 汇总 ----
    print("\n" + "=" * 78)
    print(f"市值三分位池对比（各 {args.n} 只，策略 {args.strategy}，种子 {args.seed}，窗口 {args.years} 年，单票上限 {settings.risk.max_single_position:.0%}）")
    keys = ["total_return", "annual_return", "max_drawdown", "sharpe", "calmar", "win_rate", "profit_factor"]
    hdr = f"{'指标':16s}" + "".join(f"{p:>12s}" for p in POOLS)
    print(hdr)
    for k in keys:
        row = f"{k:16s}"
        for r in results:
            v = r.get("metrics", {}).get(k)
            row += f"{v:>12.4f}" if isinstance(v, (int, float)) else f"{'-':>12s}"
        print(row)
    print(f"{'取到数据':16s}" + "".join(f"{r.get('n_with_data', 0):>12d}" for r in results))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
