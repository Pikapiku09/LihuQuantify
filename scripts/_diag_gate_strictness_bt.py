"""闸门严格度 A/B：在「因子方向已修复」的策略上，比较三种市场闸门。

背景：P1 的 market.py 放宽（加入 MA5/MA20 反转确认 + 近5日动量）是在
「因子方向错误」的背景下调优的；方向翻正后策略变成真正的逆势反转，
需要重新确认宽松版是否仍优于原版严格版，避免引入新偏差。

三组（策略固定为修复后的 IntradayReversal = 买因子高分位）：
  S  原版严格闸门：纯 20 日累计涨跌幅 ±3% 三分段
  L  宽松闸门（当前 market.py）：+ 趋势反转确认 + 深跌强反弹保护
  N  无闸门

用法：python scripts/_diag_gate_strictness_bt.py
"""
from __future__ import annotations

import json
import random
import sys
import time
from datetime import date, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

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
END_STR = "20261110"
N_STOCKS = 150
INIT_CAPITAL = 100_000.0


def classify_market_state_strict(idx_df, window: int = 20) -> dict:
    """原版严格闸门（P1 修复前的实现，仅用于 A/B 对照）。"""
    if idx_df is None or idx_df.empty:
        return {}
    df = idx_df.sort_values("trade_date").reset_index(drop=True).copy()
    df["ret_20d"] = df["close"].pct_change(window) * 100
    states = {}
    for _, row in df.iterrows():
        if pd.isna(row["ret_20d"]):
            states[row["trade_date"]] = "未知"
        elif row["ret_20d"] >= 3:
            states[row["trade_date"]] = "上涨"
        elif row["ret_20d"] <= -3:
            states[row["trade_date"]] = "下跌"
        else:
            states[row["trade_date"]] = "震荡"
    return states


def load_token() -> str:
    obj = json.loads((ROOT / "tushareMcp.json").read_text(encoding="utf-8"))
    for _n, cfg in obj.get("mcpServers", {}).items():
        url = cfg.get("url", "")
        if "token=" in url:
            return url.split("token=")[-1].split("&")[0]
    raise SystemExit("token 未找到")


def fetch_all(client, store, codes):
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
                                        "start_date": DATA_START, "end_date": END_STR})
            if df is None or df.empty or len(df) < 300:
                continue
            df["trade_date"] = pd.to_datetime(df["trade_date"], format="%Y%m%d").dt.date
            latest = df["trade_date"].max()
            df = adjust_from_store(client, store, code, df,
                                   pd.to_datetime(DATA_START, format="%Y%m%d").date(), latest)
            data[code] = df.sort_values("trade_date").reset_index(drop=True)
        except Exception as e:
            print(f"  {code} 跳过: {type(e).__name__}")
        if i % 50 == 0:
            print(f"  已处理 {i}/{len(codes)}，有效 {len(data)}，用时 {time.time()-t0:.0f}s", flush=True)
    return data, idx_df


def run_one(strategy, data, market_states, use_filter):
    broker = SimulatedBroker(commission_rate=0.00025, stamp_tax_rate=0.0005, slippage=0.001)
    engine = EventDrivenEngine(
        strategy=strategy, broker=broker, max_single=0.15,
        market_states=market_states, market_filter_on=use_filter,
        market_filter_mode="block",
    )
    return engine.run(data, init_capital=INIT_CAPITAL, start=TRADE_START)


def main():
    pool = json.loads((ROOT / "data" / "strat_pool_n200_L5_seed42.json").read_text(encoding="utf-8"))
    random.seed(42)
    codes = random.sample(pool, min(N_STOCKS, len(pool)))

    client = TushareClient(token=load_token(), cache_dir=ROOT / "data" / "cache",
                           cache_mtime_ttl=43200.0)
    store = DuckDBStore(ROOT / "data" / "lihu_quant.duckdb")

    print(f"拉取数据：{len(codes)} 只，{DATA_START} ~ {END_STR}", flush=True)
    data, idx_df = fetch_all(client, store, codes)
    print(f"有效标的 {len(data)} 只\n", flush=True)

    def mk():
        return IntradayReversal(max_position_pct=0.15, stop_loss_force_pct=-0.08)

    runs = [
        ("S 原版严格闸门", classify_market_state_strict(idx_df), True),
        ("L 宽松闸门(当前)", classify_market_state(idx_df), True),
        ("N 无闸门", None, False),
    ]

    results = {}
    for name, ms, use in runs:
        t0 = time.time()
        print(f"回测 {name} ...", flush=True)
        try:
            results[name] = run_one(mk(), data, ms, use)
            print(f"  完成，用时 {time.time()-t0:.0f}s", flush=True)
        except Exception as e:
            print(f"  失败: {type(e).__name__}: {e}", flush=True)

    print("\n" + "=" * 100)
    print(f"{'闸门':<20}{'总收益':>10}{'年化':>9}{'夏普':>8}{'最大回撤':>10}"
          f"{'胜率':>8}{'盈亏比':>8}{'交易轮次':>9}{'终值':>10}")
    print("=" * 100)
    for name, _ms, _u in runs:
        r = results.get(name)
        if r is None:
            print(f"{name:<20}{'失败':>10}")
            continue
        m = r.metrics
        print(f"{name:<20}{m['total_return']:>9.2%}{m['annual_return']:>9.2%}"
              f"{m['sharpe']:>8.2f}{m['max_drawdown']:>10.2%}{m['win_rate']:>8.1%}"
              f"{m['profit_loss_ratio']:>8.2f}{m['total_trades']:>9}{m['final_equity']:>10,.0f}")
    print("=" * 100)

    s, l, n = (results.get("S 原版严格闸门"), results.get("L 宽松闸门(当前)"),
               results.get("N 无闸门"))
    if s and l:
        print(f"\n【严格 vs 宽松】{s.metrics['total_return']:+.2%} → "
              f"{l.metrics['total_return']:+.2%}"
              f"（{l.metrics['total_return']-s.metrics['total_return']:+.2%}）"
              f"｜夏普 {s.metrics['sharpe']:.2f} → {l.metrics['sharpe']:.2f}")
    if l and n:
        print(f"【宽松 vs 无闸门】{n.metrics['total_return']:+.2%} → "
              f"{l.metrics['total_return']:+.2%}"
              f"（闸门贡献 {l.metrics['total_return']-n.metrics['total_return']:+.2%}）")

    out = ROOT / "outputs" / "gate_strictness_compare.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps({
        "window": {"data_start": DATA_START, "trade_start": str(TRADE_START), "end": END_STR},
        "n_stocks": len(data),
        "results": {k: {**v.metrics, "signals": v.signals_generated,
                        "rejected": v.orders_rejected, "trades": len(v.trades)}
                    for k, v in results.items()},
    }, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    print(f"\n结果已保存: {out}")


if __name__ == "__main__":
    main()
