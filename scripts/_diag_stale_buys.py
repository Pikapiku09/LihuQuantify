"""P0 批量验证：实际被买入的票，信号滞后多久、追高多少。

对 NAS 上 2026-10-23 的 8 笔真实买入，逐只复现：
  - latest_signal() 返回的信号来自哪一天（滞后几个交易日）
  - 信号生成时的 close vs 实际执行价（涨跌 %）
  - 该信号当日最新 bar 是否本身有信号（若无 → 说明是旧信号被硬套今日价格）
"""
from __future__ import annotations

import json
import sys
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, r"E:\LihuQuantify\src")

import pandas as pd

from lihu_quantify.data.tushare_client import TushareClient
from lihu_quantify.indicators.standard import add_all_standard
from lihu_quantify.strategy.intraday_reversal import IntradayReversal

EXEC_DATE = "20261023"
# NAS 实盘成交（10-23）
BUYS = [
    ("000504.SZ", 8.97), ("000532.SZ", 11.03), ("000598.SZ", 6.91),
    ("000651.SZ", 37.23), ("000727.SZ", 2.17), ("000786.SZ", 16.52),
    ("000868.SZ", 3.48), ("600316.SH", 37.28),
]


def load_token() -> str:
    obj = json.loads(Path(r"E:\LihuQuantify\tushareMcp.json").read_text(encoding="utf-8"))
    for _n, cfg in obj.get("mcpServers", {}).items():
        url = cfg.get("url", "")
        if "token=" in url:
            return url.split("token=")[-1].split("&")[0]
    raise SystemExit("token 未找到")


def main() -> None:
    token = load_token()
    client = TushareClient(token=token, cache_dir=r"E:\LihuQuantify\data\_diag_cache",
                           cache_mtime_ttl=0.0)
    end = pd.to_datetime(EXEC_DATE, format="%Y%m%d").date()
    start = end - timedelta(days=500)
    strat = IntradayReversal(max_position_pct=0.15, stop_loss_force_pct=-0.08)

    rows = []
    for code, exec_price in BUYS:
        try:
            df = client.query("daily", {
                "ts_code": code,
                "start_date": start.strftime("%Y%m%d"),
                "end_date": EXEC_DATE,
            })
            if df.empty:
                print(f"{code}: 无数据"); continue
            df = df.sort_values("trade_date").reset_index(drop=True)
            df["trade_date"] = pd.to_datetime(df["trade_date"], format="%Y%m%d").dt.date
            ind = add_all_standard(df)
            sigs = strat.scan(df)
            picked = strat.latest_signal(df)
            last_bar = ind.iloc[-1]
            tds = list(ind["trade_date"])
            last_date = last_bar["trade_date"]

            if picked is None:
                print(f"{code}: 无信号"); continue

            lag = tds.index(last_date) - tds.index(picked.trade_date)
            sig_close = picked.suggested_price
            chg = (exec_price / sig_close - 1) * 100 if sig_close else float("nan")
            stop_ok = picked.stop_loss < exec_price
            same_day = [s for s in sigs if s.trade_date == last_date]

            rows.append({
                "code": code, "exec_px": exec_price, "sig_date": str(picked.trade_date),
                "sig_close": sig_close, "stop": picked.stop_loss,
                "lag": lag, "chg": chg, "stop_ok": stop_ok,
                "fresh": len(same_day) > 0, "today_close": float(last_bar["close"]),
            })
        except Exception as e:
            print(f"{code}: 失败 {type(e).__name__}: {e}")

    print("\n" + "=" * 100)
    print(f"{'代码':<12}{'执行价':>8}{'信号日':>13}{'信号价':>9}{'止损':>9}{'滞后':>6}{'涨跌%':>9}{'过铁律1':>8}{'今日有信号':>10}")
    print("=" * 100)
    for r in rows:
        print(f"{r['code']:<12}{r['exec_px']:>8.2f}{r['sig_date']:>13}{r['sig_close']:>9.2f}"
              f"{r['stop']:>9.2f}{r['lag']:>5d}日{r['chg']:>+9.1f}{'是' if r['stop_ok'] else '否':>8}"
              f"{'是' if r['fresh'] else '否':>10}")

    if rows:
        n = len(rows)
        passed = [r for r in rows if r["stop_ok"]]
        fresh = [r for r in rows if r["fresh"]]
        avg_lag = sum(r["lag"] for r in rows) / n
        avg_chg = sum(r["chg"] for r in rows) / n
        print("-" * 100)
        print(f"样本 {n} 只：通过铁律1 的 {len(passed)} 只；当日真有信号的 {len(fresh)} 只")
        print(f"平均信号滞后 {avg_lag:.1f} 个交易日；相对信号价平均变动 {avg_chg:+.1f}%")
        if passed:
            print(f"通过铁律1 的 {len(passed)} 只，相对信号价平均变动 "
                  f"{sum(r['chg'] for r in passed)/len(passed):+.1f}% "
                  f"（正=追高买入）")


if __name__ == "__main__":
    main()
