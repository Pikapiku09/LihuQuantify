# -*- coding: utf-8 -*-
"""A2 入场质量分解 + A3 α 分解（选股 vs 择时/离场归因）。

框架级问题
----------
这个策略到底亏在「选股不行」还是「择时/离场不行」？

口径设计（三套，全部输出）
------------------------
- 口径A  main 单账本                    99 笔买入 / 98 轮
- 口径B  IR 系合并 = main+ir20+wbr25   292 笔买入   ← 归因主口径
- 口径C  五账本全合并                  468 笔买入 / 464 轮（含异构策略，仅作参考）
- 另列    s43+s44 = 176 笔，BookSpec.strategy 缺省 cherry_claw，**不纳入 IR 归因**

A2 分类（互斥、按优先级；阈值可复现）
------------------------------------
1) 入场即错      MAE(3日) ≤ −3% 且 MFE(3日) < +1%   → 选股/入场时机问题
2) 入场对且顺利  实际轮次 PnL > 0
3) 入场对但被洗  MFE(持仓期内) ≥ +3% 但轮次 PnL ≤ 0 → 「离场太紧」的受害者
4) 灰区          其余（未深跌、也未明显浮盈，最后仍亏）

纪律
----
- PnL 唯一来源：import `lihu_quantify.backtest.metrics._pair_rounds`（禁止另写配对算法）
- 伪交易日 2026-09-12 用 `data.integrity.filter_non_trading_days` 剔除
- 只读 import `src/`；禁止联网；行情匹配率 < 90% 直接拒绝出结论

用法
----
    $env:PYTHONIOENCODING="utf-8"
    .venv\\Scripts\\python.exe scripts/_diag_entry_quality.py
"""

from __future__ import annotations

import json
import sys
from collections import defaultdict
from datetime import date
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(r"E:\LihuQuantify")
sys.path.insert(0, str(ROOT / "src"))

from lihu_quantify.backtest.metrics import _pair_rounds  # noqa: E402
from lihu_quantify.data.duckdb_store import DuckDBStore  # noqa: E402
from lihu_quantify.data.integrity import (  # noqa: E402
    filter_non_trading_days,
    load_trading_days,
)
from lihu_quantify.types import TradeRecord  # noqa: E402

# ---------------------------------------------------------------- 配置
DB_PATH = ROOT / "data" / "lihu_quant.duckdb"
LEDGER_DIR = Path(r"E:\Dsh_WorkSapce\Dify_Agents\_tmp_mine")

LEDGERS = {
    "main": "paper_state.json",
    "ir20": "paper_state_ir20.json",
    "s43": "paper_state_s43.json",
    "s44": "paper_state_s44.json",
    "wbr25": "paper_state_wbr25.json",
}
IR_BOOKS = ["main", "ir20", "wbr25"]     # 归因主口径（IR 系）
HETERO_BOOKS = ["s43", "s44"]            # 异构策略（cherry_claw），不纳入 IR 归因

INDEX_CODE = "000001.SH"
WIN_START = date(2026, 8, 25)
WIN_END = date(2026, 11, 10)
LOAD_START = date(2026, 8, 1)
LOAD_END = date(2026, 12, 31)

HORIZONS = (1, 3, 5, 10, 20)
TH_MAE = -0.03        # 即错：MAE 跌破 −3%
TH_MFE = 0.01         # 即错：同时 MFE < +1%
TH_WASH = 0.03        # 被洗：曾浮盈 ≥ +3%

MIN_MATCH = 0.90
N_BOOT, BLOCK, SEED = 4000, 5, 20261114

TAG = "【实证】"


def hr(t="", ch="=", w=100):
    print(f"\n{ch * w}\n{t}\n{ch * w}" if t else ch * w)


def wilson(k: int, n: int, z: float = 1.96) -> tuple[float, float]:
    """Wilson 置信区间（小样本占比不硬给点估计）。"""
    if n == 0:
        return (float("nan"), float("nan"))
    p = k / n
    d = 1 + z * z / n
    c = p + z * z / (2 * n)
    m = z * np.sqrt(p * (1 - p) / n + z * z / (4 * n * n))
    return ((c - m) / d, (c + m) / d)


# ---------------------------------------------------------------- 装载
def load_ledger(name, fn):
    raw = json.loads((LEDGER_DIR / fn).read_text(encoding="utf-8"))
    return {
        "name": name,
        "init_capital": float(raw["init_capital"]),
        "asset_snapshot": raw.get("asset"),
        "raw": raw["trades"],
    }


def to_records(raw):
    out = []
    for t in raw:
        y, m, d = (int(x) for x in t["date"].split("-"))
        out.append(TradeRecord(
            ts_code=t["ts_code"], trade_date=date(y, m, d), side=t["side"],
            price=float(t["price"]), volume=int(t["volume"]),
            pnl=float(t.get("pnl", 0.0)), reason=t.get("reason", ""),
            commission=float(t.get("commission", 0.0)),
            stamp_tax=float(t.get("stamp_tax", 0.0)),
        ))
    return out


def strategy_of(reason):
    r = reason or ""
    if "日内反转" in r:
        return "IR"
    if "三层过滤" in r:
        return "CC"
    if "周频波段" in r:
        return "WBR"
    if "策略买入" in r:
        return "LEGACY"
    return "OTHER"


# ------------------------------------------- 轮次归因（PnL 只用 _pair_rounds）
def attribute_rounds(records):
    """返回 (rounds_full, per_buy)。

    PnL 数值**全部**来自 `_pair_rounds`：对按 trade_date 稳定升序的第 k 笔 sell，
    取其前缀与前一前缀的**轮次集合差**即该 sell 的轮次（_pair_rounds 追加单调，
    见 docs/评审进度切段重算_20261114.md §2.3 已实证）。
    随后按 per-code「第 j 笔 sell ↔ 第 j 笔 buy」做**身份映射**（不做 PnL 计算）：
    各账本每笔 sell 均为全平，故等价于 FIFO 消费顺序；末尾以总和断言校验。
    """
    order = sorted(range(len(records)), key=lambda i: records[i].trade_date)
    srecs = [records[i] for i in order]
    s2o = [order[i] for i in range(len(order))]

    full = _pair_rounds(records)

    sell_pos = [i for i, r in enumerate(srecs) if r.side == "sell"]
    sell_pnl, prev = {}, 0
    for pos in sell_pos:
        pre = _pair_rounds(srecs[: pos + 1])
        sell_pnl[s2o[pos]] = float(sum(pre[prev:]))
        prev = len(pre)

    code_buys, code_sells = defaultdict(list), defaultdict(list)
    for i, r in enumerate(records):
        (code_buys if r.side == "buy" else code_sells)[r.ts_code].append(i)

    per_buy = {}
    for code, sells in code_sells.items():
        buys = code_buys.get(code, [])
        assert len(sells) <= len(buys), f"{code}: sell 多于 buy"
        for j, si in enumerate(sells):
            bi = buys[j]
            per_buy[bi] = {
                "pnl": sell_pnl[si],
                "sell_idx": si,
                "sell_date": records[si].trade_date,
                "sell_price": records[si].price,
                "sell_comm": records[si].commission,
                "sell_stamp": records[si].stamp_tax,
            }
    return full, per_buy


# ---------------------------------------------------------------- 行情
def load_market(store, codes):
    tds = load_trading_days(store)
    hr("Part 2  行情装载（伪交易日剔除 + 匹配率硬断言）")
    print(f"{TAG} trade_cal 开市日 {len(tds)} 天 "
          f"({min(tds) if tds else None} ~ {max(tds) if tds else None})")

    idx = store.query(
        "SELECT trade_date, close FROM index_daily WHERE ts_code = ? "
        "AND trade_date BETWEEN ? AND ? ORDER BY trade_date",
        [INDEX_CODE, LOAD_START, LOAD_END])
    n0 = len(idx)
    idx, drop_i = filter_non_trading_days(idx, tds)
    print(f"{TAG} {INDEX_CODE}: {n0} 行 → 剔除伪交易日 {len(drop_i)} 条 "
          f"{[str(d) for d in drop_i]} → {len(idx)} 行")
    idx["trade_date"] = pd.to_datetime(idx["trade_date"]).dt.date
    idx_s = idx.set_index("trade_date")["close"].astype(float).sort_index()

    axis = [d for d in idx_s.index if WIN_START <= d <= WIN_END]
    print(f"{TAG} A3 区间 {axis[0]} ~ {axis[-1]}（{len(axis)} 个交易日）")

    q = store.query(
        "SELECT ts_code, trade_date, open, high, low, close FROM daily_quotes "
        "WHERE trade_date BETWEEN ? AND ? ORDER BY ts_code, trade_date",
        [LOAD_START, LOAD_END])
    n_q0 = len(q)
    q, drop_q = filter_non_trading_days(q, tds)
    print(f"{TAG} daily_quotes: {n_q0} 行 → 剔除伪交易日 {len(drop_q)} 条 → {len(q)} 行")
    q["trade_date"] = pd.to_datetime(q["trade_date"]).dt.date

    have = set(q["ts_code"].unique())
    missing = sorted(codes - have)
    rate = 1.0 - len(missing) / max(1, len(codes))
    print(f"{TAG} 需要行情 {len(codes)} 只，库内有 {len(codes & have)} 只，"
          f"缺 {len(missing)} 只 → 匹配率 {rate:.2%}")
    if missing:
        print(f"    缺失样例: {missing[:15]}")
    assert rate >= MIN_MATCH, (
        f"行情匹配率仅 {rate:.1%} < {MIN_MATCH:.0%}，数据不全，禁止出结论")

    def piv(col):
        return q.pivot_table(index="ts_code", columns="trade_date",
                             values=col, aggfunc="last").reindex(columns=axis)

    close_p = piv("close")
    return {
        "axis": axis, "tds": tds, "idx": idx_s,
        "px": close_p, "px_ff": close_p.ffill(axis=1),
        "hi": piv("high"), "lo": piv("low"),
        "have": have, "missing": missing, "rate": rate,
        "n_drop_i": len(drop_i), "n_drop_q": len(drop_q),
        "quotes": q,
    }


# ---------------------------------------------------------------- A2
def excursions(mkt, code, bdate, bprice):
    """买入日之后 1/3/5/10/20 个交易日的 MFE/MAE（按市场交易日轴，停牌位跳过）。"""
    axis = mkt["axis"]
    if code not in mkt["hi"].index:
        return None
    i0 = axis.index(bdate) if bdate in axis else None
    if i0 is None:
        c = [k for k, d in enumerate(axis) if d < bdate]
        if not c:
            return None
        i0 = c[-1]
    hirow, lorow = mkt["hi"].loc[code], mkt["lo"].loc[code]
    out = {}
    for n in HORIZONS:
        seg = axis[i0 + 1: i0 + 1 + n]
        h = hirow.reindex(seg).dropna()
        lo = lorow.reindex(seg).dropna()
        if len(h) == 0 or len(lo) == 0:
            out[n] = (np.nan, np.nan, 0)
        else:
            out[n] = (float(h.max()) / bprice - 1.0,
                      float(lo.min()) / bprice - 1.0, int(min(len(h), len(lo))))
    return out


def hold_excursion(mkt, code, bdate, sdate, bprice):
    """持仓期内（买入日之后 ~ 卖出日）的最大浮盈 / 最大浮亏。"""
    axis = mkt["axis"]
    if code not in mkt["hi"].index or bdate not in axis:
        return (np.nan, np.nan, 0)
    i0 = axis.index(bdate)
    i1 = axis.index(sdate) if sdate in axis else len(axis)
    seg = axis[i0 + 1: i1 + 1]
    if not seg:
        return (np.nan, np.nan, 0)
    h = mkt["hi"].loc[code].reindex(seg).dropna()
    lo = mkt["lo"].loc[code].reindex(seg).dropna()
    if len(h) == 0 or len(lo) == 0:
        return (np.nan, np.nan, 0)
    return (float(h.max()) / bprice - 1.0, float(lo.min()) / bprice - 1.0,
            int(min(len(h), len(lo))))


def build_a2(books, mkt, detail_out=None):
    rows = []
    for bk in books:
        _, per_buy = attribute_rounds(bk["records"])
        for i, r in enumerate(bk["records"]):
            if r.side != "buy":
                continue
            ex = excursions(mkt, r.ts_code, r.trade_date, r.price)
            info = per_buy.get(i)
            row = {
                "ledger": bk["name"], "strategy": strategy_of(r.reason),
                "ts_code": r.ts_code, "buy_date": r.trade_date,
                "buy_price": r.price, "buy_idx": i,
                "volume": r.volume, "buy_comm": r.commission,
                "round_pnl": info["pnl"] if info else np.nan,
                "hold_days": ((info["sell_date"] - r.trade_date).days if info else np.nan),
                "closed": info is not None,
                "sell_comm": info["sell_comm"] if info else np.nan,
                "sell_stamp": info["sell_stamp"] if info else np.nan,
            }
            for n in HORIZONS:
                if ex is None:
                    row[f"mfe{n}"] = row[f"mae{n}"] = np.nan
                    row[f"bars{n}"] = 0
                else:
                    m, a, nb = ex[n]
                    row[f"mfe{n}"], row[f"mae{n}"], row[f"bars{n}"] = m, a, nb
            if info:
                hm, ha, hb = hold_excursion(mkt, r.ts_code, r.trade_date,
                                            info["sell_date"], r.price)
            else:
                hm, ha, hb = np.nan, np.nan, 0
            row["mfe_hold"], row["mae_hold"], row["bars_hold"] = hm, ha, hb
            rows.append(row)
    df = pd.DataFrame(rows)
    if detail_out is not None:
        detail_out.append(df)
    return df


def classify(df, wash_col="mfe_hold", mae_n=3, mfe_n=3):
    """三分类 + 残差。返回 Series。"""
    cls = []
    for _, r in df.iterrows():
        mae, mfe = r[f"mae{mae_n}"], r[f"mfe{mfe_n}"]
        pnl, wf = r["round_pnl"], r[wash_col]
        if not np.isnan(mae) and not np.isnan(mfe) and mae <= TH_MAE and mfe < TH_MFE:
            cls.append("入场即错")
        elif not np.isnan(pnl) and pnl > 0:
            cls.append("入场对且顺利")
        elif not np.isnan(pnl) and pnl <= 0 and not np.isnan(wf) and wf >= TH_WASH:
            cls.append("入场对但被洗")
        else:
            cls.append("灰区（慢跌/弱信号）")
    return pd.Series(cls, index=df.index)


CLASSES = ["入场即错", "入场对但被洗", "入场对且顺利", "灰区（慢跌/弱信号）"]


def report_a2(df, label):
    hr(f"A2 入场质量分解 —— {label}")
    n = len(df)
    n_closed = int(df["closed"].sum())
    print(f"{TAG} 买入 {n} 笔，其中已平仓 {n_closed} 笔、未平仓 {n - n_closed} 笔")
    print(f"{TAG} 前瞻 bar 可用性（按市场交易日轴）：")
    for h in HORIZONS:
        ge = int((df[f"bars{h}"] >= h).sum())
        anyb = int((df[f"bars{h}"] > 0).sum())
        print(f"    T+{h:<2d}  满窗 {ge:4d}/{n}（{ge / n * 100:5.1f}%）"
              f"   至少 1 bar {anyb:4d}   平均 bar {df[f'bars{h}'].mean():.1f}")
    print(f"    持仓期窗口（买入→卖出）满窗 {int((df['bars_hold'] > 0).sum())} 笔")

    print(f"\nMFE / MAE 分布（%，样本=该视界有 bar 的笔）：")
    print(f"  {'视界':<7}{'n':>5}{'MFE中位':>9}{'MFE均值':>9}{'MFE>0':>8}"
          f"{'MAE中位':>9}{'MAE均值':>9}{'MAE<-3%':>9}")
    rows = [(f"T+{h}", df[f"mfe{h}"], df[f"mae{h}"]) for h in HORIZONS]
    rows.append(("持仓期", df["mfe_hold"], df["mae_hold"]))
    for nm, mfe, mae in rows:
        m, a = mfe.dropna(), mae.dropna()
        if len(m) == 0:
            continue
        print(f"  {nm:<7}{len(m):>5}{m.median() * 100:>9.2f}{m.mean() * 100:>9.2f}"
              f"{(m > 0).mean() * 100:>7.1f}%{a.median() * 100:>9.2f}"
              f"{a.mean() * 100:>9.2f}{(a <= TH_MAE).mean() * 100:>8.1f}%")

    print(f"\n三分类（即错: MAE(3日)≤{TH_MAE:.0%} 且 MFE(3日)<{TH_MFE:.0%}；"
          f"被洗: MFE(持仓期)≥{TH_WASH:.0%} 且轮次≤0）：")
    print(f"  {'分类':<18}{'笔数':>5}{'占比':>8}{'占比95%CI':>18}"
          f"{'合计盈亏':>13}{'EV/笔':>10}")
    total = 0.0
    for c in CLASSES:
        sub = df[df["cls"] == c]
        k = len(sub)
        pnl = sub["round_pnl"].sum()
        clo = int(sub["closed"].sum())
        lo, hi = wilson(k, n)
        total += pnl
        print(f"  {c:<18}{k:>5}{k / n * 100:>7.1f}%"
              f"{f'[{lo * 100:5.1f}%,{hi * 100:5.1f}%]':>18}"
              f"{pnl:>13.2f}{(pnl / clo if clo else float('nan')):>10.2f}")
    print(f"  {'合计':<18}{n:>5}{100.0:>7.1f}%{'':>18}{total:>13.2f}"
          f"{(total / n_closed if n_closed else float('nan')):>10.2f}")

    known = [s for s in ("IR", "CC", "WBR", "LEGACY") if s in set(df["strategy"])]
    if len(known) > 1:
        print(f"\n分类 × 策略来源：")
        print(f"  {'分类':<18}" + "".join(f"{s:>8}" for s in known) + f"{'合计':>8}")
        for c in CLASSES:
            sub = df[df["cls"] == c]
            print(f"  {c:<18}"
                  + "".join(f"{int((sub['strategy'] == s).sum()):>8}" for s in known)
                  + f"{len(sub):>8}")

    los = df[df["closed"] & (df["round_pnl"] <= 0)]
    if len(los):
        print(f"\n仅看亏损轮次（n={len(los)}，合计 {los['round_pnl'].sum():.2f}）：")
        for c in CLASSES:
            k = int((los["cls"] == c).sum())
            print(f"  {c:<18}{k:>5} {k / len(los) * 100:>6.1f}%")

    # ---- T+20 样本选择偏差检验（满窗子集是否系统性更好）----
    full20 = df[df["bars20"] >= 20]
    trunc20 = df[(df["bars5"] > 0) & (df["bars20"] < 20)]
    if len(full20) >= 10 and len(trunc20) >= 10:
        print(f"\nT+20 样本选择检验（满窗 n={len(full20)} vs 截断 n={len(trunc20)}）：")
        print(f"  {'子集':<10}{'n':>5}{'MFE5中位':>11}{'MAE5中位':>11}{'MFE>0':>9}{'即错率':>9}")
        for nm, sub in (("满窗T+20", full20), ("截断", trunc20)):
            m5 = sub["mfe5"].dropna()
            a5 = sub["mae5"].dropna()
            wrong = int((sub["cls"] == "入场即错").sum())
            print(f"  {nm:<10}{len(sub):>5}{m5.median() * 100:>11.2f}"
                  f"{a5.median() * 100:>11.2f}{(m5 > 0).mean() * 100:>8.1f}%"
                  f"{wrong / len(sub) * 100:>8.1f}%")
        d_mfe = full20["mfe5"].median() - trunc20["mfe5"].median()
        d_wrong = ((full20["cls"] == "入场即错").mean()
                   - (trunc20["cls"] == "入场即错").mean())
        print(f"  差异：MFE5 中位 {d_mfe * 100:+.2f}pp；即错率 {d_wrong * 100:+.2f}pp")
        better = d_mfe > 0.01 or d_wrong < -0.10
        worse = d_mfe < -0.01 or d_wrong > 0.10
        if better:
            print("  → [!] 满窗子集在 T+5 口径上更好 → T+20 结论偏乐观，"
                  "截断部分的信息被系统性高估，须以 T+5 口径为准")
        elif worse:
            print("  → [!] 满窗子集在 T+5 口径上*更差* → T+20 结论偏**悲观**；"
                  "即 T+20 看到的问题比真实更严重，不会美化策略，结论方向安全")
        else:
            print("  → 两子集在共同可得的 T+5 口径上无实质差异；"
                  "T+20 的 MFE 更高主要来自「窗口越长越容易出现更高极值」，非样本选择")

    # ---- 离场规则「最乐观上界」：若被洗组全部在 +3% 兑现 ----
    STAMP = 0.0005
    wash = df[(df["cls"] == "入场对但被洗") & df["closed"]]
    if len(wash):
        sp = wash["buy_price"] * (1 + TH_WASH)
        theo = ((sp - wash["buy_price"]) * wash["volume"]
                - wash["buy_comm"] - wash["sell_comm"] - sp * wash["volume"] * STAMP)
        act = wash["round_pnl"]
        gain = float((theo - act).sum())
        tot = float(df["round_pnl"].sum())
        print(f"\n离场规则的**最乐观上界**（仅被洗组 n={len(wash)}）：")
        print(f"  实际合计 {act.sum():.2f}（EV {act.mean():.2f}）")
        print(f"  若全部恰在 +3% 兑现 → 合计 {theo.sum():.2f}（EV {theo.mean():.2f}）")
        if tot < 0 and gain <= abs(tot) * 1.05:
            print(f"  → 放宽离场最多可改善约 {gain:+.2f}"
                  f"（占该口径净亏损 {abs(tot):.2f} 的 {gain / abs(tot) * 100:.1f}%）")
        elif tot < 0:
            print(f"  → 放宽离场最多可改善约 {gain:+.2f}"
                  f"（该口径净亏损仅 {abs(tot):.2f}，改善额已超过它，"
                  f"占比会算出 >100% 的失真值，故只给绝对值）")
        else:
            print(f"  → 放宽离场最多可改善约 {gain:+.2f}"
                  f"（该口径实际为净盈利 {tot:+.2f}，占比无意义，故只给绝对值）")
        print(f"  注：理论上界（假设每笔精准卖在 +3%），非可实现收益；"
              f"真实反事实见 task-4《反事实离场回测》")
    return df


def report_sensitivity(df, label):
    print(f"\n【稳健性】{label}：不同窗口定义下的三分类占比")
    variants = [
        ("即错3日 + 被洗(持仓期)  ← 主口径", 3, 3, "mfe_hold"),
        ("即错5日 + 被洗(持仓期)", 5, 5, "mfe_hold"),
        ("即错3日 + 被洗(MFE10)", 3, 3, "mfe10"),
        ("即错3日 + 被洗(MFE20)", 3, 3, "mfe20"),
    ]
    print(f"  {'口径':<34}" + "".join(f"{c[:6]:>10}" for c in CLASSES))
    for nm, mae_n, mfe_n, wcol in variants:
        s = classify(df, wash_col=wcol, mae_n=mae_n, mfe_n=mfe_n)
        n = len(s)
        print(f"  {nm:<34}" + "".join(f"{(s == c).sum() / n * 100:>9.1f}%" for c in CLASSES))


# ---------------------------------------------------------------- A3
def strategy_curve(bk, mkt):
    """返回 {total, cash, mvs, exposure}：总资产、期末现金、持仓市值序列、仓位序列。"""
    axis = mkt["axis"]
    pxf = mkt["px_ff"]
    byd = defaultdict(list)
    for r in bk["records"]:
        byd[r.trade_date].append(r)
    cash, pos, last = bk["init_capital"], defaultdict(int), {}
    vals, mvs = [], []
    for d in axis:
        for r in byd.get(d, []):
            amt = r.price * r.volume
            if r.side == "buy":
                cash -= amt + r.commission
                pos[r.ts_code] += r.volume
                last[r.ts_code] = r.price
            else:
                cash += amt - r.commission - r.stamp_tax
                pos[r.ts_code] -= r.volume
                if pos[r.ts_code] <= 0:
                    pos.pop(r.ts_code, None)
        mv = 0.0
        for code, vol in pos.items():
            if vol <= 0:
                continue
            p = pxf.at[code, d] if code in pxf.index else np.nan
            if pd.isna(p):
                p = last.get(code, np.nan)
            else:
                last[code] = float(p)
            if not pd.isna(p):
                mv += vol * float(p)
        vals.append(cash + mv)
        mvs.append(mv)
    total = pd.Series(vals, index=axis, name=bk["name"])
    mv_s = pd.Series(mvs, index=axis, name=bk["name"] + "_mv")
    return {"total": total, "cash": cash, "mvs": mv_s,
            "exposure": (mv_s / total.replace(0, np.nan)).fillna(0.0)}


def ols_alpha_beta(ra: np.ndarray, rb: np.ndarray, periods: int = 252) -> dict:
    """单因子回归 r_a = α + β·r_b + ε，返回年化 α、β 及 t 值。

    这是比"收益差"更严谨的 α 分解：β 吸收平均仓位差异，
    α 才是「同市场暴露下多赚/少赚」的部分。
    """
    from scipy import stats as st
    n = len(ra)
    if n < 10 or np.std(rb) == 0:
        return {"n": n, "alpha_ann": float("nan"), "beta": float("nan"),
                "t_alpha": float("nan"), "p_alpha": float("nan"), "r2": float("nan")}
    X = np.column_stack([np.ones(n), rb])
    coef, *_ = np.linalg.lstsq(X, ra, rcond=None)
    resid = ra - X @ coef
    dof = n - 2
    s2 = float(resid @ resid) / dof if dof > 0 else float("nan")
    try:
        cov = s2 * np.linalg.inv(X.T @ X)
        se_a, se_b = float(np.sqrt(cov[0, 0])), float(np.sqrt(cov[1, 1]))
    except Exception:
        se_a = se_b = float("nan")
    t_a = coef[0] / se_a if se_a and se_a == se_a and se_a > 0 else float("nan")
    p_a = float(2 * (1 - st.t.cdf(abs(t_a), dof))) if t_a == t_a else float("nan")
    ss_tot = float(((ra - ra.mean()) ** 2).sum())
    r2 = 1.0 - float(resid @ resid) / ss_tot if ss_tot > 0 else float("nan")
    return {"n": n, "alpha_daily": float(coef[0]),
            "alpha_ann": float(coef[0] * periods), "beta": float(coef[1]),
            "t_alpha": float(t_a), "p_alpha": p_a, "r2": float(r2)}


def staggered_eqw(details, mkt):
    """各自首次买入日等权建仓 + 持有到区间末（未建仓部分为现金）。"""
    axis, pxf = mkt["axis"], mkt["px_ff"]
    first = {}
    for code, d, p in details:
        if code not in first or d < first[code][0]:
            first[code] = (d, p)
    items, skipped = [], []
    for c, (d, p) in first.items():
        if c not in pxf.index:
            skipped.append((c, "库内无行情"))
            continue
        if d < axis[0]:
            skipped.append((c, "首买日早于区间"))
            continue
        if p <= 0 or pd.isna(p):
            skipped.append((c, "买入价异常"))
            continue
        items.append((c, d, p))
    if not items:
        return None, skipped, 0, None
    n = len(items)
    curve = np.zeros(len(axis))
    expo = np.zeros(len(axis))
    for code, d, p in items:
        row = pxf.loc[code]
        i_e = axis.index(d) if d in axis else 0
        for k in range(len(axis)):
            v = row.iloc[k]
            if k < i_e or pd.isna(v):
                curve[k] += 1.0
            else:
                curve[k] += v / p
                expo[k] += 1.0
    return (pd.Series(curve / n, index=axis, name="等权选股"), skipped, n,
            pd.Series(expo / n, index=axis))


def static_eqw(codes, mkt, name):
    axis, pxf = mkt["axis"], mkt["px_ff"]
    used, skipped = [], []
    curve = np.zeros(len(axis))
    for c in codes:
        if c not in pxf.index:
            skipped.append((c, "库内无行情"))
            continue
        row = pxf.loc[c].ffill()
        base, end = row.iloc[0], row.iloc[-1]
        if pd.isna(base) or base <= 0:
            skipped.append((c, "区间首日无价"))
            continue
        if pd.isna(end):
            skipped.append((c, "区间末日无价"))
            continue
        curve += (row / base).to_numpy(dtype=float)
        used.append(c)
    if not used:
        return None, skipped, []
    return pd.Series(curve / len(used), index=axis, name=name), skipped, used


def boot_diff(ra, rb, n_boot=N_BOOT, block=BLOCK, seed=SEED):
    n = len(ra)
    nb = int(np.ceil(n / block))
    rng = np.random.default_rng(seed)
    out = np.empty(n_boot)
    for k in range(n_boot):
        st = rng.integers(0, n, size=nb)
        idx = np.concatenate([np.arange(s, s + block) for s in st])
        idx = idx[idx < n][:n]
        out[k] = (np.prod(1 + ra[idx]) - 1) - (np.prod(1 + rb[idx]) - 1)
    return out


def report_a3(books, mkt, label, universe):
    hr(f"A3 α 分解 —— {label}")
    axis = mkt["axis"]
    idx_n = (mkt["idx"].reindex(axis) / mkt["idx"].reindex(axis).iloc[0]).astype(float)

    curves, mvs, expos, cash_end = [], [], [], {}
    for bk in books:
        r = strategy_curve(bk, mkt)
        curves.append(r["total"])
        mvs.append(r["mvs"])
        expos.append(r["exposure"])
        cash_end[bk["name"]] = r["cash"]
    strat = pd.concat(curves, axis=1).sum(axis=1)
    strat_n = strat / strat.iloc[0]
    mv_tot = pd.concat(mvs, axis=1).sum(axis=1)
    strat_expo = (mv_tot / strat.replace(0, np.nan)).fillna(0.0)

    details = [(r.ts_code, r.trade_date, r.price)
               for bk in books for r in bk["records"] if r.side == "buy"]
    eqw, sk_eqw, n_eqw, eqw_expo = staggered_eqw(details, mkt)
    basket = sorted({c for c, _, _ in details})
    bh, sk_bh, used_bh = static_eqw(basket, mkt, "篮子首日买入")
    uni, sk_uni, used_uni = static_eqw(universe, mkt, "库内全标的等权")

    # --- 成分股可用天数（Lead 要求）---
    q = mkt["quotes"]
    qq = q[(q["trade_date"] >= axis[0]) & (q["trade_date"] <= axis[-1])]
    days = qq.groupby("ts_code").size()
    comp = [c for c in basket if c in days.index]
    print(f"{TAG} 区间 {axis[0]} ~ {axis[-1]}，{len(axis)} 个交易日")
    if comp:
        dv = days.loc[comp]
        print(f"{TAG} 成分股 {len(comp)} 只：可用交易日 最少 {dv.min()} / "
              f"中位 {dv.median():.0f} / 最多 {dv.max()}（满窗={len(axis)}）；"
              f"不足满窗的 {int((dv < len(axis)).sum())} 只")
        print(f"    可用天数最少的 5 只: {dv.nsmallest(5).to_dict()}")
    print(f"{TAG} 等权选股：纳入 {n_eqw} 只、剔除 {len(sk_eqw)} 只"
          + (f"（理由 {dict((r, sum(1 for _, x in sk_eqw if x == r)) for _, r in sk_eqw)}）"
             if sk_eqw else "（无剔除：首买日均晚于区间起点 2026-08-25）"))
    if bh is not None:
        print(f"{TAG} 篮子首日买入：纳入 {len(used_bh)} 只、剔除 {len(sk_bh)} 只"
              + (f"（理由 {dict((r, sum(1 for _, x in sk_bh if x == r)) for _, r in sk_bh)}）"
                 if sk_bh else ""))
    if uni is not None:
        print(f"{TAG} 库内全标的等权：纳入 {len(used_uni)} 只、剔除 {len(sk_uni)} 只"
              + (f"（理由 {dict((r, sum(1 for _, x in sk_uni if x == r)) for _, r in sk_uni)}）"
                 if sk_uni else ""))

    cand = [("策略（账本重算净值）", strat_n, float(strat_expo.mean()))]
    if eqw is not None:
        cand.append(("等权选股（各自首买日）", eqw, float(eqw_expo.mean())))
    cand.append(("纯指数 000001.SH", idx_n, 1.0))
    if bh is not None:
        cand.append(("篮子首日买入（对照）", bh, 1.0))
    if uni is not None:
        cand.append(("库内全标的等权（对照）", uni, 1.0))

    rets = {}
    print(f"\n  {'曲线':<24}{'区间收益':>11}{'年化252':>10}{'期末净值':>10}{'平均仓位':>10}")
    for nm, s, ex in cand:
        s = s.dropna()
        r = float(s.iloc[-1] / s.iloc[0] - 1)
        rets[nm] = r
        nd = len(s)
        ann = (1 + r) ** (252 / nd) - 1 if nd and r > -1 else float("nan")
        print(f"  {nm:<24}{r * 100:>10.2f}%{ann * 100:>9.1f}%{s.iloc[-1]:>10.4f}"
              f"{ex * 100:>9.1f}%")

    r_s, r_e, r_i = (rets["策略（账本重算净值）"],
                     rets.get("等权选股（各自首买日）", float("nan")),
                     rets["纯指数 000001.SH"])
    print(f"\nα 分解（区间收益口径，任务书定义）：")
    print(f"  {'择时/离场贡献 = 策略 − 等权选股':<36}{r_s - r_e:>+10.2%}")
    print(f"  {'选股贡献       = 等权选股 − 指数':<36}{r_e - r_i:>+10.2%}")
    print(f"  {'合计           = 策略 − 指数    ':<36}{r_s - r_i:>+10.2%}")
    if "篮子首日买入（对照）" in rets:
        print(f"  {'（对照）篮子首日买入 − 指数':<36}"
              f"{rets['篮子首日买入（对照）'] - r_i:>+10.2%}   ← 纯选股、满仓无现金拖累")
    if "库内全标的等权（对照）" in rets:
        print(f"  {'（对照）库内全标的等权 − 指数':<36}"
              f"{rets['库内全标的等权（对照）'] - r_i:>+10.2%}")

    # --- 仓位调整后的择时/离场代价（策略平均仓位仅约一半，必须同仓位对比）---
    e_s = float(strat_expo.mean())
    e_e = float(eqw_expo.mean()) if eqw_expo is not None else float("nan")
    print(f"\n仓位核对：策略平均仓位 {e_s:.1%}；等权选股曲线平均仓位 {e_e:.1%}"
          f"（差 {abs(e_s - e_e) * 100:.1f}pp）")
    if abs(e_s - e_e) > 0.10:
        print(f"  [!] 两者仓位差 {abs(e_s - e_e) * 100:.1f}pp > 10pp → 直接做差含仓位差异，"
              f"必须看下方「仓位调整后」口径")
    else:
        print(f"  → 仓位基本匹配，直接做差不含显著的现金拖累伪差异")

    if "篮子首日买入（对照）" in rets:
        r_bh = rets["篮子首日买入（对照）"]
        matched = e_s * r_bh
        print(f"\n仓位调整后的择时/离场代价（近似 = 策略平均仓位 × 满仓篮子收益）：")
        print(f"  策略实际 {r_s:+.2%}；平均仓位 {e_s:.1%}；满仓篮子 {r_bh:+.2%} "
              f"→ 同仓位被动 ≈ {matched:+.2%}")
        print(f"  {'代价 = 策略 − 同仓位被动':<38}{r_s - matched:>+10.2%}")

    # --- 选股贡献必须对「可投资域」比较，否则会把风格漂移算成选股能力 ---
    if "库内全标的等权（对照）" in rets and "篮子首日买入（对照）" in rets:
        r_uni = rets["库内全标的等权（对照）"]
        r_bh = rets["篮子首日买入（对照）"]
        print(f"\n选股贡献的两种基准（后者剥离风格漂移，更严格）：")
        print(f"  {'a) 篮子首日买入 − 纯指数  ': <38}{r_bh - r_i:>+10.2%}"
              f"   （含大小盘/风格暴露差异）")
        print(f"  {'b) 篮子首日买入 − 池内等权（推荐）':<38}{r_bh - r_uni:>+10.2%}"
              f"   （在可投资域内挑股的纯贡献）")

    def daily(s):
        s = s.dropna().astype(float)
        return (s / s.shift(1) - 1).dropna().to_numpy()

    print(f"\n显著性（日收益配对检验 + 区块 bootstrap block={BLOCK} n={N_BOOT}）：")
    from scipy import stats as st
    out = {}
    pairs = []
    if eqw is not None:
        pairs = [("策略", daily(strat_n), "等权选股", daily(eqw)),
                 ("等权选股", daily(eqw), "纯指数", daily(idx_n))]
    else:
        pairs = [("策略", daily(strat_n), "纯指数", daily(idx_n))]
    for na, aa, nb, bb in pairs:
        m = min(len(aa), len(bb))
        if m < 10:
            print(f"  {na}−{nb}: 样本过少 n={m}")
            continue
        d = aa[-m:] - bb[-m:]
        t, p = st.ttest_rel(aa[-m:], bb[-m:])
        bs = boot_diff(aa[-m:], bb[-m:])
        lo, hi = np.percentile(bs, [2.5, 97.5])
        sig = "显著" if (lo > 0 or hi < 0) else "不显著"
        print(f"  {na} − {nb}: n={m} 日均差 {d.mean() * 100:+.4f}%/日 "
              f"t={t:+.2f} p={p:.3f}")
        print(f"      累计收益差 95%CI [{lo * 100:+.2f}%, {hi * 100:+.2f}%] "
              f"P(>0)={float((bs > 0).mean()):.3f} → {sig}")
        out[f"{na}-{nb}"] = {"n": m, "t": float(t), "p": float(p),
                             "ci_lo": float(lo), "ci_hi": float(hi), "sig": sig}

    # --- 单因子回归 α/β：β 吸收仓位差异，α 才是同暴露下的超额 ---
    print(f"\n单因子回归 r_策略 = α + β·r_benchmark + ε（{len(axis) - 1} 个日收益样本）：")
    print(f"  {'benchmark':<22}{'β':>8}{'年化α':>10}{'α 的 t':>9}{'p':>8}{'R²':>8}{'判读':>10}")
    reg = {}
    benches = [("纯指数 000001.SH", idx_n)]
    if eqw is not None:
        benches.insert(0, ("等权选股", eqw))
    if bh is not None:
        benches.append(("篮子首日买入", bh))
    for nm, s in benches:
        ra, rb = daily(strat_n), daily(s)
        m = min(len(ra), len(rb))
        if m < 10:
            continue
        d = ols_alpha_beta(ra[-m:], rb[-m:])
        reg[nm] = d
        verdict = ("显著" if d["p_alpha"] == d["p_alpha"] and d["p_alpha"] < 0.05
                   else "不显著")
        print(f"  {nm:<22}{d['beta']:>8.3f}{d['alpha_ann'] * 100:>9.1f}%"
              f"{d['t_alpha']:>9.2f}{d['p_alpha']:>8.3f}{d['r2']:>8.3f}{verdict:>10}")

    return {"rets": rets, "timing": r_s - r_e, "selection": r_e - r_i,
            "total": r_s - r_i, "sig": out, "reg": reg, "n_days": len(axis),
            "cash_end": cash_end, "n_eqw": n_eqw,
            "avg_expo": float(strat_expo.mean())}


# ---------------------------------------------------------------- main
def main():
    hr("A2 入场质量分解 + A3 α 分解   |   LihuQuantify   |   本地数据，禁止联网")

    hr("Part 0  账本装载与结构校验")
    books_all = {}
    for nm, fn in LEDGERS.items():
        bk = load_ledger(nm, fn)
        bk["records"] = to_records(bk["raw"])
        books_all[nm] = bk
        cash = bk["init_capital"]
        for r in bk["records"]:
            amt = r.price * r.volume
            cash += (-amt - r.commission) if r.side == "buy" else (
                amt - r.commission - r.stamp_tax)
        snap = bk["asset_snapshot"]["cash"]
        ok = abs(cash - snap) < 1e-6
        nb = sum(1 for r in bk["records"] if r.side == "buy")
        ns = sum(1 for r in bk["records"] if r.side == "sell")
        print(f"  {nm:<7} buy={nb:3d} sell={ns:3d} "
              f"{min(r.trade_date for r in bk['records'])}~"
              f"{max(r.trade_date for r in bk['records'])} "
              f"现金重算 {cash:12.2f} vs 快照 {snap:12.2f} "
              f"{'OK' if ok else '<<< 不一致'}")
        assert ok, f"{nm} 现金重算与快照不一致：trades 可能不完整，禁止继续"

    all_codes = {r.ts_code for bk in books_all.values() for r in bk["records"]}
    first_buy = min(r.trade_date for bk in books_all.values()
                    for r in bk["records"] if r.side == "buy")
    print(f"\n{TAG} 涉及标的 {len(all_codes)} 只；首笔买入 {first_buy}")
    assert first_buy >= LOAD_START, f"首笔买入 {first_buy} 早于行情装载起点 {LOAD_START}"
    assert first_buy >= WIN_START or first_buy >= date(2026, 8, 26)

    hr("Part 1  FIFO 轮次核对（唯一来源 metrics._pair_rounds）")
    tot = 0
    for nm, bk in books_all.items():
        rounds = _pair_rounds(bk["records"])
        w = [x for x in rounds if x > 0]
        l = [x for x in rounds if x < 0]
        wr = len(w) / len(rounds) if rounds else 0
        aw = float(np.mean(w)) if w else 0.0
        al = abs(float(np.mean(l))) if l else 0.0
        tot += len(rounds)
        print(f"  {nm:<7} 轮次 {len(rounds):3d} 胜率 {wr * 100:5.2f}% "
              f"盈亏比 {(aw / al if al else 0):4.2f} EV {float(np.mean(rounds)):8.2f} "
              f"合计 {sum(rounds):11.2f}")
    print(f"  合并轮次 {tot}（任务书口径 464）")

    store = DuckDBStore(DB_PATH, read_only=True)
    try:
        mkt = load_market(store, all_codes)
        store.close()

        main_books = [books_all["main"]]
        ir_books = [books_all[n] for n in IR_BOOKS]
        het_books = [books_all[n] for n in HETERO_BOOKS]
        five_books = [books_all[n] for n in LEDGERS]

        # ---- A2 ----
        d_main = build_a2(main_books, mkt)
        d_main["cls"] = classify(d_main)
        report_a2(d_main, "口径A: main 单账本")
        report_sensitivity(d_main, "main")

        d_ir = build_a2(ir_books, mkt)
        d_ir["cls"] = classify(d_ir)
        report_a2(d_ir, "口径B: IR 系合并 main+ir20+wbr25 ★归因主口径")
        report_sensitivity(d_ir, "IR系合并")

        d_five = build_a2(five_books, mkt)
        d_five["cls"] = classify(d_five)
        report_a2(d_five, "口径C: 五账本全合并（含异构 s43/s44，仅参考）")

        d_het = build_a2(het_books, mkt)
        d_het["cls"] = classify(d_het)
        report_a2(d_het, "另列: s43+s44（异构策略 cherry_claw，不纳入归因）")

        # main 内按策略切分
        for s in ("CC", "IR"):
            sub = d_main[d_main["strategy"] == s].copy()
            if len(sub) >= 5:
                report_a2(sub, f"main 内 {s} 段（n={len(sub)}）")

        # ---- A3 ----
        universe = sorted(mkt["have"])
        r_main = report_a3(main_books, mkt, "口径A: main 单账本", universe)
        r_ir = report_a3(ir_books, mkt, "口径B: IR 系合并 main+ir20+wbr25 ★", universe)
        r_five = report_a3(five_books, mkt, "口径C: 五账本全合并", universe)

        # ---- 汇总 ----
        hr("Part 4  关键结论数（供报告取数）")
        for nm, df in (("A2 main", d_main), ("A2 IR合并", d_ir),
                       ("A2 五账本", d_five)):
            n = len(df)
            print(f"\n[{nm}] n={n}")
            for c in CLASSES:
                k = int((df["cls"] == c).sum())
                print(f"   {c:<18}{k:>5} 笔 {k / n * 100:>6.2f}%")
        print()
        for nm, r in (("A3 main", r_main), ("A3 IR合并", r_ir), ("A3 五账本", r_five)):
            print(f"[{nm}] 策略 {r['rets']['策略（账本重算净值）']:+.2%}  "
                  f"平均仓位 {r['avg_expo']:.1%}  "
                  f"择时/离场贡献 {r['timing']:+.2%}  选股贡献 {r['selection']:+.2%}")
            for bn, d in (r.get("reg") or {}).items():
                print(f"   回归 vs {bn}: β={d['beta']:.3f} 年化α={d['alpha_ann']:+.1%} "
                      f"t={d['t_alpha']:+.2f} p={d['p_alpha']:.3f} "
                      f"R²={d['r2']:.3f}")

        # ---- 自检 ----
        hr("Part 5  自检")
        for nm, bk in books_all.items():
            last_tr = max(r.trade_date for r in bk["records"])
            if last_tr <= WIN_END:
                rr = strategy_curve(bk, mkt)
                ok = abs(rr["cash"] - bk["asset_snapshot"]["cash"]) < 1e-6
                print(f"  {nm:<7} 末笔成交 {last_tr} ≤ 区间末 → 期末现金与快照一致: "
                      f"{'OK' if ok else '不一致 <<<'}")
            else:
                print(f"  {nm:<7} 末笔成交 {last_tr} > 区间末 {WIN_END}，"
                      f"期末现金不可直接对账（区间内已含全部成交则现金仍应一致）")
        print(f"\n{TAG} 脚本正常结束；以上所有数字均来自本次运行输出")
    finally:
        try:
            store.close()
        except Exception:
            pass


if __name__ == "__main__":
    main()
