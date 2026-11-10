"""复盘：2026-10-20 ~ 2026-11-10 窗口内，新旧闸门各自怎么判。

只读本地 index_daily（已覆盖到 2026-11-10），不写任何状态。
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import duckdb  # noqa: E402
import pandas as pd  # noqa: E402

from lihu_quantify.market import classify_market_state  # noqa: E402

con = duckdb.connect(str(ROOT / "data" / "lihu_quant.duckdb"), read_only=True)
df = con.execute(
    "SELECT trade_date, close FROM index_daily "
    "WHERE ts_code='000001.SH' AND trade_date <= DATE '2026-11-10' "
    "ORDER BY trade_date"
).fetchdf()
con.close()

df["trade_date"] = pd.to_datetime(df["trade_date"]).dt.date
print(f"载入 000001.SH {len(df)} 根日线，{df['trade_date'].iloc[0]} ~ {df['trade_date'].iloc[-1]}")

# 新逻辑（直接调生产函数）
new_states = classify_market_state(df[["trade_date", "close"]], window=20)

# 旧逻辑（修复前：仅 20 日涨幅三分段）
d = df.sort_values("trade_date").reset_index(drop=True).copy()
d["ret_20d"] = d["close"].pct_change(20) * 100
d["ret_5d"] = d["close"].pct_change(5) * 100
d["ma5"] = d["close"].rolling(5).mean()
d["ma20"] = d["close"].rolling(20).mean()
d["ma5_prev"] = d["ma5"].shift(1)


def old_state(r20: float) -> str:
    if pd.isna(r20):
        return "未知"
    if r20 >= 3:
        return "上涨"
    if r20 <= -3:
        return "下跌"
    return "震荡"


START, END = pd.Timestamp("2026-10-01").date(), pd.Timestamp("2026-11-10").date()
print("\n" + "=" * 104)
print(f"{'日期':<12}{'收盘':>9}{'20日%':>9}{'5日%':>8}{'MA5>MA20':>10}{'MA5上行':>9}"
      f"{'旧判定':>8}{'新判定':>8}{'闸门(block)':>14}")
print("=" * 104)
changed = 0
for _, r in d.iterrows():
    td = r["trade_date"]
    if not (START <= td <= END):
        continue
    o = old_state(r["ret_20d"])
    n = new_states.get(td, "未知")
    ma5_above = pd.notna(r["ma5"]) and pd.notna(r["ma20"]) and r["ma5"] > r["ma20"]
    ma5_up = pd.notna(r["ma5"]) and pd.notna(r["ma5_prev"]) and r["ma5"] > r["ma5_prev"]
    gate = "允许开仓" if n == "上涨" else "禁止开新仓"
    flag = ""
    if o != n:
        changed += 1
        flag = "  ← 变了"
    print(f"{str(td):<12}{r['close']:>9.2f}{r['ret_20d']:>9.2f}"
          f"{(r['ret_5d'] if pd.notna(r['ret_5d']) else 0):>8.2f}"
          f"{('是' if ma5_above else '否'):>10}{('是' if ma5_up else '否'):>9}"
          f"{o:>8}{n:>8}{gate:>14}{flag}")

print("=" * 104)
print(f"窗口内判定发生变化的天数: {changed}")

# 停滞期统计
STAG = pd.Timestamp("2026-10-26").date()
print(f"\n=== 停滞期（{STAG} ~ 2026-11-10）逐日闸门 ===")
days = [td for td in d["trade_date"] if STAG <= td <= END]
wins_new = [td for td in days if new_states.get(td) == "上涨"]
wins_old = [td for td in days if old_state(d.loc[d["trade_date"] == td, "ret_20d"].iloc[0]) == "上涨"]
print(f"  交易日数: {len(days)}")
print(f"  旧闸门放行: {len(wins_old)} 天  {[str(x) for x in wins_old]}")
print(f"  新闸门放行: {len(wins_new)} 天  {[str(x) for x in wins_new]}")
