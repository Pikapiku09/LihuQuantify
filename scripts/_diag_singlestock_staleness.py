"""P0 诊断 v2：用生产同款参数（IntradayReversal）复现铁律1。

假设链：
  1. IntradayReversal._evaluate 对窗口内每根"超卖"bar 生成信号，且有 10 交易日限频
  2. base.latest_signal() 直接返回 signals[-1]（最后一根"合格"bar 的信号），
     不校验它是否属于最新 bar
  3. scheduler 用 df_ind.iloc[-1]（今日 bar 收盘）作执行价
  → 旧 bar 的 stop_loss vs 今日的执行价 → 铁律1「止损价不低于买入价」
"""
from __future__ import annotations

import json
import sys
from datetime import timedelta
from pathlib import Path

sys.path.insert(0, r"E:\LihuQuantify\src")

import pandas as pd

from lihu_quantify.data.tushare_client import TushareClient
from lihu_quantify.strategy.intraday_reversal import IntradayReversal

CODE = sys.argv[1] if len(sys.argv) > 1 else "605178.SH"
END = sys.argv[2] if len(sys.argv) > 2 else "20261023"
REPORTED_BUY = float(sys.argv[3]) if len(sys.argv) > 3 else None
REPORTED_STOP = float(sys.argv[4]) if len(sys.argv) > 4 else None


def load_token() -> str:
    obj = json.loads(Path(r"E:\LihuQuantify\tushareMcp.json").read_text(encoding="utf-8"))
    for _n, cfg in obj.get("mcpServers", {}).items():
        url = cfg.get("url", "")
        if "token=" in url:
            return url.split("token=")[-1].split("&")[0]
    raise SystemExit("token 未找到")


def main() -> None:
    token = load_token()
    end = pd.to_datetime(END, format="%Y%m%d").date()
    start = end - timedelta(days=500)

    client = TushareClient(token=token, cache_dir=r"E:\LihuQuantify\data\_diag_cache",
                           cache_mtime_ttl=0.0)
    df = client.query("daily", {
        "ts_code": CODE,
        "start_date": start.strftime("%Y%m%d"),
        "end_date": END,
    })
    if df.empty:
        raise SystemExit("无数据")

    df = df.sort_values("trade_date").reset_index(drop=True)
    df["trade_date"] = pd.to_datetime(df["trade_date"], format="%Y%m%d").dt.date

    strat = IntradayReversal(max_position_pct=0.15, stop_loss_force_pct=-0.08)
    sigs = strat.scan(df)
    picked = strat.latest_signal(df)

    # 构造与 scheduler 相同的 last_bar
    from lihu_quantify.indicators.standard import add_all_standard
    ind = add_all_standard(df)
    last_bar = ind.iloc[-1]
    last_date = last_bar["trade_date"]
    last_close = float(last_bar["close"])

    print("=" * 74)
    print(f"{CODE}  窗口 {df['trade_date'].iloc[0]} ~ {df['trade_date'].iloc[-1]}  ({len(df)} 行)")
    print("=" * 74)
    print(f"最新 bar : {last_date}   close={last_close:.2f}   ma10={last_bar.get('ma10')}")
    print(f"信号总数 : {len(sigs)}")

    print("\n全部信号 (trade_date / close / ma10 / stop_loss / 距今交易日):")
    tds = list(ind["trade_date"])
    for s in sigs:
        row = ind[ind["trade_date"] == s.trade_date]
        c = float(row["close"].iloc[0]); m = float(row["ma10"].iloc[0])
        lag = tds.index(last_date) - tds.index(s.trade_date)
        flag = "  <<< latest_signal 选中" if picked and s.trade_date == picked.trade_date else ""
        print(f"  {s.trade_date}  close={c:8.2f}  ma10={m:8.2f}  stop={s.stop_loss:8.2f}  滞后{lag:3d}日{flag}")

    if picked is None:
        print("\nlatest_signal() = None（无信号）")
        return

    lag = tds.index(last_date) - tds.index(picked.trade_date)
    print("\n" + "-" * 74)
    print(f"latest_signal() 返回 : {picked.trade_date}  stop_loss={picked.stop_loss:.2f}")
    print(f"scheduler 执行价     : {last_date}  close={last_close:.2f}")
    print(f"信号滞后             : {lag} 个交易日")
    print(f"铁律1 判定 (stop>=buy): {picked.stop_loss >= last_close}")
    if last_close:
        print(f"止损价高于执行价     : {(picked.stop_loss/last_close-1)*100:+.1f}%")

    if REPORTED_BUY is not None:
        print(f"\n日报执行价 {REPORTED_BUY:.2f} vs 复现 {last_close:.2f} → 匹配={abs(REPORTED_BUY-last_close)<0.02}")
    if REPORTED_STOP is not None:
        print(f"日报止损价 {REPORTED_STOP:.2f} vs 复现 {picked.stop_loss:.2f} → 匹配={abs(REPORTED_STOP-picked.stop_loss)<0.05}")

    # 关键验证：如果强制只认最新 bar 的信号，是否还会触发铁律1？
    same_day = [s for s in sigs if s.trade_date == last_date]
    print(f"\n若改为「只认最新 bar 信号」：今日信号数={len(same_day)} → "
          f"{'会买入且止损合法' if same_day else '不买（正确行为）'}")


if __name__ == "__main__":
    main()
