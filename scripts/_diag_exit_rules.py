"""后视镜检验：把用户每一笔真实买入，用「技术指标恶化」规则重新模拟离场。

用户的需求（2026-11-14 原话）
------------------------------
> "这些年我买的这些股票，如果现在做回测，用后视镜去看，持有到什么时候收益是最成功的
>  （不一定卖在最高点，只需在技术指标恶化时，比如 MACD 死叉、KDJ 早已死叉、
>   蜡烛图出现明显见顶信号时离场），以此来给我一个反思或者给后续策略做建议。"

设计要点
--------
1. **不预测、不选股**：只用已经发生的行情，改变"什么时候卖"这一件事
2. **三种离场规则**，各自独立统计，也做 OR 组合：
   - ``MACD``：DIF 下穿 DEA（死叉）
   - ``KDJ``：K 下穿 D 且 J 处于高位（超买区死叉，过滤震荡假信号）
   - ``KLINE``：明显见顶 K 线（长上影阴线 / 乌云盖顶 / 黄昏之星）
3. **四个对照**：
   - 实际卖出（用户真实决策）
   - 指标离场（本脚本）
   - 买入持有到 2026-11-10
   - MFE 回撤 8% 移动止盈（作为"只比指标离场差一点"的参照）
4. **诚实边界**：这是事后检验，规则是在知道结果后挑的，天然乐观。必须同时看
   "指标离场是否优于实际"和"是否优于买入持有"，以及每笔的样本数。

数据
----
- 交割单：``投研反思/Table_1254.csv`` + ``Table_1010.csv``（GBK）
- 行情：``E:/LihuQuantify/data/lihu_quant.duckdb`` 的 ``daily_quotes`` + ``adj_factors``
- 数据截止 2026-11-10，晚于该日入场的交易**无法回测**，会显式排除并报告
"""

from __future__ import annotations

import sys
from pathlib import Path

import duckdb
import numpy as np
import pandas as pd

MINE = Path(r"E:\Dsh_WorkSapce\Dify_Agents\投研反思")
DB = Path(r"E:\LihuQuantify\data\lihu_quant.duckdb")
DATA_END = pd.Timestamp("2026-11-10")


# ===================== 交割单解析 =====================


def to_ts_code(code6: str) -> str:
    """交割单是 6 位纯数字，行情库是带交易所后缀的 ts_code。"""
    c = str(code6).strip()
    if len(c) != 6 or not c.isdigit():
        return c
    h = c[0]
    if h in "69":
        return c + ".SH"
    if h in "023":
        return c + ".SZ"
    if h == "5":
        return c + ".SH"      # 上交所 ETF
    if h == "1":
        return c + ".SZ"      # 深交所 ETF
    if h in "48":
        return c + ".BJ"
    return c


def load_fills() -> pd.DataFrame:
    def _one(f: Path) -> pd.DataFrame:
        df = pd.read_csv(f, encoding="gbk", dtype=str)
        df.columns = [c.strip().replace("\ufeff", "") for c in df.columns]
        for c in df.columns:
            df[c] = df[c].map(
                lambda s: s if pd.isna(s)
                else str(s).strip().lstrip("=").strip().strip('"').strip()
            )
        df = df.drop(columns=[c for c in df.columns if c.startswith("Unnamed")],
                      errors="ignore")
        for c in ("成交数量", "成交均价", "成交金额", "佣金", "其他费用", "印花税", "过户费"):
            if c in df:
                df[c] = pd.to_numeric(df[c], errors="coerce")
        df["日期"] = pd.to_datetime(df["发生日期"], errors="coerce")
        return df

    df = pd.concat([_one(MINE / "Table_1254.csv"), _one(MINE / "Table_1010.csv")],
                   ignore_index=True)
    df = (df.dropna(subset=["日期"]).drop_duplicates()
            .sort_values(["日期", "发生时间"]).reset_index(drop=True))
    return df[df["交易类别"].isin(["证券买入", "证券卖出"])].copy()


# ===================== 行情与指标 =====================


def load_prices(codes: list[str]) -> dict[str, pd.DataFrame]:
    """取每只股票的（前复权）日线，并预计算技术指标。"""
    con = duckdb.connect(str(DB), read_only=True)
    try:
        placeholders = ",".join("?" * len(codes))
        q = con.execute(
            f"SELECT ts_code, trade_date, open, high, low, close, vol FROM daily_quotes "
            f"WHERE ts_code IN ({placeholders}) ORDER BY ts_code, trade_date", codes
        ).fetchdf()
        adj = con.execute(
            f"SELECT ts_code, trade_date, adj_factor FROM adj_factors "
            f"WHERE ts_code IN ({placeholders})", codes
        ).fetchdf()
    finally:
        con.close()

    if q.empty:
        return {}
    q["trade_date"] = pd.to_datetime(q["trade_date"])
    adj["trade_date"] = pd.to_datetime(adj["trade_date"])
    adj_map = adj.set_index(["ts_code", "trade_date"])["adj_factor"].to_dict()

    out: dict[str, pd.DataFrame] = {}
    for code, g in q.groupby("ts_code"):
        g = g.sort_values("trade_date").reset_index(drop=True).copy()
        # 前复权：factor[t] / factor[最后一天]，最后一天价格保持不变
        fac = np.array([adj_map.get((code, d), np.nan) for d in g["trade_date"]],
                       dtype=float)
        if np.isnan(fac).all():
            fac = np.ones(len(g))              # 无复权因子 → 视为无除权
        else:
            # 缺失用最近有效值前向填充
            s = pd.Series(fac).ffill().bfill()
            fac = s.to_numpy()
            fac = fac / fac[-1]
        for c in ("open", "high", "low", "close"):
            g[c] = g[c].astype(float) * fac
        g["vol"] = g["vol"].astype(float)
        out[code] = add_indicators(g)
    return out


def add_indicators(g: pd.DataFrame) -> pd.DataFrame:
    """MACD / KDJ / K 线见顶形态。"""
    g = g.copy()
    c = g["close"]
    # --- MACD (12, 26, 9) ---
    ema12 = c.ewm(span=12, adjust=False).mean()
    ema26 = c.ewm(span=26, adjust=False).mean()
    g["DIF"] = ema12 - ema26
    g["DEA"] = g["DIF"].ewm(span=9, adjust=False).mean()
    g["MACD_HIST"] = (g["DIF"] - g["DEA"]) * 2

    # --- KDJ (9, 3, 3) ---
    low9 = g["low"].rolling(9, min_periods=1).min()
    high9 = g["high"].rolling(9, min_periods=1).max()
    rng = (high9 - low9).replace(0, np.nan)
    rsv = ((c - low9) / rng * 100).fillna(50.0)
    g["K"] = rsv.ewm(alpha=1 / 3, adjust=False).mean()
    g["D"] = g["K"].ewm(alpha=1 / 3, adjust=False).mean()
    g["J"] = 3 * g["K"] - 2 * g["D"]

    # --- K 线形态 ---
    body = (g["close"] - g["open"]).abs()
    total = (g["high"] - g["low"]).replace(0, np.nan)
    upper = g["high"] - g[["open", "close"]].max(axis=1)
    lower = g[["open", "close"]].min(axis=1) - g["low"]
    g["长上影阴线"] = ((g["close"] < g["open"]) & (upper / total > 0.55)).fillna(False)
    # 乌云盖顶：前阳线实体较长，今开高于昨收、今收低于前实体中点
    prev_open, prev_close = g["open"].shift(1), g["close"].shift(1)
    prev_mid = (prev_open + prev_close) / 2
    g["乌云盖顶"] = (
        (prev_close > prev_open)                       # 前一根阳线
        & (g["open"] > prev_close)                     # 今开高于昨收
        & (g["close"] < prev_mid)                      # 今收跌破前实体中点
        & (g["close"] < g["open"])
    ).fillna(False)
    # 黄昏之星：阳线 → 小实体（跳空）→ 阴线收破前阳中点
    small = body / total < 0.3
    g["黄昏之星"] = (
        (g["close"].shift(2) > g["open"].shift(2))
        & small.shift(1)
        & (g["close"] < g["open"])
        & (g["close"] < (g["open"].shift(2) + g["close"].shift(2)) / 2)
    ).fillna(False)
    g["K线见顶"] = g["长上影阴线"] | g["乌云盖顶"] | g["黄昏之星"]

    # --- 死叉（当日发生） ---
    g["MACD死叉"] = ((g["DIF"] < g["DEA"]) & (g["DIF"].shift(1) >= g["DEA"].shift(1))).fillna(False)
    g["KDJ死叉"] = (
        (g["K"] < g["D"]) & (g["K"].shift(1) >= g["D"].shift(1)) & (g["J"].shift(1) > 70)
    ).fillna(False)
    return g


# ===================== 离场模拟 =====================


def simulate_exit(g: pd.DataFrame, entry_idx: int, rule: str) -> tuple[int, pd.Timestamp] | None:
    """从 entry_idx 之后逐日找第一个离场信号，返回 (索引, 日期)。找不到 → None。"""
    if rule == "MACD":
        col = "MACD死叉"
    elif rule == "KDJ":
        col = "KDJ死叉"
    elif rule == "KLINE":
        col = "K线见顶"
    elif rule == "ANY":
        g = g.copy()
        g["_any"] = g["MACD死叉"] | g["KDJ死叉"] | g["K线见顶"]
        col = "_any"
    else:
        raise ValueError(rule)
    sig = g[col].to_numpy()
    for i in range(entry_idx + 1, len(g)):
        if sig[i]:
            return i, g["trade_date"].iloc[i]
    return None


def mfe_exit(g: pd.DataFrame, entry_idx: int, entry_px: float, giveback: float,
             ) -> tuple[int, pd.Timestamp, float]:
    """移动止盈：持有期最高价回撤 giveback 即离场。返回 (索引, 日期, 峰值收益%)。"""
    running_max = entry_px
    peak_ret = 0.0
    for i in range(entry_idx + 1, len(g)):
        hi = g["high"].iloc[i]
        if hi > running_max:
            running_max = hi
        peak_ret = max(peak_ret, running_max / entry_px - 1)
        if running_max > entry_px and g["close"].iloc[i] < running_max * (1 - giveback):
            return i, g["trade_date"].iloc[i], peak_ret * 100
    return len(g) - 1, g["trade_date"].iloc[-1], peak_ret * 100


# ===================== 主流程 =====================


def main() -> int:
    fills = load_fills()
    buys = fills[fills["交易类别"] == "证券买入"].copy()
    print(f"买入记录 {len(buys)} 笔，涉及 {buys['证券代码'].nunique()} 只股票")

    buys["ts_code"] = buys["证券代码"].map(to_ts_code)
    codes = sorted(buys["ts_code"].unique().tolist())
    prices = load_prices(codes)
    print(f"取到行情 {len(prices)} 只 / {len(codes)} 只")

    missing = [c for c in codes if c not in prices]
    if missing:
        print(f"[!] 无行情跳过 {len(missing)} 只: {missing[:10]}")

    rows = []
    no_signal = 0
    skipped = 0
    for r in buys.itertuples():
        code = r.ts_code
        g = prices.get(code)
        if g is None:
            skipped += 1
            continue
        d = pd.Timestamp(r.日期)
        # 入场索引：买入日或之后第一个交易日
        cand = g.index[g["trade_date"] >= d]
        if len(cand) == 0:
            skipped += 1
            continue
        i0 = int(cand[0])
        if i0 + 1 >= len(g):
            skipped += 1          # 买入后无后续行情（数据边界）
            continue
        entry_px = float(r.成交均价)
        qty = abs(float(r.成交数量))

        rec = {"代码": code, "原始代码": r.证券代码, "名称": r.证券名称, "买入日": d.date(), "买入价": entry_px,
               "股数": qty, "实际卖出价": np.nan, "实际卖出日": None}

        for rule in ("MACD", "KDJ", "KLINE", "ANY"):
            res = simulate_exit(g, i0, rule)
            if res is None:
                rec[f"{rule}_卖出日"] = g["trade_date"].iloc[-1].date()
                rec[f"{rule}_收益%"] = (g["close"].iloc[-1] / entry_px - 1) * 100
                rec[f"{rule}_持有天"] = (g["trade_date"].iloc[-1] - d).days
                if rule == "ANY":
                    no_signal += 1
            else:
                idx, dt = res
                rec[f"{rule}_卖出日"] = dt.date()
                rec[f"{rule}_收益%"] = (g["close"].iloc[idx] / entry_px - 1) * 100
                rec[f"{rule}_持有天"] = (dt - d).days

        # MFE 回撤 8%
        idx, dt, peak = mfe_exit(g, i0, entry_px, 0.08)
        rec["MFE8_卖出日"] = dt.date()
        rec["MFE8_收益%"] = (g["close"].iloc[idx] / entry_px - 1) * 100
        rec["MFE8_峰值收益%"] = peak
        rec["MFE8_持有天"] = (dt - d).days

        # 买入持有到数据末尾
        rec["持有至末_收益%"] = (g["close"].iloc[-1] / entry_px - 1) * 100
        rec["持有至末_天数"] = (g["trade_date"].iloc[-1] - d).days
        rows.append(rec)

    res = pd.DataFrame(rows)
    print(f"\n可回测 {len(res)} 笔（跳过 {skipped} 笔：无行情或买入后无数据）")
    print(f"ANY 组合从未触发离场: {no_signal} 笔")

    # ---- 汇总 ----
    print("\n" + "=" * 84)
    print("总体对比（按买入笔数计，等权）")
    print("=" * 84)
    summary = []
    for col, label in [
        ("ANY_收益%", "技术指标离场(MACD|KDJ|K线)"),
        ("MACD_收益%", "仅 MACD 死叉"),
        ("KDJ_收益%", "仅 KDJ 死叉"),
        ("KLINE_收益%", "仅 K 线见顶"),
        ("MFE8_收益%", "峰值回撤 8% 止盈"),
        ("持有至末_收益%", "买入持有至 2026-11-10"),
    ]:
        s = res[col].dropna()
        summary.append({
            "规则": label, "笔数": len(s),
            "平均收益%": round(s.mean(), 2), "中位收益%": round(s.median(), 2),
            "胜率%": round((s > 0).mean() * 100, 1),
            "合计盈亏元": round((s / 100 * res["买入价"] * res["股数"]).sum(), 0),
        })
    print(pd.DataFrame(summary).to_string(index=False))

    # ---- 持有天数 ----
    print("\n各规则平均持有天数：")
    for c, lab in [("ANY_持有天", "ANY"), ("MACD_持有天", "MACD"),
                   ("MFE8_持有天", "MFE8"), ("持有至末_天数", "持有至末")]:
        print(f"  {lab:8s} {res[c].mean():>7.1f} 天")

    # ---- 逐年 ----
    res["年"] = pd.to_datetime(res["买入日"]).dt.year
    print("\n" + "=" * 84)
    print("分年：ANY 指标离场 vs 买入持有")
    print("=" * 84)
    yr = res.groupby("年").apply(lambda x: pd.Series({
        "笔数": len(x),
        "ANY平均%": round(x["ANY_收益%"].mean(), 2),
        "ANY胜率%": round((x["ANY_收益%"] > 0).mean() * 100, 1),
        "持有至末平均%": round(x["持有至末_收益%"].mean(), 2),
        "差值pp": round(x["ANY_收益%"].mean() - x["持有至末_收益%"].mean(), 2),
    }), include_groups=False)
    print(yr.to_string())

    # ---- 按标的（笔数≥3） ----
    print("\n" + "=" * 84)
    print("按标的：ANY 指标离场 vs 买入持有（买入笔数 ≥3，按差值排序）")
    print("=" * 84)
    stk = res.groupby(["代码", "名称"]).agg(
        笔数=("ANY_收益%", "size"),
        ANY平均=("ANY_收益%", "mean"),
        持有平均=("持有至末_收益%", "mean"),
    ).reset_index()
    stk = stk[stk["笔数"] >= 3].copy()
    stk["差值pp"] = stk["ANY平均"] - stk["持有平均"]
    stk = stk.sort_values("差值pp", ascending=False)
    stk[["ANY平均", "持有平均", "差值pp"]] = stk[["ANY平均", "持有平均", "差值pp"]].round(2)
    print(stk.to_string(index=False))

    out = Path(r"E:\LihuQuantify\outputs\exit_rule_compare_20261114.csv")
    out.parent.mkdir(parents=True, exist_ok=True)
    res.to_csv(out, index=False, encoding="utf-8-sig")
    print(f"\n逐笔结果已存: {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
