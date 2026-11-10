"""停滞期反事实复盘：2026-09-01 ~ 2026-11-10 整段下跌期的最优闸门对照。

背景：2026-10-26 ~ 2026-11-10 连续 12 个交易日零买入。已证明该窗口内
「旧闸门」与「新闸门」的判定完全一致（都判下跌、都禁止开仓），即
这 12 天的停滞是闸门决策的结果，而非 bug。真正的问题是：

    在整整 45 个交易日的下跌期里，闸门到底是救了我，还是坑了我？

三组（策略固定为修复后的 IntradayReversal = 买因子高分位）：
  S  原版严格闸门：纯 20 日累计涨跌幅 ±3% 三分段（P1 修复前）
  L  宽松闸门（当前 market.py）
  N  无闸门

⚠ 口径声明：
  - 每组都是「2026-09-01 从 100,000 全新起跑」的反事实，**不是**生产账本
    （生产为 88,748 / 1 持仓 / 197 笔，起始于 2026-08-25）。
  - 结果只写 outputs/replay_stagnant_window.json，绝不触碰 data/paper_state*。

用法：python scripts/replay_stagnant_window.py
"""
from __future__ import annotations

import json
import random
import sys
import time
from datetime import date
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

DATA_START = "20240601"          # 保证 250 日滚动分位有足够历史
TRADE_START = date(2026, 9, 1)   # 复盘窗口起点
END_STR = "20261110"
N_STOCKS = 150
INIT_CAPITAL = 100_000.0


def classify_market_state_strict(idx_df, window: int = 20) -> dict:
    """P1 修复前的原版闸门（纯 20 日涨幅三分段），仅作对照。"""
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


def load_pool() -> list[str]:
    for name in ("strat_pool_n200_L5_seed42.json", "strat_pool_n200_L5_seed45_20261110.json"):
        p = ROOT / "data" / name
        if p.exists():
            print(f"股票池: {name}")
            return json.loads(p.read_text(encoding="utf-8"))
    raise SystemExit("未找到股票池文件")


def fetch_all(client, store, codes):
    idx = client.query("index_daily", {"ts_code": "000001.SH", "end_date": "20301231"})
    store.upsert("index_daily", idx)
    idx_df = idx.copy()
    idx_df["trade_date"] = pd.to_datetime(idx_df["trade_date"], format="%Y%m%d").dt.date
    idx_df = idx_df.sort_values("trade_date").reset_index(drop=True)

    data, miss = {}, []
    t0 = time.time()
    for i, code in enumerate(codes, 1):
        try:
            df = client.query("daily", {"ts_code": code,
                                        "start_date": DATA_START, "end_date": END_STR})
            if df is None or df.empty or len(df) < 300:
                miss.append(code)
                continue
            df["trade_date"] = pd.to_datetime(df["trade_date"], format="%Y%m%d").dt.date
            latest = df["trade_date"].max()
            df = adjust_from_store(client, store, code, df,
                                   pd.to_datetime(DATA_START, format="%Y%m%d").date(), latest)
            data[code] = df.sort_values("trade_date").reset_index(drop=True)
        except Exception:
            miss.append(code)
        if i % 50 == 0:
            print(f"  已处理 {i}/{len(codes)}，有效 {len(data)}，用时 {time.time()-t0:.0f}s",
                  flush=True)
    if miss:
        print(f"  跳过 {len(miss)} 只（数据不足）: {miss[:5]}", flush=True)
    return data, idx_df


def run_one(data, market_states, use_filter):
    strategy = IntradayReversal(max_position_pct=0.15, stop_loss_force_pct=-0.08)
    broker = SimulatedBroker(commission_rate=0.00025, stamp_tax_rate=0.0005, slippage=0.001)
    engine = EventDrivenEngine(
        strategy=strategy, broker=broker, max_single=0.15,
        market_states=market_states, market_filter_on=use_filter,
        market_filter_mode="block",
    )
    return engine.run(data, init_capital=INIT_CAPITAL, start=TRADE_START)


def state_dist(states: dict) -> dict:
    """窗口内各状态天数分布。"""
    from collections import Counter
    c = Counter(v for k, v in states.items() if k >= TRADE_START)
    return dict(c)


def main() -> None:
    pool = load_pool()
    random.seed(42)
    codes = random.sample(pool, min(N_STOCKS, len(pool)))

    client = TushareClient(token=load_token(), cache_dir=ROOT / "data" / "cache",
                           cache_mtime_ttl=43200.0)
    store = DuckDBStore(ROOT / "data" / "lihu_quant.duckdb")

    print(f"拉取数据：{len(codes)} 只，{DATA_START} ~ {END_STR}", flush=True)
    data, idx_df = fetch_all(client, store, codes)
    print(f"有效标的 {len(data)} 只\n", flush=True)

    ms_strict = classify_market_state_strict(idx_df)
    ms_loose = classify_market_state(idx_df)
    print("=== 窗口内闸门状态天数分布（新旧对照）===")
    print("  旧严格:", state_dist(ms_strict))
    print("  新宽松:", state_dist(ms_loose))
    diff_days = [d for d in ms_loose if d >= TRADE_START and ms_strict.get(d) != ms_loose.get(d)]
    print(f"  两者判定不同的交易日: {len(diff_days)} 天 {[str(d) for d in diff_days[:10]]}")
    print()

    runs = [
        ("S 原版严格闸门", ms_strict, True),
        ("L 宽松闸门(当前)", ms_loose, True),
        ("N 无闸门", None, False),
    ]

    results = {}
    for name, ms, use in runs:
        t0 = time.time()
        print(f"回测 {name} ...", flush=True)
        try:
            results[name] = run_one(data, ms, use)
            print(f"  完成，用时 {time.time()-t0:.0f}s", flush=True)
        except Exception as e:
            print(f"  失败: {type(e).__name__}: {e}", flush=True)

    print("\n" + "=" * 108)
    print(f"{'闸门':<20}{'总收益':>10}{'最大回撤':>11}{'夏普':>8}{'胜率':>8}"
          f"{'盈亏比':>8}{'轮次':>7}{'终值':>11}{'买入拒单':>9}")
    print("=" * 108)
    for name, _ms, _u in runs:
        r = results.get(name)
        if r is None:
            print(f"{name:<20}{'失败':>10}")
            continue
        m = r.metrics
        n_tr = m.get("total_trades", 0)
        # 零交易时夏普无意义（分母为 0），显示为 "-" 而不是天文数字
        sharpe = "-" if not n_tr else f"{m['sharpe']:>8.2f}"
        print(f"{name:<20}{m['total_return']:>9.2%}{m['max_drawdown']:>11.2%}"
              f"{sharpe:>8}{m['win_rate']:>8.1%}{m['profit_loss_ratio']:>8.2f}"
              f"{n_tr:>7}{m['final_equity']:>11,.0f}{r.orders_rejected:>9}")
    print("=" * 108)

    s, l, n = (results.get("S 原版严格闸门"), results.get("L 宽松闸门(当前)"),
               results.get("N 无闸门"))
    print()
    if l and n:
        print(f"【闸门是否值得】无闸门 {n.metrics['total_return']:+.2%} → "
              f"有闸门(当前) {l.metrics['total_return']:+.2%}，"
              f"闸门贡献 {l.metrics['total_return']-n.metrics['total_return']:+.2%}")
        print(f"                   回撤 {n.metrics['max_drawdown']:.2%} → "
              f"{l.metrics['max_drawdown']:.2%}")
    if s and l:
        print(f"【P1 放宽是否更好】严格 {s.metrics['total_return']:+.2%} → "
              f"宽松 {l.metrics['total_return']:+.2%}"
              f"（{l.metrics['total_return']-s.metrics['total_return']:+.2%}）")

    out = ROOT / "outputs" / "replay_stagnant_window.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "disclaimer": "反事实复盘：每组自 2026-09-01 以 100,000 全新起跑，"
                      "非生产账本（生产为 88,748/1持仓/197笔，起始 2026-08-25）。",
        "window": {"data_start": DATA_START, "trade_start": str(TRADE_START), "end": END_STR},
        "n_stocks": len(data),
        "gate_state_days": {"strict": state_dist(ms_strict), "loose": state_dist(ms_loose)},
        "gate_diff_days": [str(d) for d in diff_days],
        "results": {
            k: {**v.metrics, "signals": v.signals_generated,
                "rejected": v.orders_rejected, "trades": len(v.trades)}
            for k, v in results.items()
        },
        "trades_by_group": {
            k: [
                {"date": str(t.trade_date), "code": t.ts_code, "side": t.side,
                 "price": round(float(t.price), 3), "volume": int(t.volume),
                 "amount": round(float(t.price) * int(t.volume), 2),
                 "pnl": round(float(t.pnl), 2), "reason": t.reason}
                for t in v.trades
            ]
            for k, v in results.items()
        },
    }
    out.write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=str),
                   encoding="utf-8")
    print(f"\n结果已保存: {out}")


if __name__ == "__main__":
    main()
