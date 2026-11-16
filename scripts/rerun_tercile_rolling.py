#!/usr/bin/env python
"""市值三分位池「滚动重切」复核（2026-11-16）。

背景
----
前序 `docs/市值三分位回测_20261116.md` 判 failed，其未做的补救是「滚动重切」：
只用了单个锚定日 2024-11-13 的市值切一次池，需要确认结论是否只是那一天的池子构成造成的。

本脚本**不做策略回测**，只做池构成 + 横截面等权收益。

数据
----
DuckDB 副本 `data/_team/lihu_quant_copy.duckdb`，**read_only**（绝不写主库）。
表：daily_quotes / daily_basic(pe_ttm,pb,total_mv) / stock_basic / index_daily / adj_factors。

⚠ 数据可用性实测（脚本每次运行都会重算并打印，不写死）：
   - `daily_basic` 只有 2026-09-01 ~ 2026-11-10，共 202 只（= 策略池），
     **不是全市场**；8 个季度末锚定日里只有 2026-09-30 有 total_mv。
   - `daily_quotes` 全库只有 496 只（策略池并集），2024/2025 锚定日只有 264~267 只有行情。
   - → 任务要求的「全市场 8 期滚动三分位」在本地数据上**无法计算**（缺项逐条列在 audit 里）。

口径
----
1. 锚定日：季度末；若当日非交易日，取 ≤ 该日的最后一个交易日（脚本会打印实际生效日期）。
2. 剔除：688（科创）/ 300·301（创业）/ 北交所（8·4·92 开头/.BJ）/ ST（stock_basic.name 含 ST）。
3. 切池：按当期 total_mv 升序 qcut 三分位，labels 显式写 ["小盘","中盘","大盘"]（升序），
   并断言「大盘中位 > 中盘中位 > 小盘中位」（前序踩过 labels 反挂的坑）。
4. 收益：池内个股 `close` 区间收益 = close(下一锚定日)/close(本锚定日) − 1，**等权**。
   ⚠ `daily_quotes.close` 是**未复权**价：分红/送转会污染个股收益（送转会假摔），
      本脚本不做复权修正，结论里必须写明这一限制。
5. 无行情/无市值/无复权因子的个股剔除，每期打印剔除数量。

两层结果
--------
- `official`：完全按任务口径（当日 total_mv 切池）。2026-09-30 之外**无法计算**。
- `proxy`：**代理口径，非任务要求**。用 2026-09-30 的 total_mv 快照，按后复权价比例
  回推各期市值：mv_t = mv_0930 × (close_t × adj_t) / (close_0930 × adj_0930)。
  样本被压在「有 2026-09-30 市值 + 两期后复权价」的 ~139 只上，且这 139 只是**当前策略池成员**
  （幸存者/选择偏差），只能用于看「机制方向」，不能替代全市场结论。

用法
----
    .venv\\Scripts\\python.exe scripts\\rerun_tercile_rolling.py
    .venv\\Scripts\\python.exe scripts\\rerun_tercile_rolling.py --out outputs/tercile_rolling.json
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import duckdb  # noqa: E402
import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402

ROOT = Path(r"E:\LihuQuantify")
DB = ROOT / "data" / "_team" / "lihu_quant_copy.duckdb"
OUT = ROOT / "outputs" / "tercile_rolling.json"

QUARTERS = ["2024-12-31", "2025-03-31", "2025-06-30", "2025-09-30",
            "2025-12-31", "2026-03-31", "2026-06-30", "2026-09-30"]
POOLS = ["小盘", "中盘", "大盘"]
SNAP_DATE = "20260930"      # 唯一的 total_mv 快照日（代理口径的基准）
MIN_PER_POOL = 5            # 一个池少于这么多只就不出数


# ---------------------------------------------------------------- 基础
def q(con, sql: str, params=None) -> pd.DataFrame:
    return con.execute(sql, params or []).fetchdf()


def resolve_anchor(con, anchor: str) -> str:
    """季度末 → 实际生效交易日（≤ 锚定日的最后一个交易日）。"""
    row = con.execute(
        "SELECT max(strftime(CAST(trade_date AS DATE),'%Y%m%d')) FROM daily_quotes "
        "WHERE CAST(trade_date AS DATE) <= CAST(? AS DATE)", [anchor],
    ).fetchone()
    return row[0] if row and row[0] else None


def board_of(ts_code: str) -> str:
    c = ts_code.split(".")[0]
    if c.startswith("688") or c.startswith("689"):
        return "科创板"
    if c.startswith("300") or c.startswith("301"):
        return "创业板"
    if ts_code.endswith(".BJ") or c.startswith(("8", "4", "92")):
        return "北交所"
    return "主板"


def close_on(con, date_key: str) -> pd.DataFrame:
    return q(con, "SELECT ts_code, close FROM daily_quotes "
                  "WHERE strftime(CAST(trade_date AS DATE),'%Y%m%d') = ?", [date_key])


def adj_on(con, date_key: str) -> pd.DataFrame:
    return q(con, "SELECT ts_code, adj_factor FROM adj_factors "
                  "WHERE strftime(CAST(trade_date AS DATE),'%Y%m%d') = ?", [date_key])


def split_tercile(df: pd.DataFrame) -> pd.DataFrame:
    """按 total_mv 升序三分位；labels 显式升序，并断言大>中>小。"""
    d = df.sort_values("total_mv").reset_index(drop=True).copy()
    if len(d) < 3 * MIN_PER_POOL:
        d["pool"] = None
        return d
    # qcut 升序 → 小盘=最低三分之一
    d["pool"] = pd.qcut(d["total_mv"], 3, labels=POOLS)
    med = d.groupby("pool", observed=True)["total_mv"].median()
    if not (med.get("大盘", 0) > med.get("中盘", 0) > med.get("小盘", 0)):
        raise SystemExit(f"[断言] 三分位标签错位：{med.to_dict()}")
    return d


def eligible(con) -> pd.DataFrame:
    """可选股票池：全市场名单 + 板块/ST 标记（用于剔除）。"""
    df = q(con, "SELECT ts_code, name FROM stock_basic WHERE ts_code NOT LIKE '%.BJ%'")
    df["board"] = df["ts_code"].map(board_of)
    df["is_st"] = df["name"].fillna("").str.contains("ST")
    return df


def quarter_returns(con, mv_df: pd.DataFrame, anchor_key: str, next_key: str,
                    elig: pd.DataFrame) -> dict:
    """给定锚定日市值截面，切三分位 → 到 next_key 的等权收益。"""
    base = mv_df.merge(elig[["ts_code", "board", "is_st"]], on="ts_code", how="left")
    n_raw = len(base)
    base = base[(base["board"] == "主板") & (~base["is_st"].fillna(False))].copy()
    n_after_filter = len(base)

    px0 = close_on(con, anchor_key).rename(columns={"close": "c0"})
    px1 = close_on(con, next_key).rename(columns={"close": "c1"})
    d = base.merge(px0, on="ts_code", how="left").merge(px1, on="ts_code", how="left")
    n_no_px0 = int(d["c0"].isna().sum())
    n_no_px1 = int(d["c1"].isna().sum())
    d = d.dropna(subset=["c0", "c1"])
    d = d[(d["c0"] > 0) & (d["c1"] > 0)].copy()
    d["ret"] = d["c1"] / d["c0"] - 1.0
    d = split_tercile(d)

    rows = []
    for p in POOLS:
        sub = d[d["pool"] == p]
        if len(sub) < MIN_PER_POOL:
            rows.append({"pool": p, "n": int(len(sub)), "ret": None,
                         "mv_median_yi": None, "note": f"样本不足（{len(sub)} 只）"})
            continue
        rows.append({"pool": p, "n": int(len(sub)),
                     "ret": float(sub["ret"].mean()),
                     "mv_median_yi": float(sub["total_mv"].median() / 1e4)})
    return {
        "anchor": anchor_key, "next": next_key,
        "universe_raw": n_raw, "after_board_st_filter": n_after_filter,
        "used": int(len(d)), "dropped_no_px_anchor": n_no_px0,
        "dropped_no_px_next": n_no_px1,
        "pools": rows,
    }


# ---------------------------------------------------------------- 主流程
def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=str(OUT))
    ap.add_argument("--db", default=str(DB))
    args = ap.parse_args()

    db_path = Path(args.db)
    if not db_path.exists():
        raise SystemExit(f"[数据] 副本不存在：{db_path}")
    if db_path.resolve() == (ROOT / "data" / "lihu_quant.duckdb").resolve():
        raise SystemExit("[数据] 拒绝直连主库（只允许副本）")

    con = duckdb.connect(str(db_path), read_only=True)
    print("=" * 100)
    print("市值三分位池滚动重切复核（只做池构成 + 横截面等权收益，不跑策略）")
    print("=" * 100)
    print(f"[数据] 副本 {db_path}（read_only）")

    # ---------- 0. 可用性审计
    print("\n【0】数据可用性审计（任务要求：全市场 × 8 个季度末 total_mv）")
    print(f"{'锚定日':<12}{'生效交易日':<12}{'行情只数':>8}{'市值只数':>8}  结论")
    audit = []
    anchor_keys = []
    for a in QUARTERS:
        key = resolve_anchor(con, a)
        n_px = len(close_on(con, key)) if key else 0
        n_mv = len(q(con, "SELECT ts_code FROM daily_basic WHERE "
                          "strftime(CAST(trade_date AS DATE),'%Y%m%d') = ? "
                          "AND total_mv IS NOT NULL", [key])) if key else 0
        ok = n_mv >= 3 * MIN_PER_POOL
        audit.append({"anchor": a, "effective_date": key, "n_quotes": n_px,
                      "n_total_mv": n_mv, "computable": ok})
        anchor_keys.append(key)
        print(f"{a:<12}{str(key):<12}{n_px:>8}{n_mv:>8}  {'可计算' if ok else '无法计算（缺 total_mv）'}")
    n_mv_dates = q(con, "SELECT count(DISTINCT strftime(CAST(trade_date AS DATE),'%Y%m%d')) n, "
                        "count(DISTINCT ts_code) c FROM daily_basic WHERE total_mv IS NOT NULL").iloc[0]
    print(f"\n  daily_basic 覆盖：{int(n_mv_dates['n'])} 个交易日、{int(n_mv_dates['c'])} 只股票"
          f"（全市场名单 {len(eligible(con))} 只）")
    print(f"  daily_quotes 覆盖：{q(con, 'SELECT count(DISTINCT ts_code) c FROM daily_quotes').iloc[0]['c']} 只股票")
    print("  → 任务口径（全市场 8 期）**无法计算**；下面给出可计算的子集与代理口径。")

    # ---------- 1. 任务口径（official）：只有 2026-09-30 可算
    print("\n【1】任务口径（当日 total_mv 切池）：只有 2026-09-30 有市值")
    elig = eligible(con)
    official = []
    last_key = q(con, "SELECT max(strftime(CAST(trade_date AS DATE),'%Y%m%d')) k FROM daily_quotes").iloc[0]["k"]
    for a, key in zip(QUARTERS, anchor_keys):
        if key is None:
            official.append({"anchor": a, "computable": False, "reason": "无行情数据"})
            continue
        mv = q(con, "SELECT ts_code, total_mv FROM daily_basic WHERE "
                    "strftime(CAST(trade_date AS DATE),'%Y%m%d') = ? AND total_mv IS NOT NULL", [key])
        if len(mv) < 3 * MIN_PER_POOL:
            official.append({"anchor": a, "computable": False,
                             "reason": f"缺 daily_basic.total_mv（该日 {len(mv)} 只，需 ≥{3*MIN_PER_POOL}）"})
            continue
        nxt = anchor_keys[anchor_keys.index(key) + 1] if anchor_keys.index(key) + 1 < len(anchor_keys) else last_key
        rec = quarter_returns(con, mv, key, nxt, elig)
        rec["anchor_label"] = a
        rec["next_note"] = "数据末日 2026-11-10（下一锚定日 2026-12-31 超出数据范围）" if nxt == last_key else ""
        rec["computable"] = True
        official.append(rec)
        print(f"  {a}（{key} → {nxt}）：可用 {rec['used']} 只｜剔除无行情 {rec['dropped_no_px_anchor']}/"
              f"{rec['dropped_no_px_next']}｜板块/ST 剔除 {rec['universe_raw']-rec['after_board_st_filter']}")
        for p in rec["pools"]:
            print(f"      {p['pool']}：{p['n']:>3} 只｜中位市值 {p['mv_median_yi']} 亿｜"
                  f"等权收益 {'无法计算' if p['ret'] is None else f'{p[chr(114)+chr(101)+chr(116)]*100:+.2f}%'}")

    # ---------- 2. 代理口径（proxy）：2026-09-30 市值 + 后复权价回推
    print("\n【2】代理口径（非任务要求）：2026-09-30 市值 + 后复权价比例回推各期市值")
    print("      mv_t = mv_0930 × (close_t×adj_t)/(close_0930×adj_0930)；样本受后复权因子覆盖限制")
    snap = q(con, "SELECT ts_code, total_mv FROM daily_basic WHERE "
                  "strftime(CAST(trade_date AS DATE),'%Y%m%d') = ? AND total_mv IS NOT NULL", [SNAP_DATE])
    px_snap = close_on(con, SNAP_DATE).rename(columns={"close": "cs"})
    adj_snap = adj_on(con, SNAP_DATE).rename(columns={"adj_factor": "as_"})
    snap = snap.merge(px_snap, on="ts_code", how="left").merge(adj_snap, on="ts_code", how="left")
    snap = snap.dropna(subset=["cs", "as_"])
    snap = snap[(snap["cs"] > 0) & (snap["as_"] > 0)].rename(columns={"total_mv": "mv0"})
    print(f"      快照可用于回推的基准样本：{len(snap)} 只")

    proxy = []
    for a, key in zip(QUARTERS, anchor_keys):
        if key is None:
            proxy.append({"anchor": a, "computable": False, "reason": "无行情数据"})
            continue
        px = close_on(con, key).rename(columns={"close": "c"})
        adj = adj_on(con, key).rename(columns={"adj_factor": "adj"})
        m = snap.merge(px, on="ts_code", how="left").merge(adj, on="ts_code", how="left")
        n_no_px = int(m["c"].isna().sum())
        n_no_adj = int(m["adj"].isna().sum())
        m = m.dropna(subset=["c", "adj"])
        m = m[(m["c"] > 0) & (m["adj"] > 0)].copy()
        m["total_mv"] = m["mv0"] * (m["c"] * m["adj"]) / (m["cs"] * m["as_"])
        nxt = anchor_keys[anchor_keys.index(key) + 1] if anchor_keys.index(key) + 1 < len(anchor_keys) else last_key
        rec = quarter_returns(con, m[["ts_code", "total_mv"]], key, nxt, elig)
        rec["anchor_label"] = a
        rec["dropped_no_adj"] = n_no_adj
        rec["computable"] = rec["pools"][0]["ret"] is not None if rec["pools"] else False
        proxy.append(rec)
        if rec["computable"]:
            diff = rec["pools"][0]["ret"] - rec["pools"][2]["ret"]
            print(f"  {a}（{key}→{nxt}）：{rec['used']:>3} 只｜"
                  f"小 {rec['pools'][0]['ret']*100:+.2f}% 中 {rec['pools'][1]['ret']*100:+.2f}% "
                  f"大 {rec['pools'][2]['ret']*100:+.2f}%｜小−大 {diff*100:+.2f}pp")
        else:
            print(f"  {a}：无法计算")

    # ---------- 3. 统计
    def stats(records: list[dict], label: str) -> dict:
        ok = [r for r in records if r.get("computable") and r.get("pools")
              and all(p.get("ret") is not None for p in r["pools"])]
        if not ok:
            return {"label": label, "n_quarters": 0, "note": "无可计算季度"}
        diffs = [r["pools"][0]["ret"] - r["pools"][2]["ret"] for r in ok]
        cum = {p: float(np.prod([1 + r["pools"][i]["ret"] for r in ok]) - 1) for i, p in enumerate(POOLS)}
        loo = []
        for i in range(len(diffs)):
            rest = diffs[:i] + diffs[i + 1:]
            if rest:
                loo.append(float(np.mean(rest)))
        # 去掉最好的两期后还剩多少（检验「是否只由个别季度贡献」）
        srt = sorted(diffs, reverse=True)
        top2_out = float(np.mean(srt[2:])) if len(srt) > 2 else None
        return {
            "label": label, "n_quarters": len(ok),
            "quarters": [r["anchor_label"] for r in ok],
            "diff_pp": [round(d * 100, 3) for d in diffs],
            "mean_diff_pp": round(float(np.mean(diffs)) * 100, 3),
            "median_diff_pp": round(float(np.median(diffs)) * 100, 3),
            "positive_share": round(float(np.mean([d > 0 for d in diffs])), 4),
            "n_positive": int(np.sum([d > 0 for d in diffs])),
            "stdev_diff_pp": round(float(np.std(diffs, ddof=1)) * 100, 3) if len(diffs) > 1 else None,
            "cum_return": {p: round(v, 4) for p, v in cum.items()},
            "leave_one_out_mean_pp": {"min": round(min(loo) * 100, 3), "max": round(max(loo) * 100, 3)} if loo else None,
            "drop_top2_mean_pp": round(top2_out * 100, 3) if top2_out is not None else None,
            "best_quarter": ok[int(np.argmax(diffs))]["anchor_label"],
            "worst_quarter": ok[int(np.argmin(diffs))]["anchor_label"],
        }

    stat_official = stats(official, "official")
    stat_proxy = stats(proxy, "proxy")

    print("\n【3】汇总")
    for s in (stat_official, stat_proxy):
        print(f"\n  [{s['label']}] 可计算 {s.get('n_quarters', 0)} 期", end="")
        if s.get("n_quarters"):
            print(f"｜小−大盘 均值 {s['mean_diff_pp']:+.2f}pp 中位 {s['median_diff_pp']:+.2f}pp "
                  f"｜为正 {s['n_positive']}/{s['n_quarters']}（{s['positive_share']*100:.1f}%）"
                  f"｜逐期 {s['diff_pp']}")
            print(f"        全期累计：小盘 {s['cum_return']['小盘']*100:+.2f}% 中盘 "
                  f"{s['cum_return']['中盘']*100:+.2f}% 大盘 {s['cum_return']['大盘']*100:+.2f}%"
                  f"｜留一法均值区间 {s['leave_one_out_mean_pp']}")
        else:
            print(f"：{s.get('note')}")

    # ---------- 4. 结论
    conclusion = []
    if stat_official["n_quarters"] == 0:
        conclusion.append("任务口径（全市场 8 期）无法计算：daily_basic.total_mv 只有 2026-09-30 一期的 202 只，"
                          "其余 7 期该字段为空；daily_quotes 全库仅 496 只，2024/2025 锚定日只有 264~267 只有行情。")
    else:
        conclusion.append(f"任务口径只有 {stat_official['n_quarters']} 期可算，"
                          f"且该期截面仅有策略池成员（非全市场），不足以判断滚动稳定性。")
    if stat_proxy["n_quarters"]:
        conclusion.append(
            f"代理口径 {stat_proxy['n_quarters']} 期：小−大盘均值 {stat_proxy['mean_diff_pp']:+.2f}pp、"
            f"中位 {stat_proxy['median_diff_pp']:+.2f}pp、为正 {stat_proxy['n_positive']}/{stat_proxy['n_quarters']} 期；"
            f"逐期极值 {min(stat_proxy['diff_pp']):+.2f} / {max(stat_proxy['diff_pp']):+.2f}pp。")
        sd = stat_proxy.get("stdev_diff_pp")
        if sd is not None:
            conclusion.append(f"逐期差标准差 {sd:.2f}pp，"
                              f"{'远大于均值，方向不可判' if sd > abs(stat_proxy['mean_diff_pp']) else '小于均值，方向相对稳定'}。")
        conclusion.append("但该口径样本仅 ~139 只、全部来自当前策略池（幸存者+选择偏差），"
                          "且 close 未复权，**不能替代全市场结论**。")
    conclusion.append("要回答问题本身，需要补：① 全市场 daily_basic.total_mv 的 8 期快照；"
                      "② 全市场日行情（或复权因子）；二者本地均缺，未联网补数。")

    print("\n【4】结论")
    for i, c in enumerate(conclusion, 1):
        print(f"  {i}. {c}")

    out = {
        "meta": {
            "generated_by": "scripts/rerun_tercile_rolling.py",
            "db": str(db_path), "read_only": True,
            "quarters_requested": QUARTERS, "snapshot_date": SNAP_DATE,
            "pools": POOLS, "min_per_pool": MIN_PER_POOL,
            "return_definition": "池内个股 close(下一锚定日)/close(本锚定日)-1 的等权均值（未复权）",
            "filter": "剔除 688/300/301/北交所/ST",
        },
        "audit": audit,
        "coverage": {
            "daily_basic_rows": int(q(con, "SELECT count(*) c FROM daily_basic").iloc[0]["c"]),
            "daily_basic_codes": int(n_mv_dates["c"]),
            "daily_basic_date_min": q(con, "SELECT min(strftime(CAST(trade_date AS DATE),'%Y%m%d')) d FROM daily_basic").iloc[0]["d"],
            "daily_basic_date_max": q(con, "SELECT max(strftime(CAST(trade_date AS DATE),'%Y%m%d')) d FROM daily_basic").iloc[0]["d"],
            "quotes_codes": int(q(con, "SELECT count(DISTINCT ts_code) c FROM daily_quotes").iloc[0]["c"]),
            "stock_basic_codes": int(len(elig)),
            "last_trading_date": last_key,
        },
        "official": {"records": official, "stats": stat_official},
        "proxy": {
            "method": "mv_t = mv_20260930 × (close_t×adj_t)/(close_20260930×adj_20260930)",
            "caveats": [
                "非任务要求口径：任务要求用当期 total_mv 切池，该数据缺失，本层是回推代理",
                "样本 ~139 只、全部为当前策略池成员 → 幸存者偏差 + 选择偏差",
                "close 未复权（daily_quotes 原始价），分红/送转会污染个股区间收益",
                "回推忽略增发/回购/转债转股导致的股本变动",
            ],
            "records": proxy, "stats": stat_proxy,
        },
        "conclusion": conclusion,
    }
    outp = Path(args.out)
    outp.parent.mkdir(parents=True, exist_ok=True)
    outp.write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"\n[输出] {outp}")
    con.close()


if __name__ == "__main__":
    main()
