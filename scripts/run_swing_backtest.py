"""波段池策略三年回测（2023-10 ~ 2026-10，真实数据，与 docs/波段池纪律.md 同源）。

策略机械口径（三重共振，信号于周五收盘确认）：
  ① 景气 = 当月券商金股名单内（broker_recommend，月首发布当月生效，PIT 无未来函数）
  ② 估值 = min(PE_TTM 分位, PB 分位) ≤ 30%（近 3 年约 756 交易日分布，仅用当期已知数据）
  ③ 趋势 = 周收盘 > 30 周线（前复权）且 20 周线 4 周斜率 ≥ -0.2%（走平或向上；敏感性用 ≥ 0）
交易规则：
  入场：信号次一交易日收盘买入（保守）；估值分位低者优先；5 槽位 × 20% 资金，单票不加仓
  出场（任一触发，均于次一交易日收盘执行）：
    a) 复权价格自买入 -15%（-12%~-15% 硬止损取中值）
    b) 周收盘 < 30 周线（趋势破坏）
    c) 周收盘自峰值回撤 8%（移动止盈）
    d) 持有 ≥ 63 个交易日（3 个月时间止损）
  成本：双边 0.2%（佣金+滑点）；基准：沪深300 000300.SH 价格指数
已知偏差（如实声明）：金股名单偏向大市值/高研究覆盖；仅 3 年单一市场环境；
"逻辑破"等定性条件未参与回测；月调频率约束（月 1 次）未机械执行。

用法：python scripts/run_swing_backtest.py [--strict-slope]
输出：outputs/swing_backtest.json + outputs/reports/<日期>_波段池回测报告.md
"""
from __future__ import annotations

import bisect
import json
import sys
import time
from datetime import date, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from loguru import logger

from lihu_quantify.config import get_settings
from lihu_quantify.data.tushare_client import TushareClient

BT = ROOT / "data" / "swing_bt"
OUT_JSON = ROOT / "outputs" / "swing_backtest.json"
OUT_MD = ROOT / "outputs" / "reports"
BENCH_CACHE = BT / "bench_000300.json"
SIMS_CACHE = ROOT / "outputs" / "_swing_sims_cache.pkl"

WIN_START, WIN_END = date(2023, 10, 2), date(2026, 10, 16)
VAL_PCT_MAX = 0.30
MA_W = 30
SLOPE_MIN = -0.002          # 走平或向上（4 周斜率）
SLOPE_MIN_STRICT = 0.0
VAL_WINDOW_DAYS = 1095      # 估值分位窗口：近 3 年（日历窗，修正“756 个非空观测”会被数据起点影响）
STOP_LOSS = 0.85            # -15%
TRAIL = 0.80                # 峰值回撤 20% 灾难线（v2：删除 8% 噪声区止盈，2026-10-18 修订）
MAX_HOLD = 126              # 交易日 ≈ 6 个月收获期（v2：原 63 日砍右尾，盈亏比 5→10）
SLOTS = 5
COST_SIDE = 0.001           # 单边 0.1%


def load_code_series() -> dict:
    """{code: {name, rows:[{d,close,adj,pe,pb}]}}"""
    gold = json.loads((BT / "gold_membership.json").read_text(encoding="utf-8"))
    names = {}
    for mm in gold.values():
        for code, e in mm.items():
            names.setdefault(code, e.get("name", ""))
    out = {}
    for f in sorted((BT / "px").glob("*.json")):
        code = f.stem.replace("_", ".")
        rows = json.loads(f.read_text(encoding="utf-8"))
        if rows:
            out[code] = {"name": names.get(code, ""), "rows": rows}
    return gold, out


def pct_rank(values: list, v: float) -> float | None:
    xs = [x for x in values if x is not None]
    if not xs or v is None:
        return None
    return sum(1 for x in xs if x < v) / len(xs)


def build_weeks(rows: list, val_window_days: int | None = None) -> list:
    """周线索引：每周最后交易日 + 前复权收盘 + MA20/30 + 斜率 + 估值分位。

    估值分位口径（2026-10-20 修正）：以“当日往前 val_window_days 个自然日”为窗口（默认 1095≈3 年），
    只在该日历窗内统计 PE/PB 分位。旧实现取“最近 756 个非空观测”，当个股 PE 有缺失时会一路回溯到
    5 年以上（例如 000530 在 2023-10 的窗口回溯到 2018-03），使分位随数据起点漂移——这是
    2023-10 窗口在“完整数据”下由 +37.7% 翻成 -47.5% 的根因。legacy 模式（val_window_days=0）仅用于复现旧结果。
    """
    if val_window_days is None:
        val_window_days = VAL_WINDOW_DAYS
    px = [r for r in rows if r.get("close") is not None and r.get("adj") is not None]
    val = [r for r in rows if r.get("pe") is not None or r.get("pb") is not None]
    if len(px) < MA_W + 8:
        return []
    weeks: dict = {}
    for r in px:
        y, w, _ = date(int(r["d"][:4]), int(r["d"][4:6]), int(r["d"][6:8])).isocalendar()
        weeks[f"{y}-{w:02d}"] = r   # 覆盖 → 留每周最后交易日
    keys = sorted(weeks)
    closes = [weeks[k]["close"] * weeks[k]["adj"] for k in keys]
    out = []
    if val_window_days:
        # 日历窗：维护窗口内 PE/PB 的有序列表（增量插入/删除，避免逐周全量扫描）
        vd = [v["d"] for v in val]
        pe_vals = [v["pe"] for v in val]
        pb_vals = [v["pb"] for v in val]
        sorted_pe: list = []
        sorted_pb: list = []
        lo = hi = 0
        for i, k in enumerate(keys):
            if i < MA_W:
                continue
            r = weeks[k]
            d = r["d"]
            cutoff = (date(int(d[:4]), int(d[4:6]), int(d[6:8])) - timedelta(days=val_window_days)).strftime("%Y%m%d")
            while hi < len(vd) and vd[hi] <= d:
                if pe_vals[hi] is not None:
                    bisect.insort(sorted_pe, pe_vals[hi])
                if pb_vals[hi] is not None:
                    bisect.insort(sorted_pb, pb_vals[hi])
                hi += 1
            while lo < hi and vd[lo] < cutoff:
                if pe_vals[lo] is not None:
                    j = bisect.bisect_left(sorted_pe, pe_vals[lo])
                    sorted_pe.pop(j)
                if pb_vals[lo] is not None:
                    j = bisect.bisect_left(sorted_pb, pb_vals[lo])
                    sorted_pb.pop(j)
                lo += 1
            adj_close = r["close"] * r["adj"]
            ma30 = sum(closes[i - MA_W + 1:i + 1]) / MA_W
            ma20 = sum(closes[i - 19:i + 1]) / 20
            if i >= 24:
                ma20_prev = sum(closes[i - 23:i - 3]) / 20
                slope = ma20 / ma20_prev - 1
            else:
                slope = None
            pe_last = next((pe_vals[j] for j in range(hi - 1, lo - 1, -1) if pe_vals[j] is not None), None)
            pb_last = next((pb_vals[j] for j in range(hi - 1, lo - 1, -1) if pb_vals[j] is not None), None)
            pe_p = (bisect.bisect_left(sorted_pe, pe_last) / len(sorted_pe)) if (pe_last is not None and pe_last > 0 and sorted_pe) else None
            pb_p = (bisect.bisect_left(sorted_pb, pb_last) / len(sorted_pb)) if (pb_last is not None and sorted_pb) else None
            cands = [x for x in (pe_p, pb_p) if x is not None]
            out.append({"wk": k, "date": d, "adj_close": adj_close, "close": r["close"],
                        "ma20": ma20, "ma30": ma30, "slope": slope,
                        "val_pct": (min(cands) if cands else None)})
        return out
    # legacy：最近 756 个非空观测（复现历史结果用，勿用于新结论）
    for i, k in enumerate(keys):
        if i < MA_W:
            continue
        r = weeks[k]
        d = r["d"]
        adj_close = r["close"] * r["adj"]
        ma30 = sum(closes[i - MA_W + 1:i + 1]) / MA_W
        ma20 = sum(closes[i - 19:i + 1]) / 20
        if i >= 24:
            ma20_prev = sum(closes[i - 23:i - 3]) / 20
            slope = ma20 / ma20_prev - 1
        else:
            slope = None
        hist_pe, hist_pb = [], []
        for v in val:
            if v["d"] > d:
                break
            if v.get("pe") is not None:
                hist_pe.append(v["pe"])
            if v.get("pb") is not None:
                hist_pb.append(v["pb"])
        pe_last = next((v["pe"] for v in reversed(val) if v["d"] <= d and v.get("pe") is not None), None)
        pb_last = next((v["pb"] for v in reversed(val) if v["d"] <= d and v.get("pb") is not None), None)
        pe_p = pct_rank(hist_pe[-756:], pe_last) if (pe_last is not None and pe_last > 0) else None
        pb_p = pct_rank(hist_pb[-756:], pb_last)
        cands = [x for x in (pe_p, pb_p) if x is not None]
        out.append({"wk": k, "date": d, "adj_close": adj_close, "close": r["close"],
                    "ma20": ma20, "ma30": ma30, "slope": slope,
                    "val_pct": (min(cands) if cands else None)})
    return out


def build_prep(series: dict, win_start: date | None = None, win_end: date | None = None) -> dict:
    """一次性预处理：主日历 + 每代码日线/周线索引。

    多变体与蒙特卡洛对照复用同一份数据（周线特征挂在 series 上），避免重复 build_weeks。
    """
    win_start = win_start or WIN_START
    win_end = win_end or WIN_END
    master = sorted({r["d"] for s in series.values() for r in s["rows"] if r.get("close") is not None})
    a, b = win_start.isoformat().replace("-", ""), win_end.isoformat().replace("-", "")
    master = [d for d in master if a <= d <= b]
    per = {}
    for code, s in series.items():
        byd = {r["d"]: r for r in s["rows"] if r.get("close") is not None}
        wkey = f"weeks_v{VAL_WINDOW_DAYS}"
        weeks = s.get(wkey)
        if weeks is None:
            weeks = build_weeks(s["rows"], VAL_WINDOW_DAYS)
            s[wkey] = weeks
        if not weeks:
            continue
        per[code] = {"byd": byd, "weeks": weeks, "wk_by_date": {w["date"]: w for w in weeks}, "name": s["name"]}
    week_dates = {}
    for code, p in per.items():
        for d in p["wk_by_date"]:
            week_dates.setdefault(d, []).append(code)
    return {"master": master, "per": per, "week_dates": week_dates, "week_date_list": sorted(week_dates)}


def simulate(gold: dict, series: dict, slope_min: float, trail: float = TRAIL, ma20_exit: bool = False,
             time_stop: int = MAX_HOLD, be_trigger: float | None = None,
             signal_fn=None, month_cap: int | None = None, max_miss: int = 40,
             prep: dict | None = None) -> dict:
    """主模拟器。

    signal_fn(t, week_codes, per, gold_map, pos, pending) -> list[code]：
        外部实验钩子（随机入场/随机择时/随机排序对照），不改变出场与记账逻辑。
    month_cap：每月最多新开仓数（用于对齐实盘“月调仓≤1次”口径）。
    prep：复用 build_prep 的预处理结果（多实验共享，避免重复计算周线）。
    """
    if prep is None:
        prep = build_prep(series)
    master = prep["master"]
    per = prep["per"]
    week_dates = prep["week_dates"]

    def fadj(code, d):
        p = per[code]
        if d in p["byd"]:
            return p["byd"][d]["close"] * p["byd"][d]["adj"]
        return None

    cash = 1.0
    pos = {}      # code -> {value, entry_adj, entry_date, peak, days}
    trades = []
    curve = []
    equity_daily = []
    funnel = {"week_pairs": 0, "gold": 0, "val_pass": 0, "trend_pass": 0, "signals": 0, "entries": 0}
    month_entries: dict = {}
    pending = []  # [(code, 'exit'|'enter', reason)] 次日收盘执行
    next_day = {master[i]: master[i + 1] for i in range(len(master) - 1)}
    days_count = {c: 0 for c in per}

    for t in master:
        # 1) 执行次日指令
        carry = []   # 审计修复(2026-10-19)：停牌无法成交的指令顺延到下一交易日，原实现静默丢弃
        for code, kind, reason in pending:
            adj_t = fadj(code, t)
            if adj_t is None:
                carry.append((code, kind, reason))
                continue
            if kind == "exit" and code in pos:
                p = pos[code]
                v = p["value_costed"] * (adj_t / p["entry_adj"])   # 审计修复：单次盯市（原实现重复乘比例→收益被平方）
                proceeds = v * (1 - COST_SIDE)
                cash += proceeds
                ret = proceeds / (p["value_costed"]) - 1
                trades.append({"code": code, "name": per[code]["name"], "entry_date": p["entry_date"],
                               "exit_date": t, "entry_close": round(p["entry_close"], 2),
                               "exit_close": round(per[code]["byd"].get(t, {}).get("close", 0) or 0, 2),
                               "ret": round(ret, 4), "hold_days": p["days"], "reason": reason})
                del pos[code]
            elif kind == "enter" and code not in pos:
                funnel["entries"] += 1
                free = min(0.2, cash)
                if free > 0.05:
                    used = free * (1 - COST_SIDE)
                    cash -= free
                    pos[code] = {"value": used, "value_costed": used, "entry_adj": adj_t,
                                 "entry_close": per[code]["byd"][t]["close"], "entry_date": t,
                                 "peak": adj_t, "days": 0}
        pending = carry
        # 2) 日内持仓推进与出场判断
        for code in list(pos):
            p = pos[code]
            p["days"] += 1
            adj_t = fadj(code, t)
            if adj_t is None:
                p["miss"] = p.get("miss", 0) + 1
                if max_miss and p["miss"] >= max_miss:
                    # 长期停牌/退市保护（扩样本新增）：按最后已知价格强制清算，
                    # 原实现会让这类标的停在成本价直到期末，等于假装没有损失（幸存者偏差）。
                    proceeds = p["value"] * (1 - COST_SIDE)
                    cash += proceeds
                    last_close = next((per[code]["byd"][d]["close"] for d in sorted(per[code]["byd"], reverse=True)
                                       if d <= t), None)
                    trades.append({"code": code, "name": per[code]["name"], "entry_date": p["entry_date"],
                                   "exit_date": t, "entry_close": round(p["entry_close"], 2),
                                   "exit_close": round(last_close, 2) if last_close else 0,
                                   "ret": round(proceeds / p["value_costed"] - 1, 4),
                                   "hold_days": p["days"], "reason": f"停牌/退市超{max_miss}日"})
                    del pos[code]
                continue
            p["miss"] = 0
            p["value"] = p["value_costed"] * (adj_t / p["entry_adj"])
            w = per[code]["wk_by_date"].get(t)
            if w:
                p["peak"] = max(p["peak"], w["adj_close"])
            if adj_t / p["entry_adj"] <= STOP_LOSS:
                pending.append((code, "exit", "硬止损-15%"))
            elif be_trigger and adj_t / p["entry_adj"] >= be_trigger and adj_t / p["entry_adj"] <= 1.0:
                pending.append((code, "exit", "保本止损"))
            elif p["days"] >= time_stop:
                pending.append((code, "exit", f"时间止损{time_stop}日"))
            elif w:
                if w["adj_close"] < w["ma30"]:
                    pending.append((code, "exit", "破30周线"))
                elif trail > 0 and w["adj_close"] < p["peak"] * trail:
                    pending.append((code, "exit", "移动止盈"))
                elif ma20_exit and w["adj_close"] < w["ma20"]:
                    pending.append((code, "exit", "破20周线"))
        # 3) 周线信号 → 次日入场
        if t in week_dates and len(pos) + len([x for x in pending if x[1] == "enter"]) < SLOTS:
            month = t[:6]
            gold_map = gold.get(month, {})
            slots_left = SLOTS - len(pos) - len([x for x in pending if x[1] == "enter"])
            if month_cap is not None:
                slots_left = min(slots_left, month_cap - month_entries.get(month, 0))
            if signal_fn is not None:
                chosen = list(signal_fn(t, week_dates[t], per, gold_map, pos, pending))
                for code in chosen[:max(0, slots_left)]:
                    if next_day.get(t) and code not in pos:
                        pending.append((code, "enter", ""))
                        month_entries[month] = month_entries.get(month, 0) + 1
            else:
                sig = []
                for code in week_dates[t]:
                    funnel["week_pairs"] += 1
                    if code in pos or code not in gold_map:
                        continue
                    funnel["gold"] += 1
                    w = per[code]["wk_by_date"][t]
                    if w["val_pct"] is None or w["val_pct"] > VAL_PCT_MAX:
                        continue
                    funnel["val_pass"] += 1
                    if w["adj_close"] <= w["ma30"]:
                        continue
                    funnel["trend_pass"] += 1
                    if w["slope"] is None or w["slope"] < slope_min:
                        continue
                    funnel["signals"] += 1
                    sig.append((w["val_pct"], code))
                sig.sort()
                for _, code in sig[:max(0, slots_left)]:
                    if next_day.get(t):
                        pending.append((code, "enter", ""))
                        month_entries[month] = month_entries.get(month, 0) + 1
        # 4) 记账
        eq = cash + sum(p["value"] for p in pos.values())
        equity_daily.append((t, eq))

    # 清仓收尾（按最后价格；停牌则用最后一个可用收盘价）
    for code, p in list(pos.items()):
        v = p["value"]   # 最后已知盯市价（停牌日沿用上一有效价，而非回退到成本价）
        cash += v * (1 - COST_SIDE)
        last_close = next((per[code]["byd"][d]["close"] for d in sorted(per[code]["byd"], reverse=True)
                           if d <= master[-1]), None)
        trades.append({"code": code, "name": per[code]["name"], "entry_date": p["entry_date"],
                       "exit_date": master[-1], "entry_close": round(p["entry_close"], 2),
                       "exit_close": round(last_close, 2) if last_close else 0,
                       "ret": round(v * (1 - COST_SIDE) / p["value_costed"] - 1, 4),
                       "hold_days": p["days"], "reason": "期末清仓"})
        del pos[code]
    equity_daily.append((master[-1], cash))

    # 指标
    eqs = [e for _, e in equity_daily]
    peak = -1.0
    max_dd = 0.0
    for e in eqs:
        peak = max(peak, e)
        max_dd = max(max_dd, (peak - e) / peak if peak > 0 else 0)
    total = eqs[-1] / eqs[0] - 1
    days = len(eqs)
    cagr = (eqs[-1] / eqs[0]) ** (252 / max(days, 1)) - 1
    rets = [eqs[i] / eqs[i - 1] - 1 for i in range(1, len(eqs))]
    mean = sum(rets) / len(rets) if rets else 0
    var = sum((r - mean) ** 2 for r in rets) / len(rets) if rets else 1
    sharpe = (mean / (var ** 0.5)) * (252 ** 0.5) if var > 0 else 0
    wins = [t0["ret"] for t0 in trades if t0["ret"] > 0]
    losses = [t0["ret"] for t0 in trades if t0["ret"] <= 0]
    pf = (sum(wins) / abs(sum(losses))) if losses and sum(losses) != 0 else None
    avg_win = sum(wins) / len(wins) if wins else None
    avg_loss = sum(losses) / len(losses) if losses else None
    win_loss_ratio = (avg_win / abs(avg_loss)) if (avg_win and avg_loss) else None
    yearly = {}
    for t0 in trades:
        y = t0["exit_date"][:4]
        yearly.setdefault(y, {"trades": 0, "wins": 0})
        yearly[y]["trades"] += 1
        yearly[y]["wins"] += 1 if t0["ret"] > 0 else 0
    for y, e0 in yearly.items():
        pts = [(d, e) for d, e in equity_daily if d[:4] == y]
        e0["ret"] = round((pts[-1][1] / pts[0][1] - 1) if len(pts) > 1 else 0, 4)
        e0["win_rate"] = round(e0["wins"] / e0["trades"], 2) if e0["trades"] else None
    curve_pts = []
    last_wk = None
    for d, e in equity_daily:
        y, w, _ = date(int(d[:4]), int(d[4:6]), int(d[6:8])).isocalendar()
        wk = f"{y}-{w:02d}"
        if wk != last_wk:
            curve_pts.append({"date": d, "equity": round(e, 4)})
            last_wk = wk
    reasons = {}
    for t0 in trades:
        reasons[t0["reason"]] = reasons.get(t0["reason"], 0) + 1
    # 集中度审计（2026-10-19 审计新增）：剔除 TOPn 盈利单后的近似累计收益
    ts_sorted = sorted(trades, key=lambda x: x["ret"], reverse=True)

    def _eq_without(k: int):
        if len(ts_sorted) <= k:
            return None
        drop = {id(x) for x in ts_sorted[:k]}
        eq2 = 1.0
        for t0 in trades:
            if id(t0) in drop:
                continue
            eq2 *= (1 + 0.2 * t0["ret"])
        return round(eq2 - 1, 4)

    concentration = {
        "note": "按单槽 20% 权重对交易序列复利近似（与组合路径有小误差）",
        "ex_top1": _eq_without(1), "ex_top3": _eq_without(3), "ex_top5": _eq_without(5),
        "top1": ({"code": ts_sorted[0]["code"], "name": ts_sorted[0]["name"], "ret": ts_sorted[0]["ret"]}
                 if ts_sorted else None),
        "top3": [{"code": t0["code"], "name": t0["name"], "ret": t0["ret"]} for t0 in ts_sorted[:3]],
    }
    logger.info(f"漏斗: {funnel} 出场分布: {reasons} 集中度: ex_top1={concentration['ex_top1']}")
    return {
        "funnel": funnel,
        "exit_reasons": reasons,
        "concentration": concentration,
        "metrics": {
            "total_return": round(total, 4), "cagr": round(cagr, 4), "max_drawdown": round(max_dd, 4),
            "sharpe": round(sharpe, 2), "win_rate": round(len(wins) / len(trades), 4) if trades else None,
            "profit_factor": round(pf, 2) if pf else None, "n_trades": len(trades),
            "avg_win": round(avg_win, 4) if avg_win else None,
            "avg_loss": round(avg_loss, 4) if avg_loss else None,
            "win_loss_ratio": round(win_loss_ratio, 2) if win_loss_ratio else None,
            "avg_hold_days": round(sum(t0["hold_days"] for t0 in trades) / len(trades), 1) if trades else None,
            "avg_trade_ret": round(sum(t0["ret"] for t0 in trades) / len(trades), 4) if trades else None,
        },
        "yearly": {y: yearly[y] for y in sorted(yearly)},
        "equity_curve": curve_pts,
        "trades": trades,
    }


def _load_sims_cache():
    """sim 结果缓存（引擎文件更新后自动失效，避免重复 12 分钟计算）。"""
    import pickle
    try:
        if SIMS_CACHE.exists() and SIMS_CACHE.stat().st_mtime > Path(__file__).stat().st_mtime:
            with SIMS_CACHE.open("rb") as f:
                return pickle.load(f)
    except Exception as e:
        logger.warning(f"sim 缓存读取失败（忽略）: {e}")
    return None


def _save_sims_cache(payload) -> None:
    import pickle
    try:
        SIMS_CACHE.parent.mkdir(parents=True, exist_ok=True)
        with SIMS_CACHE.open("wb") as f:
            pickle.dump(payload, f)
        logger.info(f"sim 结果缓存已写入: {SIMS_CACHE}")
    except Exception as e:
        logger.warning(f"sim 缓存写入失败（忽略）: {e}")


def _fetch_benchmark() -> dict:
    """沪深300 基准（文件缓存 + 重试 + 失败降级，绝不因网络问题丢弃回测结果）。"""
    need_start = WIN_START.isoformat().replace("-", "")
    if BENCH_CACHE.exists():
        try:
            d = json.loads(BENCH_CACHE.read_text(encoding="utf-8"))
            data = d.get("data") or {}
            # 覆盖检查：缓存起点必须不晚于窗口起点，否则重取（扩样本到 2021 时旧缓存会失效）
            if data and min(data) <= need_start:
                logger.info(f"基准读缓存：{BENCH_CACHE.name}（{d.get('fetched_at')}，{len(data)} 日）")
                return data
        except Exception:
            pass
    try:
        cfg = get_settings()
        raw = Path(cfg.tushare.token_file).read_text(encoding="utf-8").strip()
        import re
        token = cfg.tushare.token or (re.search(r"token=([A-Za-z0-9]+)", raw).group(1) if raw.startswith("{") else raw)
        c = TushareClient(token=token, cache_dir=ROOT / "data" / "cache", cache_mtime_ttl=0)
        idx = None
        for attempt in range(3):
            try:
                idx = c.query("index_daily", {"ts_code": "000300.SH", "start_date": "20200101", "end_date": "20261016"})
                break
            except Exception as e:
                logger.warning(f"基准取数失败（第 {attempt + 1} 次）: {e}")
                idx = None
                time.sleep(2 * (attempt + 1))
        bench = {}
        if idx is not None and not idx.empty:
            for _, r in idx.iterrows():
                bench[str(r["trade_date"])] = float(r["close"])
            BENCH_CACHE.parent.mkdir(parents=True, exist_ok=True)
            BENCH_CACHE.write_text(json.dumps({"fetched_at": date.today().isoformat(), "data": bench},
                                              ensure_ascii=False), encoding="utf-8")
            logger.info(f"基准已缓存：{len(bench)} 个交易日 → {BENCH_CACHE.name}")
        return bench
    except Exception as e:
        logger.warning(f"基准不可得，跳过基准对比（不影响策略指标）: {e}")
        return {}


def main() -> None:
    global WIN_START, WIN_END, VAL_WINDOW_DAYS
    strict = "--strict-slope" in sys.argv
    only_base = "--only-baseline" in sys.argv
    out_suffix = ""
    for a in sys.argv[1:]:
        if a.startswith("--start="):
            WIN_START = date.fromisoformat(a.split("=", 1)[1])
        elif a.startswith("--end="):
            WIN_END = date.fromisoformat(a.split("=", 1)[1])
        elif a.startswith("--out-suffix="):
            out_suffix = a.split("=", 1)[1]
        elif a.startswith("--val-window="):
            v = a.split("=", 1)[1]
            VAL_WINDOW_DAYS = 0 if v == "legacy" else int(v)
    out_json = OUT_JSON if not out_suffix else OUT_JSON.with_name(f"swing_backtest{out_suffix}.json")
    cached = None if only_base else _load_sims_cache()
    if (cached is not None
            and cached[0].get("window") == [WIN_START.isoformat(), WIN_END.isoformat()]
            and cached[0].get("val_window_days") == VAL_WINDOW_DAYS):
        universe, res, res_strict, res_trail12, res_notrail, res_live, res_t189 = cached
        logger.info(f"复用 sim 结果缓存：{universe}")
    else:
        gold, series = load_code_series()
        universe = {"n_codes": len(series), "n_gold_months": len(gold),
                    "window": [WIN_START.isoformat(), WIN_END.isoformat()],
                    "val_window_days": VAL_WINDOW_DAYS}
        logger.info(f"候选数据 {len(series)} 只，金股月份数 {len(gold)}，窗口 {WIN_START}~{WIN_END}")
        prep = build_prep(series)
        res = simulate(gold, series, SLOPE_MIN, prep=prep)
        if only_base:
            res_strict = res_trail12 = res_notrail = res_live = res_t189 = None
        else:
            # 敏感性：严格趋势口径 + 出场参数变体（诊断用，不构成参数优选）
            res_strict = simulate(gold, series, SLOPE_MIN_STRICT, prep=prep)
            res_trail12 = simulate(gold, series, SLOPE_MIN, trail=0.88, prep=prep)
            res_notrail = simulate(gold, series, SLOPE_MIN, trail=0.0, prep=prep)
            # v1 规则（修订前纪律原文）：移动止盈=回撤8% 或 破20周线 + 63 日时间帽
            res_live = simulate(gold, series, SLOPE_MIN, trail=0.92, ma20_exit=True, time_stop=63, prep=prep)
            # 时间帽 189 日反例（证明收获期必要）
            res_t189 = simulate(gold, series, SLOPE_MIN, trail=0.80, time_stop=189, prep=prep)
            _save_sims_cache((universe, res, res_strict, res_trail12, res_notrail, res_live, res_t189))

    # 基准：沪深300（可降级）
    bench = _fetch_benchmark()
    b0 = next((bench[d] for d in sorted(bench) if d >= WIN_START.isoformat().replace("-", "")), None)
    for pt in res["equity_curve"]:
        bd = next((bench[d] for d in sorted(bench) if d <= pt["date"]), None)
        pt["bench"] = round(bd / b0, 4) if (bd and b0) else None
    b_last = res["equity_curve"][-1].get("bench")
    res["metrics"]["bench_return"] = b_last - 1 if b_last else None
    res["metrics"]["excess_return"] = round(res["metrics"]["total_return"] - (b_last - 1), 4) if b_last else None

    sensitivity = {}
    if res_strict is not None:
        sensitivity = {
            "strict_trend": {
                "label": "严格趋势口径（20 周线必须向上，斜率≥0）",
                "metrics": res_strict["metrics"], "funnel": res_strict["funnel"], "yearly": res_strict["yearly"],
            },
            "trail_12pct": {
                "label": "移动止盈放宽到峰值回撤 12%",
                "metrics": res_trail12["metrics"], "funnel": res_trail12["funnel"], "yearly": res_trail12["yearly"],
            },
            "no_trail": {
                "label": "取消移动止盈（仅止损+时间出场）",
                "metrics": res_notrail["metrics"], "funnel": res_notrail["funnel"], "yearly": res_notrail["yearly"],
            },
            "v1_rules": {
                "label": "v1 修订前（回撤8%或破20周线 + 63日时间帽）",
                "metrics": res_live["metrics"], "funnel": res_live["funnel"], "yearly": res_live["yearly"],
            },
            "time_189": {
                "label": "时间帽放宽到 189 日（反例：收获期不可过长）",
                "metrics": res_t189["metrics"], "funnel": res_t189["funnel"], "yearly": res_t189["yearly"],
            },
        }

    out = {
        "generated_at": date.today().isoformat(),
        "params": {
            "window": [WIN_START.isoformat(), WIN_END.isoformat()], "slope_min": SLOPE_MIN_STRICT if strict else SLOPE_MIN,
            "strict_trend": strict, "val_pct_max": VAL_PCT_MAX, "stop_loss": STOP_LOSS, "trail": TRAIL,
            "max_hold_days": MAX_HOLD, "slots": SLOTS, "cost_round_trip": COST_SIDE * 2,
            "val_window_days": VAL_WINDOW_DAYS,
            "signal": "①当月金股 ②估值分位≤30% ③收盘>30周线且20周线斜率≥阈值（周五收盘确认，次日收盘买入）",
        },
        "universe": universe,
        **res,
        "sensitivity": sensitivity,
        "disclaimers": [
            "金股名单偏向大市值/高研究覆盖标的（覆盖面偏差）",
            f"样本窗口 {WIN_START}~{WIN_END}（单一市场周期；2021-2022 熊市 / 2024-2025 牛市 / 2026 回调），外推需谨慎",
            "估值分位口径：近 3 年日历窗（1095 自然日）；旧口径“最近 756 个非空观测”已被证明会被数据起点污染，作废",
            "数据缺口：8 只北交所金股在 Tushare daily_basic 无行情，被排除（占候选 0.36%）",
            "定性条件（政策催化/逻辑破坏）未参与回测；随机对照/滚动窗口见 outputs/swing_montecarlo*.json",
            "月调频率约束（每月最多 1 次调仓）未机械执行（month_cap 口径另见随机对照输出）",
            "训练段参考，非实盘口径；不构成投资建议",
        ],
    }
    out_json.parent.mkdir(parents=True, exist_ok=True)
    out_json.write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")

    m = out["metrics"]
    lines = [f"# 波段池策略回测报告（{WIN_START} ~ {WIN_END}）", "",
             f"> 生成：{out['generated_at']} ｜ 机械口径：当月金股 + 估值分位≤30% + 周线趋势 ｜ **v2 出场（2026-10-18 修订）**：-15% 硬止损 / 破 30 周线 / 峰值 20% 灾难线 / 126 交易日收获期 ｜ 5 槽位 / 双边成本 0.2%",
             "> 引擎版本：2026-10-20 —— ①出场/期末清算双重计数修复（2026-10-19）②估值分位改为近 3 年日历窗（原“756 个非空观测”会被数据起点污染）③补齐 18 只此前漏抓候选（含京东方等）、8 只北交所无行情数据排除 ④长期停牌/退市强制清算保护 ⑤周线特征复用 + 随机对照（Monte Carlo）", "",
             "> ⚠️ 参数敏感性提示：时间帽与回撤线在邻域内收益量级摆动明显（路径运气主导），**绝对收益数字不可外推**；可外推的只有期望方向、PF 区间、2026 年表现与集中度（见下节）。2027 年样本外验证中。", "",
             "## 核心指标", "",
             "| 指标 | 策略 | 沪深300 |", "|---|---|---|",
             f"| 总收益 | {m['total_return']*100:.1f}% | " + (f"{m['bench_return']*100:.1f}%" if m.get('bench_return') is not None else "—（基准不可得）") + " |",
             f"| 年化 | {m['cagr']*100:.1f}% | — |",
             f"| 最大回撤 | {m['max_drawdown']*100:.1f}% | — |",
             f"| 夏普 | {m['sharpe']} | — |",
             f"| 交易笔数 | {m['n_trades']} | — |",
             f"| 胜率 | {(m.get('win_rate') or 0)*100:.1f}% | — |",
             f"| 盈亏比(PF) | {m.get('profit_factor')} | — |",
             f"| 平均持有 | {m.get('avg_hold_days')} 日 | — |",
             "| 超额 | " + (f"{m['excess_return']*100:.1f}%" if m.get('excess_return') is not None else "—") + " | — |", "",
             "## 分年", "", "| 年份 | 收益 | 交易 | 胜率 |", "|---|---|---|---|"]
    for y, e0 in out["yearly"].items():
        lines.append(f"| {y} | {e0['ret']*100:.1f}% | {e0['trades']} | {(e0.get('win_rate') or 0)*100:.0f}% |")
    c = out.get("concentration") or {}
    lines += ["", "## 集中度审计（剔除最大盈利单后的收益）", "",
              f"- 全样本总收益：{m['total_return']*100:.1f}% ｜ 剔除 TOP1：{(c.get('ex_top1') or 0)*100:.1f}% ｜ "
              f"剔除 TOP3：{(c.get('ex_top3') or 0)*100:.1f}% ｜ 剔除 TOP5：{(c.get('ex_top5') or 0)*100:.1f}%",
              f"- TOP1 交易：{(c.get('top1') or {}).get('name', '-')}（{(c.get('top1') or {}).get('code', '-')}）"
              f"收益 {(c.get('top1') or {}).get('ret', 0)*100:.1f}%",
              "- **判读**：若剔除 TOP1 后为负，则该策略的正收益依赖单笔极端盈利，不能视为稳定 edge。", "",
              "- **随机对照（3y 窗口）**：见 outputs/swing_montecarlo.json（同择时随机选股/完全随机/随机排序的收益分布与实际值分位）。", ""]
    lines += ["", "## 出场原因分布（主口径）", ""]
    for k, v in (out.get("exit_reasons") or {}).items():
        lines.append(f"- {k}：{v} 笔")
    if out.get("sensitivity"):
        lines += ["", "## 敏感性（诊断用，不构成参数优选）", "",
                  "| 变体 | 总收益 | 年化 | 最大回撤 | 交易 | 胜率 | PF |", "|---|---|---|---|---|---|---|"]
        for key in ("v1_rules", "strict_trend", "trail_12pct", "no_trail", "time_189"):
            ms = out["sensitivity"][key]["metrics"]
            lines.append(f"| {out['sensitivity'][key]['label']} | {ms['total_return']*100:.1f}% | {ms['cagr']*100:.1f}% | "
                         f"{ms['max_drawdown']*100:.1f}% | {ms['n_trades']} | {(ms.get('win_rate') or 0)*100:.1f}% | {ms.get('profit_factor')} |")
    lines += ["", "## 声明", ""]
    for d0 in out["disclaimers"]:
        lines.append(f"- {d0}")
    lines.append("")
    lines.append("本报告由回测脚本自动生成，仅供参考，不构成任何投资建议。")
    md = "\n".join(lines)
    OUT_MD.mkdir(parents=True, exist_ok=True)
    tag = date.today().isoformat().replace("-", "")
    (OUT_MD / f"{tag}_波段池回测报告{out_suffix}.md").write_text(md, encoding="utf-8")
    print(md)


if __name__ == "__main__":
    main()
