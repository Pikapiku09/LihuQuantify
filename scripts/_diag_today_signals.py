"""回归工具：每日信号新鲜度体检（P0-A 验收/巡检用）。

用途：抽样股票池，统计「今日有效信号 / 有历史信号但今日无（应丢弃）/ 完全无信号」，
并把修复前行为（signals[-1] 回退）作为对照一起打印，便于回归时一眼看出差异。

背景（2026-11-10 P0-A）：
    base.latest_signal 原实现 `return signals[-1]` 会把「窗口内最后一根合格 bar」
    的历史信号当今日信号返回，交易日最多滞后 134 个交易日，导致：
      - 池内 200 只几乎每天都有「信号」（僵尸信号堆积），
      - 旧止损价配今日执行价 → 铁律1「止损价不低于买入价」拦截，
      - 通过铁律1 的则按已经反弹后的价格成交（追高）。

用法：
    python scripts/_diag_today_signals.py [YYYYMMDD] [样本数]
    默认：今天（本地日期）与 15 只
"""
from __future__ import annotations

import json
import random
import sys
from datetime import date, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import pandas as pd

from lihu_quantify.data.tushare_client import TushareClient
from lihu_quantify.strategy.intraday_reversal import IntradayReversal

POOL_FILE = ROOT / "data" / "strat_pool_n200_L5_seed42.json"
CACHE_DIR = ROOT / "data" / "_diag_cache"


def load_token() -> str:
    obj = json.loads((ROOT / "tushareMcp.json").read_text(encoding="utf-8"))
    for _n, cfg in obj.get("mcpServers", {}).items():
        url = cfg.get("url", "")
        if "token=" in url:
            return url.split("token=")[-1].split("&")[0]
    raise SystemExit("未能在 tushareMcp.json 中定位 token")


def main() -> None:
    today = sys.argv[1] if len(sys.argv) > 1 else date.today().strftime("%Y%m%d")
    n_sample = int(sys.argv[2]) if len(sys.argv) > 2 else 15

    pool = json.loads(POOL_FILE.read_text(encoding="utf-8"))
    random.seed(42)
    codes = random.sample(pool, min(n_sample, len(pool)))

    end = pd.to_datetime(today, format="%Y%m%d").date()
    start = end - timedelta(days=500)
    client = TushareClient(token=load_token(), cache_dir=CACHE_DIR, cache_mtime_ttl=0.0)
    strat = IntradayReversal(max_position_pct=0.15, stop_loss_force_pct=-0.08)

    rows = []
    for code in codes:
        try:
            df = client.query("daily", {"ts_code": code,
                                        "start_date": start.strftime("%Y%m%d"),
                                        "end_date": today})
            if df is None or df.empty or len(df) < 30:
                continue
            df = df.sort_values("trade_date").reset_index(drop=True)
            df["trade_date"] = pd.to_datetime(df["trade_date"], format="%Y%m%d").dt.date
            last_date = df["trade_date"].iloc[-1]
            last_close = float(df["close"].iloc[-1])

            all_sigs = strat.scan(df)
            sig_today = strat.latest_signal(df)      # 修复后：只认当日
            old_behaviour = all_sigs[-1] if all_sigs else None   # 修复前：signals[-1]

            if sig_today is not None:
                status = "今日有效"
                lag = 0
                detail = f"止损{sig_today.stop_loss:.2f} vs 今价{last_close:.2f}"
            elif all_sigs:
                status = "历史信号·应丢弃"
                lag = len(df) - 1 - list(df["trade_date"]).index(old_behaviour.trade_date)
                detail = (f"最后信号 {old_behaviour.trade_date}（滞后{lag}日）"
                          f" 修复前会被当今日信号使用")
            else:
                status, lag, detail = "无信号", 0, "-"

            rows.append((code, str(last_date), status, lag, detail))
        except Exception as e:
            rows.append((code, "ERR", f"{type(e).__name__}", 0, str(e)[:40]))

    print("=" * 116)
    print(f"{'代码':<12}{'最新bar':>12}  {'判定':<20}{'滞后':>6}   说明")
    print("=" * 116)
    for code, ld, status, lag, detail in rows:
        print(f"{code:<12}{ld:>12}  {status:<20}{lag:>5}日   {detail}")

    fresh = sum(1 for r in rows if r[2] == "今日有效")
    dropped = sum(1 for r in rows if r[2] == "历史信号·应丢弃")
    none_ = sum(1 for r in rows if r[2] == "无信号")
    err = sum(1 for r in rows if r[2] not in ("今日有效", "历史信号·应丢弃", "无信号"))

    print("-" * 116)
    print(f"抽样 {len(rows)} 只：今日有效 {fresh}｜历史信号应丢弃 {dropped}｜无信号 {none_}｜异常 {err}")
    if fresh:
        print(f"→ 按 {len(pool)} 只池子折算，当日有效信号约 {round(fresh / len(rows) * len(pool))} 个")
    if dropped:
        lags = [r[3] for r in rows if r[2] == "历史信号·应丢弃"]
        print(f"→ 被正确丢弃的 {dropped} 只，平均滞后 {sum(lags)/len(lags):.1f} 个交易日"
              f"（最长 {max(lags)}）——修复前这些都会被当信号下单")


if __name__ == "__main__":
    main()
