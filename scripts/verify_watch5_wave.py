"""「第 5 个交易日收盘仍为负就清仓」——在用户真实交割单上的反事实检验。

背景
====
用户真实交易的既有结论是「波的最终收益主要由进场时机决定（进场涨幅 R²=0.553），
持有天数 R² 仅 0.010」。据此想验证一条离场纪律：

    买入后第 5 个交易日收盘，若仍低于「波内买入加权均价」，当日收盘全部清仓。

回测引擎自带 −3%/−5%/−8% 止损、两年只触发 3 次，无法验证这条规则，
所以在**用户自己的真实交易**上做反事实。

口径（六条，务必连结论一起读）
==============================
1. **波口径**：复用 ``behavior.ingest.load_fills`` + ``behavior.wave.build_waves``，
   只取 ``not w.open`` 的已清仓波。不自己重写聚合逻辑。

2. **观察点有两个候选口径，差一根 bar，结论会变，所以两套都跑**：
   - **A 任务书字面口径**（主）：``buy_date`` 当天算第 1 根，第 5 根 bar → 索引偏移 4。
   - **B 参考面板口径**：``buy_date`` 之后第 5 个交易日（不含当日）→ 索引偏移 5。
     实测 ``data/wave_entry_panel.parquet`` 的 ``ret5`` 用的正是这一套（41/41 完全命中）。
   两者在含停牌/长假的标的上可能差 1~10 个自然日。**报告以 A 为主、B 并列**。

3. **覆盖闸门**：``buy_date`` 当天在该股行情序列里必须存在。``quotes.parquet`` 里
   223 只标的只有 66 根 bar（2026-08-03 起），2024~2026 早期的波无法评估，直接跳过
   并计数，不用近端 bar 冒充。

4. **反事实只作用于观察点当日的持仓切片**，不是「整波的买入总额」：
   真实交易里有 35 个波在第 5 根 bar 之前就已全部清仓，还有 35 个波在第 5 根 bar
   之后仍在加仓。对这 70 个波，"当日全部卖出"没有可卖的整仓——能卖的只有观察点时
   实际持有的那 ``qty_at5`` 股。所以：

       qty_at5     = Σ买入股数(≤观察日) − Σ卖出股数(<观察日)
       实际退出净额 = 观察日及之后按时间顺序卖出 qty_at5 股的实际成交额（扣卖出费）
       反事实退出净额 = close(观察日) × qty_at5 × (1 − 0.0013)
       Δ = 反事实 − 实际 ；已清仓波的最终盈亏 = w.pnl + Δ（未触发的波 Δ=0）

   买入费用在 Δ 里自动抵消（成本基准相同），无需分摊。

5. **前视偏差防护**：任务口径写「买入加权均价 = w.buy_amount / 波内买入股数合计」，
   这是**整波**（含观察日之后的加仓）均价。对 35 个在观察日之后仍在加仓的波，
   用含未来买入价的均价去判断观察日是否亏，属前视。故：
     - **主口径**：只用观察日及之前已发生的买入（无前视）
     - **敏感性**：整波均价（任务书原口径，含前视）
   两套都出，结论一致才敢下判断。

6. **成本**：反事实卖出成本 **0.13%**（13bp，覆盖佣金+过户费+印花税+滑点的保守合并值），
   只作用于反事实卖出额；实际退出用交割单里的真实费用。另给 0/5/13/26bp 敏感性。

写作用域：本脚本 + outputs/watch5_wave.json。不改 src/，不碰 data/lihu_quant.duckdb。
行情只从 data/_team/quotes.parquet 经 duckdb **内存连接**读取。
"""

from __future__ import annotations

import collections
import json
import sys
from datetime import date
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import duckdb  # noqa: E402

from lihu_quantify.behavior.ingest import load_fills  # noqa: E402
from lihu_quantify.behavior.wave import build_waves  # noqa: E402

# ------------------------------------------------------------------ 配置
QUOTES_PARQUET = ROOT / "data" / "_team" / "quotes.parquet"
PANEL_PARQUET = ROOT / "data" / "wave_entry_panel.parquet"
CSV_PATHS = [
    Path(r"E:/Dsh_WorkSapce/Dify_Agents/投研反思/Table_1010.csv"),
    Path(r"E:/Dsh_WorkSapce/Dify_Agents/投研反思/Table_1254.csv"),
]
OUT_JSON = ROOT / "outputs" / "watch5_wave.json"

WATCH_BAR = 5
SELL_COST = 0.0013
IS_END = date(2025, 12, 31)

# 两套观察点口径：label -> 该股行情序列里相对 buy_date 的索引偏移
VARIANTS = [
    ("A_spec_5th_inclusive", 4),   # 任务书字面：buy_date 算第 1 根 → 第 5 根
    ("B_panel_T_plus_5", 5),       # 参考面板实测：buy_date 之后第 5 个交易日
]

BUCKETS = [
    ("<-5%", None, -0.05),
    ("-5%~-2%", -0.05, -0.02),
    ("-2%~0%", -0.02, 0.0),
    ("0%~+2%", 0.0, 0.02),
    (">+2%", 0.02, None),
]


def log(msg: str = "") -> None:
    print(msg, flush=True)


def hdr(title: str) -> None:
    log("")
    log("=" * 80)
    log(title)
    log("=" * 80)


def pct(x, nd: int = 2) -> str:
    return "n/a" if x is None else f"{x * 100:.{nd}f}%"


def money(x) -> str:
    return "n/a" if x is None else f"{x:,.2f}"


def _mean(xs):
    return sum(xs) / len(xs) if xs else 0.0


def _median(xs):
    if not xs:
        return 0.0
    s = sorted(xs)
    k = len(s) // 2
    return s[k] if len(s) % 2 else (s[k - 1] + s[k]) / 2


# ------------------------------------------------------------------ 行情
def load_quotes() -> dict:
    if not QUOTES_PARQUET.exists():
        raise SystemExit(f"[FATAL] 行情文件不存在：{QUOTES_PARQUET}")
    con = duckdb.connect()          # 纯内存，不 attach 任何持久库
    try:
        rows = con.execute(
            f"select ts_code, trade_date, close from read_parquet('{QUOTES_PARQUET.as_posix()}') "
            f"where close is not null and close > 0"
        ).fetchall()
    finally:
        con.close()
    book: dict[str, dict] = {}
    for code, d, c in rows:
        book.setdefault(code, {"close": {}})["close"][str(d)] = float(c)
    for e in book.values():
        e["dates"] = sorted(e["close"])
    return book


def load_panel_reference() -> dict:
    if not PANEL_PARQUET.exists():
        return {}
    con = duckdb.connect()
    try:
        rows = con.execute(
            f"select ts_code, buy_date, ret5, buy_px from "
            f"read_parquet('{PANEL_PARQUET.as_posix()}')"
        ).fetchall()
    finally:
        con.close()
    return {(c, str(b)[:10].replace("-", "")): (ret5, buy_px) for c, b, ret5, buy_px in rows}


# ------------------------------------------------------------------ 单波评估
def evaluate_wave(w, quotes: dict, offset: int, sell_cost: float) -> dict:
    """观察点 = 该股行情序列中 buy_date 起第 offset 号下标（buy_date 为 0 号）。"""
    bd = w.buy_date.strftime("%Y%m%d")
    q = quotes.get(w.ts_code)
    if not q or bd not in q["close"]:
        return {"status": "no_quote", "ts_code": w.ts_code, "buy_date": bd}
    dates = q["dates"]
    lo = _bisect_left(dates, bd)
    if lo + offset >= len(dates):
        return {"status": "short", "ts_code": w.ts_code, "buy_date": bd,
                "bars": len(dates) - lo}
    d5 = dates[lo + offset]
    close5 = q["close"][d5]

    buys = [f for f in w.fills if f["side"] == "buy"]
    sells = [f for f in w.fills if f["side"] == "sell"]

    qty_buy_le5 = sum(f["volume"] for f in buys if f["trade_date"].strftime("%Y%m%d") <= d5)
    amt_buy_le5 = sum(float(f["amount"]) for f in buys if f["trade_date"].strftime("%Y%m%d") <= d5)
    qty_sell_lt5 = sum(f["volume"] for f in sells if f["trade_date"].strftime("%Y%m%d") < d5)
    qty_at5 = qty_buy_le5 - qty_sell_lt5
    if qty_at5 <= 0 or close5 <= 0:
        return {"status": "flat_at_bar", "ts_code": w.ts_code, "buy_date": bd, "d5": d5}

    px_le5 = (amt_buy_le5 / qty_buy_le5) if qty_buy_le5 > 0 else 0.0
    qty_buy_all = sum(f["volume"] for f in buys)
    px_all = (w.buy_amount / qty_buy_all) if qty_buy_all > 0 else 0.0
    ret5 = (close5 / px_le5 - 1.0) if px_le5 > 0 else None
    ret5_lookahead = (close5 / px_all - 1.0) if px_all > 0 else None

    # 实际退出：观察日及之后按时间顺序卖出 qty_at5 股
    need = qty_at5
    gross = fee_sell = 0.0
    for f in sorted(sells, key=lambda x: x["trade_date"]):
        if f["trade_date"].strftime("%Y%m%d") < d5:
            continue
        take = min(need, f["volume"])
        if take <= 0:
            continue
        gross += float(f["price"]) * take
        fee_sell += float(f["fee"]) * take / max(1, f["volume"])
        need -= take
        if need <= 0:
            break
    if need > 0:
        return {"status": "incomplete_sell", "ts_code": w.ts_code, "buy_date": bd,
                "missing": need}

    actual_net = gross - fee_sell
    cf_gross = close5 * qty_at5
    delta = cf_gross * (1.0 - sell_cost) - actual_net

    return {
        "status": "ok",
        "ts_code": w.ts_code,
        "name": w.name,
        "buy_date": bd,
        "d5": d5,
        "close5": close5,
        "px_le5": px_le5,
        "px_all": px_all,
        "ret5": ret5,
        "ret5_lookahead": ret5_lookahead,
        "qty_at5": qty_at5,
        "qty_buy_all": qty_buy_all,
        "staged_after_bar": qty_buy_le5 < qty_buy_all,
        "actual_net": round(actual_net, 2),
        "cf_gross": round(cf_gross, 2),
        "delta": round(delta, 2),
        "wave_pnl": round(w.pnl, 2),
        "wave_cost": round(w.buy_amount, 2),
        "hit": bool(ret5 is not None and ret5 < 0),
        "hit_lookahead": bool(ret5_lookahead is not None and ret5_lookahead < 0),
        "is_period": w.buy_date <= IS_END,
        "hold_days": w.hold_days,
    }


def _bisect_left(seq: list[str], x: str) -> int:
    lo, hi = 0, len(seq)
    while lo < hi:
        mid = (lo + hi) // 2
        if seq[mid] < x:
            lo = mid + 1
        else:
            hi = mid
    return lo


# ------------------------------------------------------------------ 汇总
def summarize(rows: list[dict], hit_key: str = "hit") -> dict:
    n = len(rows)
    if n == 0:
        return {"n": 0}
    act = [r["wave_pnl"] for r in rows]
    cf = [r["wave_pnl"] + (r["delta"] if r[hit_key] else 0.0) for r in rows]
    costs = [r["wave_cost"] for r in rows]
    act_ret = [p / c for p, c in zip(act, costs) if c > 0]
    cf_ret = [p / c for p, c in zip(cf, costs) if c > 0]
    cost_total = sum(costs)
    return {
        "n": n,
        "n_hit": sum(1 for r in rows if r[hit_key]),
        "actual_pnl_total": round(sum(act), 2),
        "cf_pnl_total": round(sum(cf), 2),
        "delta_total": round(sum(cf) - sum(act), 2),
        "cost_total": round(cost_total, 2),
        "actual_win_rate": round(sum(1 for p in act if p > 0) / n, 4),
        "cf_win_rate": round(sum(1 for p in cf if p > 0) / n, 4),
        "actual_ret_mean": round(_mean(act_ret), 6),
        "cf_ret_mean": round(_mean(cf_ret), 6),
        "actual_ret_median": round(_median(act_ret), 6),
        "cf_ret_median": round(_median(cf_ret), 6),
        "actual_ret_moneyweighted": round(sum(act) / cost_total, 6) if cost_total > 0 else 0.0,
        "cf_ret_moneyweighted": round(sum(cf) / cost_total, 6) if cost_total > 0 else 0.0,
        "actual_worst": round(min(act), 2),
        "cf_worst": round(min(cf), 2),
        "actual_best": round(max(act), 2),
        "cf_best": round(max(cf), 2),
    }


def bucket_of(ret5: float) -> str:
    for lab, lo, hi in BUCKETS:
        if (lo is None or ret5 > lo) and (hi is None or ret5 <= hi):
            return lab
    return BUCKETS[-1][0]


# ------------------------------------------------------------------ 变体流程
def run_variant(label: str, offset: int, closed: list, quotes: dict,
                panel: dict) -> dict:
    hdr(f"[口径 {label}] 观察点偏移 = {offset}（buy_date 记为 0 号 bar）")
    status = collections.Counter()
    rows: list[dict] = []
    no_quote_list, short_list = [], []
    skipped_by_status: dict[str, list] = collections.defaultdict(list)
    for w in closed:
        r = evaluate_wave(w, quotes, offset, SELL_COST)
        status[r["status"]] += 1
        if r["status"] == "ok":
            rows.append(r)
        elif r["status"] == "no_quote":
            no_quote_list.append([w.ts_code, str(w.buy_date)])
            skipped_by_status["no_quote"].append(w)
        elif r["status"] == "short":
            short_list.append([w.ts_code, str(w.buy_date), r.get("bars")])
            skipped_by_status["short"].append(w)
        else:
            skipped_by_status[r["status"]].append(w)
    skipped = len(closed) - len(rows)
    log(f"  已清仓波 {len(closed)} 的状态：ok {status.get('ok',0)} / "
        f"flat_at_bar {status.get('flat_at_bar',0)} / no_quote {status.get('no_quote',0)} / "
        f"short {status.get('short',0)} / incomplete_sell {status.get('incomplete_sell',0)}")
    log(f"  → 进入结论 n = {len(rows)}，跳过 {skipped}"
        f"（未到期 {status.get('short',0)}，行情不覆盖 {status.get('no_quote',0)}，"
        f"观察点已清仓 {status.get('flat_at_bar',0)}）")
    if skipped:
        log("  被跳过样本的实际结局（⚠ 它们不进结论，但要交代方向）：")
        for st_key, ws in sorted(skipped_by_status.items(),
                                 key=lambda kv: -len(kv[1])):
            p = [w.pnl for w in ws]
            log(f"      {st_key:<14} n={len(ws):>3d}  实际合计 {money(sum(p)):>12}  "
                f"均值 {money(_mean(p)):>10}  胜率 "
                f"{pct(sum(1 for x in p if x > 0) / len(p) if p else 0):>7}")
    if no_quote_list:
        log(f"  无行情明细（ts_code, buy_date）：{no_quote_list}")
    if short_list:
        log(f"  未到期明细（ts_code, buy_date, 可用bar）：{short_list}")
    if not rows:
        return {"label": label, "offset": offset, "n": 0}

    # 与参考面板交叉校验
    matched = agree = 0
    diffs = []
    for r in rows:
        p = panel.get((r["ts_code"], r["buy_date"]))
        if not p or p[0] is None or r["ret5"] is None:
            continue
        matched += 1
        d = r["ret5"] - float(p[0])
        diffs.append(d)
        if abs(d) < 1e-6:
            agree += 1
    if matched:
        log(f"  与参考面板 ret5 校验：可比 {matched} 笔，完全一致 {agree} 笔"
            f"（{pct(agree/matched,1)}），中位差 {_median(diffs)*100:+.4f}%")

    main_sum = summarize(rows, "hit")
    is_rows = [r for r in rows if r["is_period"]]
    oos_rows = [r for r in rows if not r["is_period"]]

    log("")
    log(f"  n = {main_sum['n']}；触发（第5日收盘 < 买入均价）{main_sum['n_hit']} 笔 "
        f"（{pct(main_sum['n_hit']/main_sum['n'],1)}）；"
        f"投入成本合计 {money(main_sum['cost_total'])} 元")
    log("")
    log(f"  {'指标':<20} {'实际':>16} {'反事实':>16} {'差':>16}")
    log("  " + "-" * 72)
    rows_tbl = [
        ("总盈亏(元)", money(main_sum["actual_pnl_total"]), money(main_sum["cf_pnl_total"]),
         money(main_sum["delta_total"])),
        ("胜率", pct(main_sum["actual_win_rate"]), pct(main_sum["cf_win_rate"]),
         pct(main_sum["cf_win_rate"] - main_sum["actual_win_rate"])),
        ("均值收益(等权)", pct(main_sum["actual_ret_mean"]), pct(main_sum["cf_ret_mean"]),
         pct(main_sum["cf_ret_mean"] - main_sum["actual_ret_mean"])),
        ("中位收益(等权)", pct(main_sum["actual_ret_median"]), pct(main_sum["cf_ret_median"]),
         pct(main_sum["cf_ret_median"] - main_sum["actual_ret_median"])),
        ("收益(资金加权)", pct(main_sum["actual_ret_moneyweighted"]),
         pct(main_sum["cf_ret_moneyweighted"]),
         pct(main_sum["cf_ret_moneyweighted"] - main_sum["actual_ret_moneyweighted"])),
        ("最大单笔亏损(元)", money(main_sum["actual_worst"]), money(main_sum["cf_worst"]),
         money(main_sum["cf_worst"] - main_sum["actual_worst"])),
        ("最大单笔盈利(元)", money(main_sum["actual_best"]), money(main_sum["cf_best"]), "—"),
    ]
    for a, b, c, d in rows_tbl:
        log(f"  {a:<20} {b:>16} {c:>16} {d:>16}")

    log("")
    log("  IS / OOS（IS: buy_date <= 2025-12-31）")
    log(f"  {'期间':<6} {'n':>4} {'触发':>5} {'实际总盈亏':>13} {'反事实总盈亏':>14} "
        f"{'差':>12} {'实际胜率':>9} {'反事实胜率':>10}")
    log("  " + "-" * 88)
    is_sum = summarize(is_rows) if is_rows else {"n": 0}
    oos_sum = summarize(oos_rows) if oos_rows else {"n": 0}
    for lab, s in (("IS", is_sum), ("OOS", oos_sum)):
        if s["n"] == 0:
            log(f"  {lab:<6} {0:>4}  无法计算")
            continue
        log(f"  {lab:<6} {s['n']:>4d} {s['n_hit']:>5d} {money(s['actual_pnl_total']):>13} "
            f"{money(s['cf_pnl_total']):>14} {money(s['delta_total']):>12} "
            f"{pct(s['actual_win_rate']):>9} {pct(s['cf_win_rate']):>10}")

    log("")
    log("  按「第 5 日涨幅」分档")
    groups = collections.defaultdict(list)
    for r in rows:
        if r["ret5"] is not None:
            groups[bucket_of(r["ret5"])].append(r)
    log(f"  {'档位':<10} {'n':>4} {'触发':>5} {'实际均值收益':>12} {'反事实均值收益':>14} "
        f"{'差(收益)':>10} {'实际总盈亏':>12} {'反事实总盈亏':>12} {'差(元)':>12}")
    log("  " + "-" * 108)
    bucket_rows = []
    for lab, _, _ in BUCKETS:
        sub = groups.get(lab, [])
        if not sub:
            log(f"  {lab:<10} {'0':>4}  无法计算（该档无样本）")
            continue
        s = summarize(sub)
        bucket_rows.append({"bucket": lab, **s})
        log(f"  {lab:<10} {s['n']:>4d} {s['n_hit']:>5d} {pct(s['actual_ret_mean']):>12} "
            f"{pct(s['cf_ret_mean']):>14} {pct(s['cf_ret_mean']-s['actual_ret_mean']):>10} "
            f"{money(s['actual_pnl_total']):>12} {money(s['cf_pnl_total']):>12} "
            f"{money(s['delta_total']):>12}")

    log("")
    log("  前视敏感性：整波均价口径（任务书字面，含观察日之后的加仓）")
    s_look = summarize(rows, "hit_lookahead")
    staged = sum(1 for r in rows if r["staged_after_bar"])
    log(f"      触发 {s_look['n_hit']} 笔（无前视口径为 {main_sum['n_hit']} 笔）；"
        f"实际 {money(s_look['actual_pnl_total'])} → 反事实 {money(s_look['cf_pnl_total'])}"
        f"（差 {money(s_look['delta_total'])}）；胜率 {pct(s_look['actual_win_rate'])} → "
        f"{pct(s_look['cf_win_rate'])}")
    log(f"      观察日之后仍在加仓的波 {staged}/{len(rows)} 笔")

    log("")
    log("  成本敏感性（反事实卖出成本）")
    cost_rows = []
    act_total = sum(r["wave_pnl"] for r in rows)
    for c in (0.0, 0.0005, SELL_COST, 0.0026):
        cf_total = 0.0
        wins = 0
        for r in rows:
            d = (r["cf_gross"] * (1 - c) - r["actual_net"]) if r["hit"] else 0.0
            v = r["wave_pnl"] + d
            cf_total += v
            if v > 0:
                wins += 1
        cost_rows.append({"sell_cost": c, "cf_pnl_total": round(cf_total, 2),
                          "delta_vs_actual": round(cf_total - act_total, 2),
                          "cf_win_rate": round(wins / len(rows), 4)})
        log(f"      {pct(c,2):<8} 反事实总盈亏 {money(cf_total):>14}  "
            f"vs 实际 {money(cf_total-act_total):>12}  胜率 {pct(wins/len(rows)):>8}")

    # ---- 诊断 1：等权均值与资金加权的背离
    log("")
    log("  诊断：为什么「均值收益(等权)」可能改善而「总盈亏」恶化")
    ordered = sorted(rows, key=lambda r: r["wave_cost"])
    k = max(1, len(ordered) // 3)
    terc = [("小仓位(成本下1/3)", ordered[:k]),
            ("中仓位", ordered[k:2 * k]),
            ("大仓位(成本上1/3)", ordered[2 * k:])]
    log(f"      {'成本分档':<18} {'n':>4} {'成本合计':>14} {'实际盈亏':>12} {'Δ':>12} "
        f"{'Δ/成本':>9}")
    log("      " + "-" * 74)
    cost_tercile = []
    for lab, sub in terc:
        if not sub:
            continue
        csum = sum(r["wave_cost"] for r in sub)
        ap = sum(r["wave_pnl"] for r in sub)
        dd = sum(r["delta"] for r in sub if r["hit"])
        cost_tercile.append({"tercile": lab, "n": len(sub), "cost": round(csum, 2),
                             "actual_pnl": round(ap, 2), "delta": round(dd, 2)})
        log(f"      {lab:<18} {len(sub):>4d} {money(csum):>14} {money(ap):>12} "
            f"{money(dd):>12} {pct(dd/csum) if csum else 'n/a':>9}")
    log("      → 等权均值按单笔收益率平均、不看仓位大小；资金加权才反映账户真实损益。"
        "以资金加权为准。")

    # ---- 诊断 2：被触发波的实际结局拆解
    log("")
    log("  诊断：被触发（第5日收盘为负）的波，实际最后是赚还是亏？")
    trig = [r for r in rows if r["hit"]]
    ntrig = [r for r in rows if not r["hit"]]
    if trig:
        win_trig = [r for r in trig if r["wave_pnl"] > 0]
        log(f"      触发 {len(trig)} 笔：实际合计 {money(sum(r['wave_pnl'] for r in trig))}，"
            f"Δ合计 {money(sum(r['delta'] for r in trig))}")
        log(f"        其中实际最终赚钱 {len(win_trig)} 笔，实际合计盈利 "
            f"{money(sum(r['wave_pnl'] for r in win_trig))}，"
            f"被规则砍掉后 Δ合计 {money(sum(r['delta'] for r in win_trig))}")
    log(f"      未触发 {len(ntrig)} 笔：实际合计 {money(sum(r['wave_pnl'] for r in ntrig))}")
    worst = min(rows, key=lambda r: r["wave_pnl"])
    best = max(rows, key=lambda r: r["wave_pnl"])
    log(f"      本口径最大单笔亏损 {worst['ts_code']} {worst['buy_date']} "
        f"{money(worst['wave_pnl'])} 元，ret5="
        f"{(pct(worst['ret5']) if worst['ret5'] is not None else 'n/a')}，"
        f"{'已被规则触发' if worst['hit'] else '未被规则触发'}")
    log(f"      本口径最大单笔盈利 {best['ts_code']} {best['buy_date']} "
        f"{money(best['wave_pnl'])} 元，ret5="
        f"{(pct(best['ret5']) if best['ret5'] is not None else 'n/a')}，"
        f"{'已被规则触发' if best['hit'] else '未被规则触发'}")

    return {
        "label": label,
        "offset": offset,
        "n": len(rows),
        "skipped": skipped,
        "status_counts": dict(status),
        "skipped_list": {"no_quote": no_quote_list, "short": short_list},
        "skipped_by_status": {
            k: {"n": len(v), "actual_pnl": round(sum(w.pnl for w in v), 2)}
            for k, v in skipped_by_status.items()
        },
        "actual_pnl_of_skipped": round(
            sum(w.pnl for w in closed) - sum(r["wave_pnl"] for r in rows), 2),
        "overall": main_sum,
        "overall_lookahead_variant": s_look,
        "is": is_sum,
        "oos": oos_sum,
        "by_ret5_bucket": bucket_rows,
        "cost_sensitivity": cost_rows,
        "cost_tercile": cost_tercile,
        "trigger_breakdown": {
            "n_triggered": len(trig),
            "triggered_actual_pnl": round(sum(r["wave_pnl"] for r in trig), 2),
            "triggered_delta": round(sum(r["delta"] for r in trig), 2),
            "triggered_but_actually_won": len([r for r in trig if r["wave_pnl"] > 0]),
            "triggered_but_actually_won_pnl": round(
                sum(r["wave_pnl"] for r in trig if r["wave_pnl"] > 0), 2),
            "untriggered_n": len(ntrig),
            "untriggered_actual_pnl": round(sum(r["wave_pnl"] for r in ntrig), 2),
            "worst_hit": bool(worst["hit"]),
            "worst_pnl": worst["wave_pnl"],
            "best_hit": bool(best["hit"]),
        },
        "panel_match": {"matched": matched, "exact_agree": agree},
        "per_wave": sorted(rows, key=lambda r: (r["buy_date"], r["ts_code"])),
    }


# ------------------------------------------------------------------ 主流程
def main() -> None:
    hdr("0. 读交割单 → 聚波（复用 behavior.ingest / behavior.wave）")
    for p in CSV_PATHS:
        if not p.exists():
            raise SystemExit(f"[FATAL] 交割单不存在：{p}")
    fills = load_fills(CSV_PATHS)
    waves = build_waves(fills)
    closed = [w for w in waves if not w.open]
    act_all = sum(w.pnl for w in closed)
    log(f"  成交笔数 {len(fills)}；波 {len(waves)}（已清仓 {len(closed)}，"
        f"未平仓 {len(waves)-len(closed)}）")
    log(f"  已清仓波覆盖 {len({w.ts_code for w in closed})} 只标的，"
        f"buy_date {min(w.buy_date for w in closed)} ~ {max(w.buy_date for w in closed)}")
    log(f"  已清仓波实际合计盈亏 {money(act_all)}，胜率 "
        f"{pct(sum(1 for w in closed if w.pnl>0)/len(closed),1)}")
    is_closed = [w for w in closed if w.buy_date <= IS_END]
    log(f"  其中 IS（buy_date<={IS_END}）{len(is_closed)} 笔，OOS {len(closed)-len(is_closed)} 笔")

    hdr("1. 行情覆盖闸门")
    quotes = load_quotes()
    depth = collections.Counter(len(v["dates"]) for v in quotes.values())
    log(f"  quotes.parquet 标的数 {len(quotes)}")
    log(f"  行情深度（bar 数→标的数）前 5 档 {dict(sorted(depth.items())[:5])}；"
        f">=1000 bar 的标的 {sum(c for n,c in depth.items() if n>=1000)} 只，"
        f"<=100 bar 的 {sum(c for n,c in depth.items() if n<=100)} 只")
    log("  ⚠ 223 只标的只有 66 根 bar（2026-08-03 起）：2024~2026 早期的波无法评估，"
        "已在下方逐口径计数跳过。")
    panel = load_panel_reference()
    log(f"  参考面板 {PANEL_PARQUET.name}：" +
        (f"{len(panel)} 行" if panel else "不可用"))

    results = {}
    for label, off in VARIANTS:
        results[label] = run_variant(label, off, closed, quotes, panel)

    # ---------------- 口径对比
    hdr("2. 两套观察点口径对比（差一根 bar 的影响）")
    log(f"  {'口径':<24} {'n':>4} {'触发':>5} {'实际总盈亏':>14} {'反事实总盈亏':>15} "
        f"{'差':>13} {'实际胜率':>9} {'反事实胜率':>10}")
    log("  " + "-" * 104)
    for label, _ in VARIANTS:
        r = results[label]
        if r["n"] == 0:
            continue
        s = r["overall"]
        log(f"  {label:<24} {s['n']:>4d} {s['n_hit']:>5d} {money(s['actual_pnl_total']):>14} "
            f"{money(s['cf_pnl_total']):>15} {money(s['delta_total']):>13} "
            f"{pct(s['actual_win_rate']):>9} {pct(s['cf_win_rate']):>10}")
    log("")
    log("  说明：A = 任务书字面口径（buy_date 算第 1 根），B = 参考面板实测口径"
        "（buy_date 后第 5 个交易日）。")
    log("        data/wave_entry_panel.parquet 的 ret5 与 B 完全一致，"
        "说明此前那份「买入后 5 日涨幅」分析用的是 B。")

    # ---------------- 落盘
    hdr("3. 写 outputs/watch5_wave.json")
    out = {
        "meta": {
            "generated_at": date.today().isoformat(),
            "fills": len(fills),
            "waves_total": len(waves),
            "waves_closed": len(closed),
            "waves_open": len(waves) - len(closed),
            "closed_pnl_total": round(act_all, 2),
            "closed_win_rate": round(sum(1 for w in closed if w.pnl > 0) / len(closed), 4),
            "is_closed": len(is_closed),
            "oos_closed": len(closed) - len(is_closed),
            "watch_bar": WATCH_BAR,
            "sell_cost": SELL_COST,
            "is_end": IS_END.isoformat(),
            "primary_variant": VARIANTS[0][0],
            "rule": "观察日收盘价 / 买入加权均价 - 1 < 0 → 当日收盘清仓观察点持仓",
            "quote_source": "data/_team/quotes.parquet（duckdb 内存只读）",
            "caveat_no_quote": "quotes.parquet 中 223 只标的仅 66 根 bar（2026-08-03 起），早期波无法评估",
        },
        "variants": {k: {kk: vv for kk, vv in v.items() if kk != "per_wave"}
                     for k, v in results.items()},
        "per_wave": {k: v.get("per_wave", []) for k, v in results.items()},
    }
    OUT_JSON.parent.mkdir(parents=True, exist_ok=True)
    OUT_JSON.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    log(f"  已写入 {OUT_JSON}（{OUT_JSON.stat().st_size:,} 字节）")

    # ---------------- 结论
    hdr("4. 一句话结论")
    for label, _ in VARIANTS:
        r = results[label]
        if r["n"] == 0:
            log(f"  [{label}] 无法计算")
            continue
        s = r["overall"]
        d, n, hit = s["delta_total"], s["n"], s["n_hit"]
        if d < 0:
            head = "规则无效"
        elif d > 0:
            head = "规则有效"
        else:
            head = "规则未改变结果"
        sample = "样本够" if (n >= 60 and hit >= 20) else "样本偏小，不足以下结论"
        log(f"  [{label}] {head}：反事实总盈亏 {money(s['cf_pnl_total'])} vs 实际 "
            f"{money(s['actual_pnl_total'])}，Δ={money(d)} 元；"
            f"胜率 {pct(s['actual_win_rate'])}→{pct(s['cf_win_rate'])}；"
            f"（n={n}，触发 {hit}）· {sample}")


if __name__ == "__main__":
    main()
