"""P0-B 回测对比：IntradayReversal 因子方向 —— 原版 vs 反向。

原版  mask = ir20_pct <= 0.20   （买入因子最低 20% 分位）
反向  mask = ir20_pct >= 0.80   （买入因子最高 20% 分位）
       —— 用 ir20_pct → 1 - ir20_pct 实现，不改动 _evaluate。

同时交叉验证市场闸门（block/无过滤）两个维度，共 4 组:
  A  原方向 + block     （≈ 当前生产配置）
  B  反方向 + block
  C  原方向 + 无闸门
  D  反方向 + 无闸门

数据窗口 2024-06-01 ~ 最新；交易窗口 2025-01-01 ~ 最新（留足 ir20_pct 的 250 日预热）。
"""
from __future__ import annotations

import json
import random
import sys
import time
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, r"E:\LihuQuantify\src")

import pandas as pd

from lihu_quantify.backtest.broker import SimulatedBroker
from lihu_quantify.backtest.engine import EventDrivenEngine
from lihu_quantify.data.adjustment import adjust_from_store
from lihu_quantify.data.duckdb_store import DuckDBStore
from lihu_quantify.data.tushare_client import TushareClient
from lihu_quantify.market import classify_market_state
from lihu_quantify.strategy.intraday_reversal import IntradayReversal

DATA_START = "20240601"
TRADE_START = date(2025, 1, 1)
N_STOCKS = 150
INIT_CAPITAL = 100_000.0


class FlippedIntradayReversal(IntradayReversal):
    """把 ir20_pct 取反 → mask(<=0.20) 等价选中原始因子的最高 20% 分位。"""

    def _prepare_indicators(self, df):
        out = super()._prepare_indicators(df)
        d = out.get("df")
        if d is not None and "ir20_pct" in d.columns:
            d = d.copy()
            d["ir20_pct"] = 1.0 - d["ir20_pct"]
            out = dict(out)
            out["df"] = d
        return out


def load_token() -> str:
    obj = json.loads(Path(r"E:\LihuQuantify\tushareMcp.json").read_text(encoding="utf-8"))
    for _n, cfg in obj.get("mcpServers", {}).items():
        url = cfg.get("url", "")
        if "token=" in url:
            return url.split("token=")[-1].split("&")[0]
    raise SystemExit("token 未找到")


def fetch_all(client, store, codes, end_str):
    idx = client.query("index_daily", {"ts_code": "000001.SH", "end_date": "20301231"})
    store.upsert("index_daily", idx)
    idx_df = idx.copy()
    idx_df["trade_date"] = pd.to_datetime(idx_df["trade_date"], format="%Y%m%d").dt.date
    idx_df = idx_df.sort_values("trade_date").reset_index(drop=True)

    data = {}
    t0 = time.time()
    for i, code in enumerate(codes, 1):
        try:
            df = client.query("daily", {"ts_code": code,
                                        "start_date": DATA_START, "end_date": end_str})
            if df is None or df.empty or len(df) < 300:
                continue
            df["trade_date"] = pd.to_datetime(df["trade_date"], format="%Y%m%d").dt.date
            latest = df["trade_date"].max()
            df = adjust_from_store(client, store, code, df,
                                   pd.to_datetime(DATA_START, format="%Y%m%d").date(), latest)
            data[code] = df.sort_values("trade_date").reset_index(drop=True)
        except Exception as e:
            print(f"  {code} 跳过: {type(e).__name__}")
        if i % 25 == 0:
            print(f"  已处理 {i}/{len(codes)}，有效 {len(data)}，用时 {time.time()-t0:.0f}s", flush=True)
    return data, idx_df


def run_one(strategy, data, market_states, filter_mode):
    broker = SimulatedBroker(commission_rate=0.00025, stamp_tax_rate=0.0005, slippage=0.001)
    engine = EventDrivenEngine(
        strategy=strategy, broker=broker,
        max_single=0.15,
        market_states=market_states,
        market_filter_on=filter_mode is not None,
        market_filter_mode=filter_mode or "block",
    )
    return engine.run(data, init_capital=INIT_CAPITAL, start=TRADE_START)


def main():
    token = load_token()
    end_str = "20261110"
    pool = json.loads(Path(r"E:\LihuQuantify\data\strat_pool_n200_L5_seed42.json")
                      .read_text(encoding="utf-8"))
    random.seed(42)
    codes = random.sample(pool, min(N_STOCKS, len(pool)))

    client = TushareClient(token=token,
                           cache_dir=Path(r"E:\LihuQuantify\data\cache"),
                           cache_mtime_ttl=43200.0)
    store = DuckDBStore(r"E:\LihuQuantify\data\lihu_quant.duckdb")

    print(f"拉取数据：{len(codes)} 只，{DATA_START} ~ {end_str}", flush=True)
    data, idx_df = fetch_all(client, store, codes, end_str)
    print(f"有效标的 {len(data)} 只\n", flush=True)

    market_states = classify_market_state(idx_df)

    def mk(flipped):
        cls = FlippedIntradayReversal if flipped else IntradayReversal
        return cls(max_position_pct=0.15, stop_loss_force_pct=-0.08)

    runs = [
        ("A 修复后(高分位)+block", mk(False), market_states, "block"),
        ("B 修复前(低分位)+block", mk(True), market_states, "block"),
        ("C 修复后(高分位)+无闸门", mk(False), None, None),
        ("D 修复前(低分位)+无闸门", mk(True), None, None),
    ]

    results = {}
    for name, strat, ms, mode in runs:
        t0 = time.time()
        print(f"回测 {name} ...", flush=True)
        try:
            results[name] = run_one(strat, data, ms, mode)
            print(f"  完成，用时 {time.time()-t0:.0f}s", flush=True)
        except Exception as e:
            print(f"  失败: {type(e).__name__}: {e}", flush=True)

    print("\n" + "=" * 104)
    print(f"{'组合':<18}{'总收益':>10}{'年化':>9}{'夏普':>8}{'最大回撤':>10}"
          f"{'胜率':>8}{'盈亏比':>8}{'交易轮次':>9}{'均持仓天':>9}{'终值':>10}")
    print("=" * 104)
    for name, _unused, _m, _mode in runs:
        r = results.get(name)
        if r is None:
            print(f"{name:<18}{'运行失败':>10}")
            continue
        m = r.metrics
        print(f"{name:<18}{m['total_return']:>9.2%}{m['annual_return']:>9.2%}"
              f"{m['sharpe']:>8.2f}{m['max_drawdown']:>10.2%}{m['win_rate']:>8.1%}"
              f"{m['profit_loss_ratio']:>8.2f}{m['total_trades']:>9}{m['avg_holding_days']:>9.1f}"
              f"{m['final_equity']:>10,.0f}")
    print("=" * 104)

    if not results:
        return

    print("\n【方向效应】(修复前低分位 → 修复后高分位)")
    for a, b, tag in [("B 修复前(低分位)+block", "A 修复后(高分位)+block", "有闸门"),
                      ("D 修复前(低分位)+无闸门", "C 修复后(高分位)+无闸门", "无闸门")]:
        if a in results and b in results:
            ra, rb = results[a].metrics, results[b].metrics
            print(f"  {tag}：总收益 {ra['total_return']:+.2%} → {rb['total_return']:+.2%} "
                  f"（{rb['total_return']-ra['total_return']:+.2%}）｜"
                  f"胜率 {ra['win_rate']:.1%} → {rb['win_rate']:.1%}｜"
                  f"夏普 {ra['sharpe']:.2f} → {rb['sharpe']:.2f}")

    print("\n【闸门效应】(无闸门 → 加闸门)")
    for a, b, tag in [("C 修复后(高分位)+无闸门", "A 修复后(高分位)+block", "修复后"),
                      ("D 修复前(低分位)+无闸门", "B 修复前(低分位)+block", "修复前")]:
        if a in results and b in results:
            ra, rb = results[a].metrics, results[b].metrics
            print(f"  {tag}：无闸门 {ra['total_return']:+.2%} vs 加闸门 {rb['total_return']:+.2%} "
                  f"（闸门贡献 {rb['total_return']-ra['total_return']:+.2%}）")

    out = Path(r"E:\LihuQuantify\outputs\p0b_direction_compare.json")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps({
        "window": {"data_start": DATA_START, "trade_start": str(TRADE_START), "end": end_str},
        "n_stocks": len(data),
        "results": {k: {**v.metrics, "signals": v.signals_generated,
                        "rejected": v.orders_rejected, "trades": len(v.trades)}
                    for k, v in results.items()},
    }, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    print(f"\n结果已保存: {out}")


if __name__ == "__main__":
    main()
