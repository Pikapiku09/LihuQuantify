# -*- coding: utf-8 -*-
"""
账户级月度绩效与回撤分析（交易视角权益曲线）
================================================
输入
  交割单  E:/Dsh_WorkSapce/Dify_Agents/投研反思/Table_1254.csv + Table_1010.csv（GBK）
          解析用现成模块 lihu_quantify.behavior.ingest.load_fills
  行情    data/_team/quotes.parquet（ts_code, trade_date=YYYYMMDD, close）—— 原始价，非复权
  大盘    data/_team/lihu_quant_copy.duckdb（read_only）的 index_daily 000001.SH

输出
  outputs/monthly_perf.json
  同时把可读摘要打印到 stdout

核心口径
  权益(t) = 初始资金 + 累计现金流 + Σ(当日持仓数量 × 当日收盘价)
  初始资金 110000 元 —— **这是假设值**（用户账户量级约 11 万，真实初始资金未知）

必须知道的三个限制（报告与 JSON 里都写了）
  1) 初始资金 110000 是假设，不是交割单里的事实
  2) 只覆盖这一个证券账户；用户可能还有理财、其他账户、场外资产
  3) 持仓按「原始收盘价」计价，不含分红再投；现金分红到账会体现为价格下跌 + 现金流入的错位
"""
import sys

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import json
from collections import Counter, defaultdict
from pathlib import Path

import duckdb
import numpy as np
import pandas as pd

sys.path.insert(0, r"E:\LihuQuantify\src")
from lihu_quantify.behavior.ingest import load_fills  # noqa: E402

ROOT = Path(r"E:\LihuQuantify")
FILL_CSVS = [
    Path(r"E:\Dsh_WorkSapce\Dify_Agents\投研反思\Table_1254.csv"),
    Path(r"E:\Dsh_WorkSapce\Dify_Agents\投研反思\Table_1010.csv"),
]
QUOTES = ROOT / "data" / "_team" / "quotes.parquet"
IDXDB = ROOT / "data" / "_team" / "lihu_quant_copy.duckdb"
OUT_JSON = ROOT / "outputs" / "monthly_perf.json"

INIT_CAPITAL = 110000.0   # 假设值
BENCH_CODE = "000001.SH"  # 上证指数


def hr(t=""):
    print("\n" + "=" * 96)
    if t:
        print(t)
        print("=" * 96)


# ============================================================ 1 读交割单
hr("一、交割单读取与清洗")
frames = []
for p in FILL_CSVS:
    df = pd.DataFrame(load_fills(p))
    df["src"] = p.name
    frames.append(df)
    print(f"  {p.name}: {len(df)} 笔，{df['trade_date'].min()} ~ {df['trade_date'].max()}")
A, B = frames[0], frames[1]

# 两个文件在边界日（2024-11-15）有重叠：只删「同时在两个文件里出现」的那份。
# 不能用内容去重——同文件内存在价格/数量完全相同的**真实分笔**（成交编号不同、股份余额递减）。
KEY = ["trade_date", "ts_code", "side", "price", "volume", "amount", "fee"]
cnt_a = Counter(map(tuple, A[KEY].values))
used = Counter()
keep_idx = []
for i, row in enumerate(B.itertuples(index=False)):
    k = tuple(getattr(row, c) for c in KEY)
    if cnt_a[k] - used[k] > 0:
        used[k] += 1          # 与 A 重复，丢弃 B 的这一份
    else:
        keep_idx.append(i)
B2 = B.iloc[keep_idx].reset_index(drop=True)
fills = pd.concat([A, B2], ignore_index=True)
n_cross_dup = len(B) - len(B2)
print(f"跨文件重复删除 {n_cross_dup} 笔 → 合并 {len(fills)} 笔")
print(f"  买入 {int((fills['side']=='buy').sum())} / 卖出 {int((fills['side']=='sell').sum())}"
      f"｜标的 {fills['ts_code'].nunique()} 只")

fills["d"] = pd.to_datetime(fills["trade_date"])
fills = fills.sort_values(["d", "ts_code"], kind="stable").reset_index(drop=True)


# ============================================================ 2 行情
hr("二、行情数据")
con = duckdb.connect()
q = con.execute(
    "select ts_code, trade_date, close from read_parquet(?)", [str(QUOTES)]
).df()
q["d"] = pd.to_datetime(q["trade_date"], format="%Y%m%d")
print(f"  quotes.parquet: {len(q):,} 行，{q['ts_code'].nunique()} 只标的，"
      f"{q['d'].min().date()} ~ {q['d'].max().date()}")

# 后缀修正：ingest 的代码映射对 56xxxx 会给出 .SZ，实际是 .SH
have = set(q["ts_code"].unique())
fixed = 0
for i, c in enumerate(fills["ts_code"].values):
    if c in have:
        continue
    alt = c[:-3] + (".SZ" if c.endswith(".SH") else ".SH")
    if alt in have:
        fills.at[i, "ts_code"] = alt
        fixed += 1
print(f"  交易所后缀修正 {fixed} 笔（如 561360.SZ→.SH）")

no_quote = sorted(set(fills["ts_code"]) - have)
print(f"  无任何行情的标的 {len(no_quote)} 只: {no_quote}")

px = q.pivot_table(index="d", columns="ts_code", values="close", aggfunc="last").sort_index()
cal_all = px.index
qmax = cal_all.max()

win_start = fills["d"].min()
win_end = min(qmax, fills["d"].max())
cal = cal_all[(cal_all >= win_start) & (cal_all <= win_end)]
print(f"  权益曲线区间 {win_start.date()} ~ {win_end.date()}，{len(cal)} 个交易日")

dropped = fills[fills["d"] > win_end]
if len(dropped):
    print(f"  ⚠️ 有 {len(dropped)} 笔成交晚于行情末日（{win_end.date()}），"
          f"无法计入权益曲线：{sorted(dropped['trade_date'].astype(str).unique())}")
fills_in = fills[fills["d"] <= win_end].reset_index(drop=True)


# ============================================================ 3 期初持仓
hr("三、期初持仓识别（记录开始前已持有、无买入记录）")
net_all = fills_in.groupby("ts_code")["volume"].sum().to_frame("n")
net_all["signed"] = [
    int(fills_in.loc[fills_in["ts_code"] == c, "volume"].where(
        fills_in.loc[fills_in["ts_code"] == c, "side"] == "buy",
        -fills_in.loc[fills_in["ts_code"] == c, "volume"]).sum())
    for c in net_all.index
]
opening = {c: -v for c, v in zip(net_all.index, net_all["signed"]) if v < 0}
opening_total = sum(opening.values())
day0 = fills_in["d"].min()
px_day0 = q[q["d"] == day0].set_index("ts_code")["close"]
print(f"  首日 {day0.date()}，按当日收盘价计值：")
opening_val = {}
for c, v in sorted(opening.items(), key=lambda x: -x[1]):
    nm = fills_in.loc[fills_in["ts_code"] == c, "name"].iloc[-1]
    p0 = px_day0.get(c, np.nan)
    if not np.isfinite(p0):
        print(f"    {c} {nm:<14s} {v:>9,.0f}  ×  无首日行情 → **排除在模型外**")
        continue
    opening_val[c] = float(v * p0)
    print(f"    {c} {nm:<14s} {v:>9,.0f} × {p0:>8.3f} = {opening_val[c]:>12,.2f}")
mv0 = sum(opening_val.values())
cash0 = INIT_CAPITAL - mv0
print(f"  期初持仓首日市值合计 {mv0:,.2f}")
print(f"  → 剩余现金 = 假设初始资金 {INIT_CAPITAL:,.0f} − {mv0:,.2f} = {cash0:,.2f}")
print(f"  ✅ 自洽性：期初持仓市值占假设初始资金的 {mv0/INIT_CAPITAL*100:.1f}%，"
      f"两者量级吻合（这也反过来支持 11 万这个假设）")
print("  口径：期初持仓按【首日收盘价】作为成本纳入模型（其盈亏从首日起算），")
print("        无首日行情的标的（春风转债，实为 2026-06 配债所得）整体排除。")


# ============================================================ 4 逐日权益 + FIFO
hr("四、构建日级权益曲线")
# 采用「代数式重建」而非运行态匹配：
#   现金(t) = 期初现金 + Σ现金流水（买：−(成交额+费用)；卖：+(成交额−费用)）
#   持仓(t) = 期初持仓 + Σ净买入量
#   权益(t) = 现金(t) + Σ持仓(t)×当日收盘
# 这样不存在「同日先卖后买导致持仓为负」的路径依赖，也不会产生假现金负值。
EXCLUDED_CODES = {"113704.SH"}          # 春风转债：配债所得、无行情，整体排除
fl = fills_in[~fills_in["ts_code"].isin(EXCLUDED_CODES)].copy()
fl["cashflow"] = np.where(fl["side"] == "buy",
                          -(fl["amount"] + fl["fee"]),
                          fl["amount"] - fl["fee"])
buy_gross = float(fl.loc[fl["side"] == "buy", "amount"].sum())
sell_gross = float(fl.loc[fl["side"] == "sell", "amount"].sum())
fee_total = float(fl["fee"].sum())
excluded_proceeds = float(fills_in.loc[fills_in["ts_code"].isin(EXCLUDED_CODES)
                                       & (fills_in["side"] == "sell"), "amount"].sum())

# 每日现金流与每日净买入量
cf_day = fl.groupby("d")["cashflow"].sum()
qty_day = fl.pivot_table(index="d", columns="ts_code", values="volume", aggfunc="sum")
signed = fl.assign(sq=np.where(fl["side"] == "buy", fl["volume"], -fl["volume"])) \
           .pivot_table(index="d", columns="ts_code", values="sq", aggfunc="sum")
signed = signed.reindex(columns=px.columns, fill_value=0.0).fillna(0.0) \
               .reindex(cal, fill_value=0.0).cumsum()
pos_df = signed.copy()
for c, v in opening.items():
    if c in opening_val:
        pos_df[c] = pos_df.get(c, 0.0) + v

cash_series = cash0 + cf_day.reindex(cal, fill_value=0.0).cumsum()
# ---- 估值价格面板 ----------------------------------------------------------
# 有些标的的行情在分析窗口内**根本不存在**（例如凤凰航运 000520 在 2026-06~07 交易了
# 47.7 万元，但行情从 2026-08-03 才开始）。若不处理，持仓市值会按 0 计，
# 权益会出现荒谬的负值。这里采用「行情优先 → 缺失日用该标的当日成交均价 → ffill」。
px_fill = fl.pivot_table(index="d", columns="ts_code", values="price", aggfunc="mean")
px_val = px.copy()
for c in px_fill.columns:
    s = px_fill[c].dropna()
    if c in px_val.columns:
        px_val[c] = px_val[c].where(px_val[c].notna(), s)
    else:
        px_val[c] = s
px_val = px_val.ffill().reindex(cal)
mv_series = (pos_df * px_val).sum(axis=1, min_count=1).fillna(0.0)
na_cells = int((pos_df.abs() > 1e-9).mul(px_val.isna()).sum().sum())
print(f"  无行情标的数 {len(no_quote)}（春风转债已整体排除）；"
      f"用成交价兜底的标的 {int((px_val.iloc[-1].notna() & px.iloc[-1].isna()).sum())} 只")
print(f"  持仓>0 但仍无任何价格的 (日×标的) 单元格：{na_cells}（已按 0 计值）")
print(f"  成交额：买入 {buy_gross:,.0f}｜卖出 {sell_gross:,.0f}｜费用 {fee_total:,.0f}")
print(f"  排除标的 {sorted(EXCLUDED_CODES)} 的卖出回款 {excluded_proceeds:,.2f} 元（无持仓、无行情）")
print(f"  全程现金流合计 {float(fl['cashflow'].sum()):+,.2f} 元")

# ---------- 独立的 FIFO 已实现盈亏（用于月度胜率与归因，不影响权益）----------
realized_total = 0.0
sell_records = []
lots = defaultdict(list)
for c, v in opening.items():
    if c in opening_val:
        lots[c].append([v, float(px_day0[c])])
# 同日先买后卖（理由见上：load_fills 丢失日内时间）
fl_ord = fl.assign(_o=(fl["side"] == "sell").astype(int)).sort_values(["d", "_o"], kind="stable")
total_sell_vol = 0.0
total_matched = 0.0
for _, f in fl_ord.iterrows():
    c, side = f["ts_code"], f["side"]
    if side == "buy":
        lots[c].append([float(f["volume"]), (float(f["amount"]) + float(f["fee"])) / float(f["volume"])])
    else:
        vol, amt, fee = float(f["volume"]), float(f["amount"]), float(f["fee"])
        total_sell_vol += vol
        need, mc, mq = vol, 0.0, 0.0
        while need > 1e-9 and lots[c]:
            lot = lots[c][0]
            take = min(need, lot[0])
            mc += take * lot[1]
            mq += take
            lot[0] -= take
            need -= take
            if lot[0] <= 1e-9:
                lots[c].pop(0)
        total_matched += mq
        if mq > 1e-9:
            alloc = (amt - fee) * mq / vol
            pnl = alloc - mc
            realized_total += pnl
            sell_records.append({"d": f["d"], "ts_code": c, "name": f["name"],
                                 "pnl": pnl, "cost": mc, "ret": pnl / mc if mc else np.nan})

equity_rows = []
for day in cal:
    equity_rows.append({"date": day,
                        "cash": float(cash_series.loc[day]),
                        "mv": float(mv_series.loc[day]),
                        "equity": float(cash_series.loc[day] + mv_series.loc[day]),
                        "realized": realized_total})
eq = pd.DataFrame(equity_rows).set_index("date")
print(f"  期末权益 {eq['equity'].iloc[-1]:,.2f} 元"
      f"（现金 {eq['cash'].iloc[-1]:,.2f} + 持仓市值 {eq['mv'].iloc[-1]:,.2f}）")
print(f"  FIFO 已实现盈亏合计 {realized_total:,.2f} 元")
unmatched_qty = total_sell_vol - total_matched
print(f"  卖出中未能配对到买入批次的数量 {unmatched_qty:,.0f}（应为 0）")
print(f"  现金项最低 {eq['cash'].min():,.2f}｜最高 {eq['cash'].max():,.2f}")
print("  ⚠️ 现金项可能为负：交割单不含【天天宝申购/赎回】与【银证转账】，")
print("     外部资金注入未被记录会让现金项变负，但「现金+持仓市值」的总权益仍成立")
print("     （被买入的资产已计入持仓市值，两者相互抵消）。")

# 恒等式自检：权益 = 初始资金 + FIFO已实现 + 未实现浮动
remaining_cost = sum(lot[0] * lot[1] for c in lots for lot in lots[c])
floating = eq["mv"].iloc[-1] - remaining_cost
lhs = float(eq["equity"].iloc[-1])
rhs = INIT_CAPITAL + realized_total + floating
print(f"  期末持仓成本 {remaining_cost:,.2f}｜持仓市值 {eq['mv'].iloc[-1]:,.2f}"
      f"｜浮动盈亏 {floating:+,.2f}")
print(f"  恒等式自检：初始 {INIT_CAPITAL:,.0f} + 已实现 {realized_total:,.2f} "
      f"+ 浮动 {floating:,.2f} = {rhs:,.2f} vs 权益 {lhs:,.2f}（差 {lhs-rhs:+,.2f}）")
assert abs(lhs - rhs) < 1.0, "权益恒等式不平，检查现金/持仓口径"
print("  ✅ 恒等式通过")


# ============================================================ 5 月度
hr("五、月度收益")
me = eq["equity"].resample("ME").last()
me.index = me.index.to_period("M")
prev = me.shift(1)
mret = (me / prev - 1.0)
mret.iloc[0] = me.iloc[0] / INIT_CAPITAL - 1.0     # 首月：从假设初始资金算起

fills_in["ym"] = fills_in["d"].dt.to_period("M")
sr = pd.DataFrame(sell_records)
sr["ym"] = pd.to_datetime(sr["d"]).dt.to_period("M")

rows = []
for ym in me.index:
    fl = fills_in[fills_in["ym"] == ym]
    s = sr[sr["ym"] == ym] if len(sr) else sr
    rows.append({
        "month": str(ym),
        "return_pct": round(float(mret.loc[ym]) * 100, 4),
        "equity_end": round(float(me.loc[ym]), 2),
        "n_trades": int(len(fl)),
        "n_buy": int((fl["side"] == "buy").sum()),
        "n_sell": int((fl["side"] == "sell").sum()),
        "turnover": round(float(fl["amount"].sum()), 2),
        "n_closed": int(len(s)),
        "win_rate_pct": round(float((s["pnl"] > 0).mean()) * 100, 2) if len(s) else None,
        "realized_pnl": round(float(s["pnl"].sum()), 2) if len(s) else 0.0,
    })
mt = pd.DataFrame(rows)

print(f"  月度数 {len(mt)}")
print(f"  月度胜率 {(mt['return_pct']>0).mean()*100:.1f}%"
      f"（{int((mt['return_pct']>0).sum())} 涨 / {int((mt['return_pct']<0).sum())} 跌）")
print("\n" + "| 年月 | 收益率 | 期末权益 | 交易笔数 | 买/卖 | 成交额 | 了结笔数 | 当月胜率 | 当月已实现 |")
print("|---|---:|---:|---:|---|---:|---:|---:|---:|")
for _, r in mt.iterrows():
    print(f"| {r['month']} | {r['return_pct']:+.2f}% | {r['equity_end']:,.0f} | {r['n_trades']} | "
          f"{r['n_buy']}/{r['n_sell']} | {r['turnover']:,.0f} | {r['n_closed']} | "
          f"{r['win_rate_pct'] if r['win_rate_pct'] is not None else '—'} | {r['realized_pnl']:+,.0f} |")


# ============================================================ 6 回撤
hr("六、最大回撤")
cummax = eq["equity"].cummax()
dd = eq["equity"] / cummax - 1.0
trough = dd.idxmin()
peak = eq["equity"][:trough].idxmax()
maxdd = float(dd.min())
print(f"  全期最大回撤 {maxdd*100:.2f}%")
print(f"  峰值日 {peak.date()} 权益 {eq['equity'].loc[peak]:,.2f}")
print(f"  谷值日 {trough.date()} 权益 {eq['equity'].loc[trough]:,.2f}")
print(f"  回撤区间 {(trough - peak).days} 个自然日")
after = eq["equity"][eq.index > trough]
hit = after[after >= eq["equity"].loc[peak]]
recovered = hit.index[0] if len(hit) else None
print(f"  修复日 {recovered.date() if recovered is not None else '未修复（至末日仍未回到峰值）'}")
print("\n  单日权益波动最大 5 天：")
dchg = eq["equity"].pct_change()
for d, v in dchg.abs().nlargest(5).items():
    print(f"    {d.date()}  {dchg.loc[d]*100:+.2f}%")


# ============================================================ 7 与大盘相关性
hr("七、与上证指数的相关性")
con.execute(f"attach '{IDXDB.as_posix()}' as t (READ_ONLY)")
idx = con.execute(
    "select trade_date, close from t.index_daily where ts_code=? order by trade_date",
    [BENCH_CODE]).df()
idx["trade_date"] = pd.to_datetime(idx["trade_date"])
idx = idx.set_index("trade_date")["close"].sort_index()
idx_me = idx.resample("ME").last()
idx_me.index = idx_me.index.to_period("M")
idx_ret = idx_me / idx_me.shift(1) - 1.0
print(f"  {BENCH_CODE} 月份数 {len(idx_me)}，覆盖 {idx_me.index.min()} ~ {idx_me.index.max()}")

common = mret.index.intersection(idx_ret.index)
a_ = mret.loc[common].astype(float)
b_ = idx_ret.loc[common].astype(float)
valid = a_.notna() & b_.notna()
a_, b_ = a_[valid], b_[valid]
sp = float(a_.corr(b_, method="spearman"))
pe = float(a_.corr(b_, method="pearson"))
print(f"  可比月份 {len(a_)}")
print(f"  Spearman 相关系数 {sp:+.4f}")
print(f"  Pearson  相关系数 {pe:+.4f}")
print(f"  账户月均 {a_.mean()*100:+.2f}%｜大盘月均 {b_.mean()*100:+.2f}%")
print(f"  账户月度波动 {a_.std()*100:.2f}%｜大盘 {b_.std()*100:.2f}%")

print("\n  逐月对照：")
print("| 年月 | 账户 | 上证 | 差 |")
print("|---|---:|---:|---:|")
bench_rows = []
for ym in common:
    av, bv = mret.loc[ym], idx_ret.loc[ym]
    if pd.isna(av) or pd.isna(bv):
        continue
    bench_rows.append({"month": str(ym), "account_pct": round(float(av)*100, 4),
                       "index_pct": round(float(bv)*100, 4),
                       "excess_pct": round((float(av)-float(bv))*100, 4)})
    print(f"| {ym} | {av*100:+.2f}% | {bv*100:+.2f}% | {(av-bv)*100:+.2f}% |")


# ============================================================ 8 最好/最差月份
hr("八、最好的 3 个月与最差的 3 个月")


def month_detail(ym_rows):
    out = []
    for _, r in ym_rows.iterrows():
        ym = pd.Period(r["month"], "M")
        fl = fills_in[fills_in["ym"] == ym]
        s = sr[sr["ym"] == ym]
        g = fl.groupby(["ts_code", "name"]).agg(turnover=("amount", "sum"),
                                               n=("amount", "size")).reset_index()
        g = g.sort_values("turnover", ascending=False).head(3)
        p = s.groupby("ts_code")["pnl"].sum().sort_values()
        out.append({
            "month": r["month"],
            "return_pct": r["return_pct"],
            "equity_end": r["equity_end"],
            "n_trades": r["n_trades"],
            "turnover": r["turnover"],
            "win_rate_pct": r["win_rate_pct"],
            "realized_pnl": r["realized_pnl"],
            "top_turnover": [{"ts_code": x.ts_code, "name": x.name,
                              "turnover": round(float(x.turnover), 2), "n": int(x.n)}
                             for x in g.itertuples(index=False)],
            "realized_by_stock": [{"ts_code": c, "pnl": round(float(v), 2)}
                                  for c, v in p.items()],
        })
    return out


mt_sorted = mt.sort_values("return_pct", ascending=False)
best = month_detail(mt_sorted.head(3))
worst = month_detail(mt_sorted.tail(3).sort_values("return_pct"))

for lab, arr in [("最好 3 个月", best), ("最差 3 个月", worst)]:
    print(f"\n  【{lab}】")
    for r in arr:
        print(f"  {r['month']}  收益 {r['return_pct']:+.2f}%  交易 {r['n_trades']} 笔  "
              f"当月胜率 {r['win_rate_pct']}%  已实现 {r['realized_pnl']:+,.0f}")
        print(f"    成交额 TOP3: " + "；".join(
            f"{t['name']}({t['ts_code']}) {t['turnover']:,.0f}元/{t['n']}笔" for t in r["top_turnover"]))
        if r["realized_by_stock"]:
            top = r["realized_by_stock"]
            print(f"    当月已实现: 最赚 " + "、".join(
                f"{t['ts_code']} {t['pnl']:+,.0f}" for t in top[::-1][:3]) +
                "｜最亏 " + "、".join(f"{t['ts_code']} {t['pnl']:+,.0f}" for t in top[:3]))


# ============================================================ 9 汇总输出
hr("九、全期汇总")
first_eq = eq["equity"].iloc[0]
total_ret = eq["equity"].iloc[-1] / INIT_CAPITAL - 1.0
years = (eq.index[-1] - eq.index[0]).days / 365.25
ann = (1 + total_ret) ** (1 / years) - 1 if years > 0 else np.nan
idx_ret_valid = idx_ret.loc[idx_ret.index.isin(common)].dropna()
idx_cum = float((1 + idx_ret_valid).prod() - 1)
print(f"  初始资金（假设） {INIT_CAPITAL:,.0f}")
print(f"  期末权益 {eq['equity'].iloc[-1]:,.2f}")
print(f"  全期收益 {total_ret*100:+.2f}%（{years:.2f} 年，年化 {ann*100:+.2f}%）")
print(f"  同期上证累计 {idx_cum*100:+.2f}%（{len(idx_ret_valid)} 个月口径）")
print(f"  最大回撤 {maxdd*100:.2f}%（{peak.date()} → {trough.date()}）")
print(f"  月度胜率 {(mt['return_pct']>0).mean()*100:.1f}%（{len(mt)} 个月）")
print(f"  与上证 Spearman {sp:+.4f}")

payload = {
    "generated_at": pd.Timestamp.now().isoformat(timespec="seconds"),
    "assumptions": {
        "initial_capital": INIT_CAPITAL,
        "initial_capital_note": "假设值；用户账户量级约 11 万，真实初始资金未知",
        "price_basis": "quotes.parquet 原始收盘价（未复权），持仓按当日最后可得收盘价计价",
        "fills_used": int(len(fills_in)),
        "cross_file_duplicates_removed": int(n_cross_dup),
        "window": [str(eq.index[0].date()), str(eq.index[-1].date())],
        "dropped_late_fills": int(len(dropped)),
        "opening_positions_excluded": {c: int(v) for c, v in opening.items()},
        "opening_positions_total_units": int(opening_total),
        "opening_positions_day0_value": round(mv0, 2),
        "opening_positions_valuation": "期初持仓按首日收盘价计入模型成本，其盈亏从首日起算",
        "excluded_code_sale_proceeds": round(excluded_proceeds, 2),
    },
    "limitations": [
        "初始资金 110000 元为假设值，不是交割单中的事实；收益率水平依赖该假设",
        "只覆盖一个证券账户，不含理财（天天宝）、其他账户、场外资产与打新收益",
        "持仓按原始收盘价计价，未做复权；现金分红、送转会造成价格与权益的时点错位",
        "期初已有持仓（无买入记录）被排除在模型外，其卖出回款不计入现金",
        "成交晚于行情末日的记录无法计价，已剔除",
        "月度收益首月（2024-10）为不完整月，从假设初始资金起算",
    ],
    "totals": {
        "equity_end": round(float(eq["equity"].iloc[-1]), 2),
        "total_return_pct": round(float(total_ret) * 100, 4),
        "annualized_pct": round(float(ann) * 100, 4),
        "years": round(float(years), 3),
        "realized_pnl_fifo": round(float(realized_total), 2),
        "fee_total": round(float(fee_total), 2),
        "buy_gross": round(float(buy_gross), 2),
        "sell_gross": round(float(sell_gross), 2),
        "n_months": int(len(mt)),
        "monthly_win_rate_pct": round(float((mt["return_pct"] > 0).mean()) * 100, 2),
    },
    "max_drawdown": {
        "pct": round(maxdd * 100, 4),
        "peak_date": str(peak.date()),
        "peak_equity": round(float(eq["equity"].loc[peak]), 2),
        "trough_date": str(trough.date()),
        "trough_equity": round(float(eq["equity"].loc[trough]), 2),
        "calendar_days": int((trough - peak).days),
        "recovered_date": str(recovered.date()) if recovered is not None else None,
    },
    "benchmark": {
        "code": BENCH_CODE,
        "spearman": round(sp, 4),
        "pearson": round(pe, 4),
        "n_months": int(len(a_)),
        "account_mean_pct": round(float(a_.mean()) * 100, 4),
        "index_mean_pct": round(float(b_.mean()) * 100, 4),
        "account_std_pct": round(float(a_.std()) * 100, 4),
        "index_std_pct": round(float(b_.std()) * 100, 4),
        "monthly": bench_rows,
    },
    "monthly": mt.to_dict(orient="records"),
    "best_months": best,
    "worst_months": worst,
    "equity_curve": [{"date": str(d.date()), "equity": round(float(v), 2)}
                     for d, v in eq["equity"].items()],
}

OUT_JSON.parent.mkdir(parents=True, exist_ok=True)
OUT_JSON.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
print(f"\n  已写出 {OUT_JSON}（{OUT_JSON.stat().st_size:,} 字节）")




