"""波段池稳健性/运气检验（P1-②③④）：随机入场·随机择时·随机排序对照 + 滚动窗口 + 口径对齐。

回答三个问题：
  1) 修正后 +37.7% 是来自选股/择时能力，还是金股池 beta + 单笔运气？
  2) 把“信号”随机化后收益分布如何？实际值处在什么分位（是否显著）？
  3) 实盘“每月≤1次调仓”与回测口径（月约 2.6 笔）差多少？

对照组（每组 N 次独立随机）：
  C1 同择时·随机选股：保持原策略每周入场笔数与日期，股票从当月金股池中随机抽
  C2 同标的·随机择时：保持原 90 笔标的，入场周在 ±8 周内随机平移（出场规则不变）
  C3 完全随机：周×当月金股池随机抽 90 个 (周, 标的)
  C4 同准入集·随机排序：三重共振通过者中随机挑（检验“估值分位低者优先”排序贡献）
  C5 同准入集·高估值优先：排序反向（对照 C4，检验排序方向是否有信息）
另附：滚动起点窗口、月度调仓 ≤1/≤2 次口径、金股池月度等权指数（beta 对照）。

用法：
  python scripts/swing_montecarlo.py [--sims=300] [--start=2023-10-02] [--end=2026-10-16]
                                     [--out=outputs/swing_montecarlo.json] [--tag=2023]
"""
from __future__ import annotations

import importlib.util
import json
import random
import statistics
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location("swingbt", ROOT / "scripts" / "run_swing_backtest.py")
bt = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(bt)  # type: ignore[union-attr]

OUT = ROOT / "outputs" / "swing_montecarlo.json"


def _arg(name: str, default):
    for a in sys.argv[1:]:
        if a.startswith(f"--{name}="):
            v = a.split("=", 1)[1]
            if isinstance(default, bool):
                return v.lower() in ("1", "true", "yes")
            if isinstance(default, int):
                return int(v)
            return v
    return default


def _q(vals: list, ps=(0.01, 0.05, 0.25, 0.5, 0.75, 0.95, 0.99)) -> dict:
    sv = sorted(vals)
    n = len(sv)
    return {f"p{int(p*100)}": round(sv[min(n - 1, max(0, int(p * (n - 1))))], 4) for p in ps}


def _summarize(vals: list, baseline: float) -> dict:
    sv = sorted(vals)
    n = len(sv)
    mean = sum(sv) / n
    sd = (sum((x - mean) ** 2 for x in sv) / n) ** 0.5
    return {
        "n_sims": n,
        "mean": round(mean, 4),
        "sd": round(sd, 4),
        "q": _q(vals),
        "baseline": round(baseline, 4),
        "baseline_percentile": round(sum(1 for v in sv if v < baseline) / n, 4),
        "p_control_ge_baseline": round(sum(1 for v in sv if v >= baseline) / n, 4),
    }


def window_prep(prep: dict, start: date, end: date) -> dict:
    """按窗口裁剪主日历/信号日历（per 复用，避免重复预处理）。"""
    a, b = start.isoformat().replace("-", ""), end.isoformat().replace("-", "")
    master = [d for d in prep["master"] if a <= d <= b]
    wd = {d: v for d, v in prep["week_dates"].items() if a <= d <= b}
    return {"master": master, "per": prep["per"], "week_dates": wd, "week_date_list": sorted(wd)}


def pass_filter(t: str, codes: list, per: dict, gold_map: dict, pos: dict) -> list:
    """复刻引擎三重共振准入（仅供对照排序使用）。"""
    out = []
    for code in codes:
        if code in pos or code not in gold_map:
            continue
        w = per[code]["wk_by_date"].get(t)
        if w is None:
            continue
        if w["val_pct"] is None or w["val_pct"] > bt.VAL_PCT_MAX:
            continue
        if w["adj_close"] <= w["ma30"]:
            continue
        if w["slope"] is None or w["slope"] < bt.SLOPE_MIN:
            continue
        out.append(code)
    return out


def gold_universe_beta(series: dict, gold: dict, start: date, end: date) -> dict:
    """金股池月度等权指数（beta 对照）：每月首个交易日等权买入、月末收盘卖出。"""
    # 交易日历
    days = sorted({r["d"] for s in series.values() for r in s["rows"] if r.get("close") is not None})
    a, b = start.isoformat().replace("-", ""), end.isoformat().replace("-", "")
    days = [d for d in days if a <= d <= b]
    if not days:
        return {}
    byd = {}
    for code, s in series.items():
        byd[code] = {r["d"]: (r["close"] * r["adj"]) if (r.get("close") is not None and r.get("adj") is not None) else None
                     for r in s["rows"]}
    months = sorted({d[:6] for d in days if gold.get(d[:6])})
    nav, rows = 1.0, []
    for m in months:
        mdays = [d for d in days if d[:6] == m]
        d0, d1 = mdays[0], mdays[-1]
        rets = []
        for code in gold.get(m, {}):
            p0 = byd.get(code, {}).get(d0)
            p1 = byd.get(code, {}).get(d1)
            if p0 and p1:
                rets.append(p1 / p0 - 1)
        if not rets:
            continue
        r = sum(rets) / len(rets)
        nav *= 1 + r
        rows.append({"month": m, "n": len(rets), "ret": round(r, 4), "nav": round(nav, 4)})
    return {"nav": round(nav, 4), "total_return": round(nav - 1, 4), "months": len(rows), "rows": rows}


def main() -> None:
    sims = _arg("sims", 300)
    start = date.fromisoformat(_arg("start", bt.WIN_START.isoformat()))
    end = date.fromisoformat(_arg("end", bt.WIN_END.isoformat()))
    tag = _arg("tag", start.strftime("%Y"))
    out_path = Path(_arg("out", str(OUT)))
    bt.WIN_START, bt.WIN_END = start, end

    print(f"[data] 载入候选序列（窗口 {start}~{end}）…", flush=True)
    gold, series = bt.load_code_series()
    prep = bt.build_prep(series, start, end)
    print(f"[data] 候选 {len(series)} 只 / 金股月 {len(gold)} / 交易日 {len(prep['master'])}", flush=True)

    base = bt.simulate(gold, series, bt.SLOPE_MIN, prep=prep)
    bm = base["metrics"]
    print(f"[base] total={bm['total_return']:+.4f} n={bm['n_trades']} dd={bm['max_drawdown']:.4f} pf={bm.get('profit_factor')}", flush=True)

    master, week_list = prep["master"], prep["week_date_list"]
    next_day = {master[i]: master[i + 1] for i in range(len(master) - 1)}
    entry_to_week = {next_day[w]: w for w in week_list if w in next_day}
    widx = {w: i for i, w in enumerate(week_list)}
    plan: dict = {}
    for t in base["trades"]:
        w = entry_to_week.get(t["entry_date"])
        if w:
            plan.setdefault(w, []).append(t["code"])
    n_base = len(base["trades"])
    print(f"[base] 入场计划周数 {len(plan)} 笔 {sum(len(v) for v in plan.values())}", flush=True)

    def run(factory, n: int, label: str) -> list:
        vals = []
        for s in range(n):
            rng = random.Random(20261020 + s)
            fn = factory(rng)
            r = bt.simulate(gold, series, bt.SLOPE_MIN, signal_fn=fn, prep=prep)
            vals.append(r["metrics"]["total_return"])
            if (s + 1) % 50 == 0:
                print(f"  [{label}] {s+1}/{n} mean={statistics.mean(vals):+.4f}", flush=True)
        return vals

    # C1 同择时·随机选股
    def c1(rng):
        def fn(t, codes, per, gold_map, pos, pending):
            k = len(plan.get(t, ()))
            if k <= 0:
                return []
            pool = [c for c in codes if c not in pos and c in gold_map]
            rng.shuffle(pool)
            return pool[:k]
        return fn

    # C2 同标的·随机择时（±8 周）
    def c2(rng):
        plan2: dict = {}
        for t in base["trades"]:
            w = entry_to_week.get(t["entry_date"])
            if w is None:
                continue
            j = min(max(0, widx[w] + rng.randint(-8, 8)), len(week_list) - 1)
            plan2.setdefault(week_list[j], []).append(t["code"])

        def fn(t, codes, per, gold_map, pos, pending):
            lst = plan2.get(t)
            if not lst:
                return []
            rng.shuffle(lst)
            return [c for c in lst if c not in pos]
        return fn

    # C3 完全随机（周 × 当月金股池）
    def c3(rng):
        plan3: dict = {}
        for _ in base["trades"]:
            w = week_list[rng.randrange(len(week_list))]
            cand = [c for c in prep["week_dates"].get(w, []) if c in gold.get(w[:6], {})]
            if cand:
                plan3.setdefault(w, []).append(rng.choice(cand))

        def fn(t, codes, per, gold_map, pos, pending):
            lst = plan3.get(t)
            if not lst:
                return []
            rng.shuffle(lst)
            return [c for c in lst if c not in pos]
        return fn

    # C4 同准入集·随机排序
    def c4(rng):
        def fn(t, codes, per, gold_map, pos, pending):
            ps = pass_filter(t, codes, per, gold_map, pos)
            rng.shuffle(ps)
            return ps
        return fn

    # C5 同准入集·高估值优先（反向排序）
    def c5(rng):
        def fn(t, codes, per, gold_map, pos, pending):
            ps = pass_filter(t, codes, per, gold_map, pos)
            ps.sort(key=lambda c: -per[c]["wk_by_date"][t]["val_pct"])
            return ps
        return fn

    out = {
        "generated_at": date.today().isoformat(),
        "window": [start.isoformat(), end.isoformat()],
        "val_window_days": bt.VAL_WINDOW_DAYS,
        "baseline": {"metrics": bm, "n_entries_plan": n_base, "plan_weeks": len(plan)},
        "controls": {},
        "month_cap": {},
        "rolling_windows": {},
        "gold_universe_beta": {},
    }

    for label, factory in (("C1_同择时_随机选股", c1), ("C2_同标的_随机择时", c2),
                           ("C3_完全随机", c3), ("C4_同准入集_随机排序", c4), ("C5_同准入集_高估值优先", c5)):
        print(f"[sim] {label} × {sims} …", flush=True)
        vals = run(factory, sims, label)
        out["controls"][label] = _summarize(vals, bm["total_return"])
        print(f"[sim] {label} -> mean={out['controls'][label]['mean']:+.4f} "
              f"baseline_pct={out['controls'][label]['baseline_percentile']:.2%}", flush=True)

    # 月度调仓上限（实盘口径对齐）
    for cap in (1, 2):
        r = bt.simulate(gold, series, bt.SLOPE_MIN, month_cap=cap, prep=prep)
        out["month_cap"][f"cap_{cap}"] = {
            "metrics": r["metrics"],
            "entries_per_month": round(r["metrics"]["n_trades"] / max(1, len({m for m in (t["entry_date"][:6] for t in base["trades"])})), 2),
        }
        print(f"[cap] 每月≤{cap} 笔：total={r['metrics']['total_return']:+.4f} n={r['metrics']['n_trades']}", flush=True)

    # 滚动起点窗口
    starts = ["2023-10-02", "2024-01-02", "2024-04-01", "2024-07-01", "2024-10-08", "2025-01-02", "2025-04-01"]
    for s0 in starts:
        sd = date.fromisoformat(s0)
        if sd >= end:
            continue
        wprep = window_prep(prep, sd, end)
        r = bt.simulate(gold, series, bt.SLOPE_MIN, prep=wprep)
        out["rolling_windows"][s0] = {"metrics": r["metrics"], "n_trades": r["metrics"]["n_trades"]}
        print(f"[roll] {s0} -> total={r['metrics']['total_return']:+.4f} n={r['metrics']['n_trades']}", flush=True)

    # 金股池月度等权 beta 对照
    try:
        beta = gold_universe_beta(series, gold, start, end)
        # 同期沪深300
        bench = bt._fetch_benchmark()
        bd = [d for d in sorted(bench) if start.isoformat().replace("-", "") <= d <= end.isoformat().replace("-", "")]
        if bd:
            beta["hs300"] = round(bench[bd[-1]] / bench[bd[0]] - 1, 4)
        out["gold_universe_beta"] = beta
        print(f"[beta] 金股池月度等权 {beta.get('total_return'):+.4f} 月数={beta.get('months')} 沪深300={beta.get('hs300')}", flush=True)
    except Exception as e:
        out["gold_universe_beta"] = {"error": str(e)}
        print(f"[beta] 失败（忽略）：{e}", flush=True)

    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"[done] -> {out_path}", flush=True)


if __name__ == "__main__":
    main()
