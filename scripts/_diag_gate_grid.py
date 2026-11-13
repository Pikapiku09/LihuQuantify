#!/usr/bin/env python
"""闸门阈值网格 + Treynor-Mazuy 择时检验（P1-5）。

回答三个问题：
1. 在**无未来函数（T+1）**口径下，闸门到底提供了什么？（收益 or 风险调整）
2. 上涨阈值 / 窗口 / r5 反转阈值 的网格里，是"平坦区间"还是"尖峰"？当前配置在哪？
3. 旧严格语义（只有主维度）与新宽松语义（含辅维度）谁的风险调整更好？

顺带把两件方法论事故钉在证据上：
- **口径纠错**：同一段数据用「同日对齐」与「T+1」各跑一遍，量化未来函数能虚增多少。
- **伪交易日**：用 trade_cal 剔除 index_daily 里周末的脏行，并给出剔除前后的结论差。

数据（铁律：只用本地，禁止联网）
-------------------------------
首选 ``data/lihu_quant.duckdb`` 的 ``index_daily``（000001.SH），并用同库 ``trade_cal`` 校验；
若该库被其它进程独占（DuckDB 写锁），降级到同一接口的本地缓存
``data/cache/index_daily_000001_SH_20301231_na.json`` 并交叉核对。两条路都只读本地文件。

口径（全部显式声明）
--------------------
- **仓位对齐**：``pos[t] = state[t-1]=='上涨'``（state 由当日收盘算出，只作用于次日收益），
  与 EventDrivenEngine 的 T+1 撮合一致。``lag=0`` 把当日信号用在当日收益上＝未来函数，仅用于复现历史基线。
- **区间收益**：区间首日按收盘建仓，收益从次日开始计（``equity[0]=1.0``），IS 与 OOS 收益不重叠。
- **成本**：仓位单边变动计 5bp（默认；另给 0 / 10bp）。买入持有作为指数基准不计费（偏保守）。
- **年化**：``(1+total)**(252/n_bars)-1``（与 ``backtest/metrics.py`` 的 trading_days=252 一致）。
- **波动率**：``std(daily, ddof=0)*sqrt(252)``；**夏普** ``= 年化/波动率``（rf=0，与 Faber 2007 可比），
  另列 repo 式 ``sqrt(252)*mean/std`` 备查；**卡玛** ``= 年化/|最大回撤|``。
- **滚动 6 月胜率**：126 交易日滚动窗口内策略区间收益 > 0 的比例；另给"跑赢买入持有"的比例。
- **IS** = 2025-01-01~2025-12-31；**OOS** = 2026-01-01~2026-11-10；**全段** = IS ∪ OOS。
- **超额** = 策略累计收益 − 同期买入持有累计收益（pp）。

用法
----
    .venv\\Scripts\\python.exe scripts\\_diag_gate_grid.py
    .venv\\Scripts\\python.exe scripts\\_diag_gate_grid.py --db-wait 180
    .venv\\Scripts\\python.exe scripts\\_diag_gate_grid.py --source cache
    .venv\\Scripts\\python.exe scripts\\_diag_gate_grid.py --keep-raw   # 不复位伪交易日（对照组）

输出
----
``outputs/gate_grid_20261114.csv``：100 格主网格（grid=main）+ 20 格旧严格语义（grid=strict）
+ 7 行深跌保护敏感性（grid=aux_deep），含 ``*_raw`` 未清洗对照列。
stdout：口径纠错、三方风险调整对照、Faber 对照、脏数据敏感性、状态有效性、网格摘要、
严格/宽松对比、一维边际、平坦区间、TM 检验、可引用数字块。
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import duckdb  # noqa: E402
import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402
from scipy import stats  # noqa: E402

from lihu_quantify.market import classify_market_state  # noqa: E402

try:  # Windows 控制台中文
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:  # pragma: no cover
    pass

# ---------------------------------------------------------------- 常量
DB_PATH = ROOT / "data" / "lihu_quant.duckdb"
CACHE_PATH = ROOT / "data" / "cache" / "index_daily_000001_SH_20301231_na.json"
OUT_CSV = ROOT / "outputs" / "gate_grid_20261114.csv"

TS_CODE = "000001.SH"
FIELDS = ["trade_date", "close", "pct_chg", "vol"]

IS_START, IS_END = pd.Timestamp("2025-01-01"), pd.Timestamp("2025-12-31")
OOS_START, OOS_END = pd.Timestamp("2026-01-01"), pd.Timestamp("2026-11-10")
FULL_START = IS_START

UP_THRESHOLDS = [1.0, 2.0, 3.0, 4.0, 5.0]
WINDOWS = [10, 15, 20, 30]
REV_R5S = [0.0, 1.0, 2.0, 3.0, 5.0]
DEEP_R5_FIXED = 5.0
AUX_DEEP_R5S = [2.0, 3.0, 4.0, 5.0, 6.0, 8.0, 10.0]

CURRENT = {"up_thresh_pct": 3.0, "window_days": 20, "rev_r5_pct": 2.0, "deep_r5_pct": 5.0}
COST_ONE_WAY = 0.0005
COST_CASES = [0.0, 0.0005, 0.0010]
ANNUAL_DAYS = 252.0
ROLL_WIN = 126
NW_LAGS = 5
PLATEAU_TOL_PP = 5.0
# Faber (2007) 的 S&P500 对照数字由 Lead 提供（本任务禁止联网，未独立复核）
FABER = {"cagr_bh": 0.1124, "cagr_timing": 0.1118, "vol_bh": 0.1747, "vol_timing": 0.1400,
         "mar_bh": 0.25, "mar_timing": 0.48}


# ---------------------------------------------------------------- 数据
def _read_duckdb() -> pd.DataFrame:
    con = duckdb.connect(str(DB_PATH), read_only=True)
    try:
        df = con.execute(
            "SELECT trade_date, close, pct_chg, vol FROM index_daily WHERE ts_code = ? "
            "AND trade_date <= DATE '2026-11-10' ORDER BY trade_date",
            [TS_CODE],
        ).fetchdf()
    finally:
        con.close()
    return df


def _read_calendar() -> pd.DataFrame | None:
    try:
        con = duckdb.connect(str(DB_PATH), read_only=True)
        try:
            cal = con.execute("SELECT cal_date, is_open FROM trade_cal ORDER BY cal_date").fetchdf()
        finally:
            con.close()
        cal["cal_date"] = pd.to_datetime(cal["cal_date"])
        return cal
    except Exception:
        return None


def _read_cache() -> pd.DataFrame:
    raw = json.loads(CACHE_PATH.read_text(encoding="utf-8"))
    data = raw["data"]
    return pd.DataFrame(data["items"], columns=data["fields"])[FIELDS]


def load_index(source: str = "auto", db_wait: float = 60.0) -> tuple[pd.DataFrame, str, float]:
    t0 = time.time()
    if source in ("auto", "duckdb"):
        attempts = max(1, int(db_wait // 10) + 1)
        last_err = None
        for i in range(attempts):
            try:
                df = _read_duckdb()
                df["trade_date"] = pd.to_datetime(df["trade_date"])
                df = df.sort_values("trade_date").reset_index(drop=True)
                print(f"[数据] 来源=duckdb index_daily  行数={len(df)}  "
                      f"{df['trade_date'].min():%Y-%m-%d} ~ {df['trade_date'].max():%Y-%m-%d}"
                      f"（解锁等待 {time.time() - t0:.0f}s）")
                return df, "duckdb", time.time() - t0
            except Exception as exc:
                last_err = exc
                if i < attempts - 1:
                    time.sleep(10)
        if source == "duckdb":
            raise SystemExit(f"[数据] duckdb 不可读且禁止降级：{type(last_err).__name__}: {last_err}")
        print(f"[数据] duckdb 不可用（{type(last_err).__name__}），降级到本地 Tushare 缓存。")

    df = _read_cache()
    df["trade_date"] = pd.to_datetime(df["trade_date"])
    df = df.sort_values("trade_date").reset_index(drop=True)
    print(f"[数据] 来源=本地缓存 index_daily_000001_SH_20301231_na.json  行数={len(df)}  "
          f"{df['trade_date'].min():%Y-%m-%d} ~ {df['trade_date'].max():%Y-%m-%d}")
    try:
        db_df = _read_duckdb()
        db_df["trade_date"] = pd.to_datetime(db_df["trade_date"])
        merged = db_df.merge(df, on="trade_date", how="outer", suffixes=("_db", "_cache"))
        only_db = merged.loc[merged["close_cache"].isna(), "trade_date"]
        only_cache = merged.loc[merged["close_db"].isna(), "trade_date"]
        both = merged.dropna(subset=["close_db", "close_cache"])
        print(f"[数据] 交叉核对：仅DB有 {len(only_db)} 天{[d.strftime('%Y-%m-%d') for d in only_db[:5]]}，"
              f"仅缓存有 {len(only_cache)} 天{[d.strftime('%Y-%m-%d') for d in only_cache[:5]]}，"
              f"共同 {len(both)} 天收盘价最大差 {(both['close_db'] - both['close_cache']).abs().max():.6f}")
    except Exception as exc:
        print(f"[数据] 交叉核对跳过（duckdb 被占用：{type(exc).__name__}）")
    return df, "cache", time.time() - t0


def validate_calendar(df: pd.DataFrame, cal: pd.DataFrame | None) -> tuple[pd.DataFrame, list[str]]:
    """剔除伪交易日：周末（全历史）＋ trade_cal 覆盖区间内 is_open=0 的日期。"""
    d = df.copy()
    dow = d["trade_date"].dt.dayofweek
    bad = pd.Series(dow >= 5, index=d.index)
    reason = pd.Series(np.where(dow >= 5, "周末", ""), index=d.index)

    if cal is not None and len(cal):
        open_days = set(cal.loc[cal["is_open"] == 1, "cal_date"])
        lo, hi = cal["cal_date"].min(), cal["cal_date"].max()
        not_open = d["trade_date"].between(lo, hi) & ~d["trade_date"].isin(open_days)
        bad |= not_open
        reason[not_open & (reason == "")] = "trade_cal is_open=0"
        note = f"trade_cal 覆盖 {lo:%Y-%m-%d} ~ {hi:%Y-%m-%d}"
    else:
        note = "trade_cal 不可用，仅做周末校验"

    removed = []
    if bad.any():
        print(f"[校验] 发现伪交易日 {int(bad.sum())} 条（{note}）：")
        for i in d.index[bad]:
            r = d.loc[i]
            pct = f"{r['pct_chg']:+.2f}%" if pd.notna(r.get("pct_chg")) else "n/a"
            vol = f"{r['vol']:,.0f}" if pd.notna(r.get("vol")) else "n/a"
            print(f"        剔除 {r['trade_date']:%Y-%m-%d}（周{int(dow.loc[i]) + 1}）  "
                  f"close={r['close']:.2f}  pct_chg={pct}  vol={vol}  原因={reason.loc[i]}")
            removed.append(f"{r['trade_date']:%Y-%m-%d}")
    else:
        print(f"[校验] 未发现伪交易日（{note}）。")

    clean = d.loc[~bad].sort_values("trade_date").reset_index(drop=True)
    print(f"[校验] 清洗后 {len(clean)} 行（原 {len(d)} 行，剔除 {int(bad.sum())} 行）；"
          f"{clean['trade_date'].min():%Y-%m-%d} ~ {clean['trade_date'].max():%Y-%m-%d}")
    if removed:
        print("[校验] 影响：该行会让其后约 20 个交易日的 ret_20d / MA20 被系统性抬升，"
              "且其自身 +4.73%/+4.59% 的尖峰直接污染区间收益。")
    return clean, removed


# ---------------------------------------------------------------- 闸门语义
def classify_gate(idx_df: pd.DataFrame, up_thresh: float = 3.0, window: int = 20,
                  deep_r5: float = 5.0, rev_r5: float = 2.0) -> tuple[pd.Series, pd.DataFrame]:
    """参数化复刻 classify_market_state（block 语义）。"""
    if idx_df is None or idx_df.empty:
        return pd.Series(dtype=object), pd.DataFrame()
    d = idx_df.sort_values("trade_date").reset_index(drop=True).copy()
    close = d["close"].astype(float)
    ret_w = close.pct_change(window) * 100.0
    ret_5 = close.pct_change(5) * 100.0
    ma5 = close.rolling(5).mean()
    ma_w = close.rolling(window).mean()
    ma5_prev = ma5.shift(1)

    r5 = ret_5.fillna(0.0)
    ma5_up = (ma5 > ma5_prev) & ma5.notna() & ma5_prev.notna()
    ma5_above = (ma5 > ma_w) & ma5.notna() & ma_w.notna()

    state = np.where(
        ret_w.isna(), "未知",
        np.where(ret_w >= up_thresh, "上涨",
                 np.where(ret_w <= -up_thresh,
                          np.where(r5 >= deep_r5, "上涨", "下跌"),
                          np.where((ma5_above & ma5_up) | (r5 >= rev_r5), "上涨", "震荡"))),
    )
    return pd.Series(state, index=d.index), d


def classify_strict(idx_df: pd.DataFrame, up_thresh: float = 3.0,
                    window: int = 20) -> tuple[pd.Series, pd.DataFrame]:
    """P1 修复前的旧严格闸门（仅主维度三分段），用于严格/宽松对比。"""
    d = idx_df.sort_values("trade_date").reset_index(drop=True).copy()
    ret_w = d["close"].astype(float).pct_change(window) * 100.0
    state = np.where(ret_w.isna(), "未知",
                     np.where(ret_w >= up_thresh, "上涨",
                              np.where(ret_w <= -up_thresh, "下跌", "震荡")))
    return pd.Series(state, index=d.index), d


def assert_semantics(df: pd.DataFrame) -> None:
    """默认参数 (3, 20, 5, 2) 与官方函数逐日断言相等；前 5 年 + 全历史都断言。"""
    official = classify_market_state(df[["trade_date", "close"]], window=20)
    states, sorted_df = classify_gate(df, 3.0, 20, 5.0, 2.0)
    dates = list(sorted_df["trade_date"])
    mine = list(states)
    first5_end = pd.Timestamp(dates[0]) + pd.DateOffset(years=5)
    scopes = {"前5年": [i for i, d in enumerate(dates) if d <= first5_end],
              "全历史": list(range(len(dates)))}
    for name, idxs in scopes.items():
        bad = [(dates[i], official.get(dates[i]), mine[i]) for i in idxs
               if official.get(dates[i]) != mine[i]]
        if bad:
            print(f"[断言] {name} 不一致 {len(bad)} 天，前 10 例：")
            for dt, o, m in bad[:10]:
                print(f"         {dt:%Y-%m-%d}  官方={o}  参数化={m}")
            raise SystemExit("[断言] 闸门语义复刻失败，终止。")
    print(f"[断言] 逐日一致：前5年 {len(scopes['前5年'])} 天、全历史 {len(dates)} 天，"
          f"参数化闸门 == 官方 classify_market_state(3%/20日/深跌5%/r5≥2%)")


# ---------------------------------------------------------------- 回测
def backtest_gate(states: pd.Series, sorted_df: pd.DataFrame, start: pd.Timestamp,
                  end: pd.Timestamp, cost: float = COST_ONE_WAY, lag: int = 1) -> dict:
    """指数级闸门回测。lag=1: state[t]→pos[t+1]（T+1）；lag=0: 同日对齐（含未来函数）。"""
    dates = sorted_df["trade_date"].to_numpy()
    close = sorted_df["close"].to_numpy(dtype=float)
    n = len(close)
    if len(states) != n:
        raise SystemExit(f"[回测] 状态长度 {len(states)} != 行情长度 {n}")

    up = (states.to_numpy() == "上涨").astype(float)
    pos = np.zeros(n)
    if lag == 0:
        pos[:] = up
    else:
        pos[lag:] = up[:-lag]
    ret = np.zeros(n)
    ret[1:] = close[1:] / close[:-1] - 1.0

    mask = (dates >= np.datetime64(start)) & (dates <= np.datetime64(end))
    idx = np.flatnonzero(mask)
    if len(idx) < 30:
        raise SystemExit(f"[回测] {start:%Y-%m-%d}~{end:%Y-%m-%d} 交易日不足：{len(idx)}")
    n_bars = len(idx)

    # 区间首日收盘建仓 → 收益从次日起（IS/OOS 收益不重叠）
    eidx = idx[1:]
    p = pos[eidx]
    r = ret[eidx]
    turn = np.zeros(len(eidx))
    turn[0] = abs(p[0])
    turn[1:] = np.abs(np.diff(p))
    net = p * r - turn * cost
    bench = r
    eq = np.concatenate([[1.0], np.cumprod(1.0 + net)])
    eq_b = np.concatenate([[1.0], np.cumprod(1.0 + bench)])

    total = float(eq[-1] - 1.0)
    total_b = float(eq_b[-1] - 1.0)
    annual = float((1.0 + total) ** (ANNUAL_DAYS / n_bars) - 1.0) if total > -1 else -1.0
    vol = float(net.std(ddof=0) * np.sqrt(ANNUAL_DAYS))
    sharpe = float(annual / vol) if vol > 0 else float("nan")
    sharpe_repo = float(np.sqrt(ANNUAL_DAYS) * net.mean() / net.std(ddof=1)) if net.std(ddof=1) > 0 else float("nan")
    mdd = float((eq / np.maximum.accumulate(eq) - 1.0).min())
    calmar = float(annual / abs(mdd)) if mdd < 0 else float("nan")

    def roll_win(curve: np.ndarray) -> float:
        if len(curve) <= ROLL_WIN:
            return float("nan")
        return float(((curve[ROLL_WIN:] / curve[:-ROLL_WIN] - 1.0) > 0).mean())

    def roll_beat(a: np.ndarray, b: np.ndarray) -> float:
        if len(a) <= ROLL_WIN:
            return float("nan")
        return float(((a[ROLL_WIN:] / a[:-ROLL_WIN]) > (b[ROLL_WIN:] / b[:-ROLL_WIN])).mean())

    return {
        "days": n_bars, "total": total, "total_bh": total_b, "annual": annual,
        "vol": vol, "sharpe": sharpe, "sharpe_repo": sharpe_repo, "mdd": mdd, "calmar": calmar,
        "roll6m_win": roll_win(eq), "roll6m_beat_bh": roll_beat(eq, eq_b),
        "hold": float(pos[idx].mean()), "trades": int((turn > 0).sum()),
        "excess_pp": (total - total_b) * 100.0, "excess_daily_nw_t": nw_t(net - bench),
        "net_ret": net, "bench_ret": bench,
    }


def nw_t(x: np.ndarray, lags: int = NW_LAGS) -> float:
    x = np.asarray(x, dtype=float)
    n = len(x)
    if n < 10:
        return float("nan")
    e = x - x.mean()
    var = float(e @ e) / n
    for lag in range(1, min(lags, n - 1) + 1):
        var += 2.0 * (1.0 - lag / (lags + 1.0)) * float(e[lag:] @ e[:-lag]) / n
    return float(x.mean() / np.sqrt(var / n)) if var > 0 else float("nan")


def bench_only(sorted_df: pd.DataFrame, start: pd.Timestamp, end: pd.Timestamp,
               cost: float = 0.0) -> dict:
    ones = pd.Series(np.where(np.arange(len(sorted_df)) >= 0, "上涨", "上涨"))
    return backtest_gate(ones, sorted_df, start, end, cost=cost, lag=1)


# ---------------------------------------------------------------- 择时检验
def treynor_mazuy(rp: np.ndarray, rm: np.ndarray) -> dict:
    rp, rm = np.asarray(rp, float), np.asarray(rm, float)
    X = np.column_stack([np.ones_like(rm), rm, rm * rm])
    coef, *_ = np.linalg.lstsq(X, rp, rcond=None)
    resid = rp - X @ coef
    n, k = X.shape
    dof = n - k
    rss = float(resid @ resid)
    se = np.sqrt(np.diag((rss / dof) * np.linalg.inv(X.T @ X)))
    tvals = coef / se
    pvals = 2.0 * stats.t.sf(np.abs(tvals), dof)
    tss = float((rp - rp.mean()) @ (rp - rp.mean()))
    r2 = 1.0 - rss / tss if tss > 0 else float("nan")
    betas = {}
    for tag, m in (("up", rm > 0), ("down", rm < 0)):
        if int(m.sum()) < 10:
            betas[tag] = float("nan")
        else:
            Xb = np.column_stack([np.ones(int(m.sum())), rm[m]])
            betas[tag] = float(np.linalg.lstsq(Xb, rp[m], rcond=None)[0][1])
    return {"n": n, "alpha_annual": float(coef[0] * ANNUAL_DAYS), "beta": float(coef[1]),
            "gamma": float(coef[2]), "t_alpha": float(tvals[0]), "t_gamma": float(tvals[2]),
            "p_alpha": float(pvals[0]), "p_gamma": float(pvals[2]), "r2": float(r2),
            "beta_up": betas["up"], "beta_down": betas["down"]}


def state_effectiveness(states: pd.Series, sorted_df: pd.DataFrame,
                        start: pd.Timestamp, end: pd.Timestamp) -> pd.DataFrame:
    dates = sorted_df["trade_date"].to_numpy()
    close = sorted_df["close"].to_numpy(dtype=float)
    fwd = np.full(len(close), np.nan)
    fwd[:-1] = close[1:] / close[:-1] - 1.0
    st = states.to_numpy()
    mask = (dates >= np.datetime64(start)) & (dates <= np.datetime64(end))
    rows = []
    for s in ("上涨", "震荡", "下跌", "未知"):
        x = fwd[mask & (st == s)]
        x = x[np.isfinite(x)]
        rows.append({"state": s, "days": int((mask & (st == s)).sum()),
                     "fwd_mean_bp": float(x.mean() * 1e4) if len(x) else float("nan"),
                     "fwd_annual": float((1.0 + x.mean()) ** ANNUAL_DAYS - 1.0) if len(x) else float("nan"),
                     "hit": float((x > 0).mean()) if len(x) else float("nan")})
    return pd.DataFrame(rows)


# ---------------------------------------------------------------- 主流程
def grid_specs() -> pd.DataFrame:
    rows = [("main", "loose", up, win, rev, DEEP_R5_FIXED)
            for up in UP_THRESHOLDS for win in WINDOWS for rev in REV_R5S]
    rows += [("strict", "strict", up, win, 0.0, DEEP_R5_FIXED)
             for up in UP_THRESHOLDS for win in WINDOWS]
    rows += [("aux_deep", "loose", CURRENT["up_thresh_pct"], int(CURRENT["window_days"]),
              CURRENT["rev_r5_pct"], deep) for deep in AUX_DEEP_R5S]
    return pd.DataFrame(rows, columns=["grid", "mode", "up_thresh_pct", "window_days",
                                       "rev_r5_pct", "deep_r5_pct"])


def make_states(df: pd.DataFrame, spec: pd.Series):
    up, win = float(spec["up_thresh_pct"]), int(spec["window_days"])
    if spec["mode"] == "strict":
        return classify_strict(df, up, win)
    return classify_gate(df, up, win, float(spec["deep_r5_pct"]), float(spec["rev_r5_pct"]))


def evaluate(df: pd.DataFrame, spec: pd.Series) -> dict:
    states, sorted_df = make_states(df, spec)
    out = {}
    for tag, (s, e) in {"is": (IS_START, IS_END), "oos": (OOS_START, OOS_END),
                        "full": (FULL_START, OOS_END)}.items():
        r = backtest_gate(states, sorted_df, s, e)
        for k in ("days", "total", "annual", "mdd", "vol", "sharpe", "calmar",
                  "roll6m_win", "roll6m_beat_bh", "hold", "trades", "excess_pp",
                  "excess_daily_nw_t"):
            out[f"{tag}_{k}"] = r[k]
        out[f"{tag}_bench_total"] = r["total_bh"]
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--source", choices=["auto", "duckdb", "cache"], default="auto")
    ap.add_argument("--db-wait", type=float, default=60.0)
    ap.add_argument("--keep-raw", action="store_true")
    args = ap.parse_args()

    print("=" * 110)
    print("P1-5 闸门阈值网格 + Treynor-Mazuy 择时检验（本地数据，禁止联网）")
    print("=" * 110)

    df_raw, source, waited = load_index(args.source, args.db_wait)
    if len(df_raw) < 5000:
        raise SystemExit("[数据] 序列过短，终止。")
    cal = None if args.keep_raw else _read_calendar()
    if args.keep_raw:
        df, removed = df_raw, []
        print("[校验] --keep-raw：跳过交易日历校验（脏数据原样保留，仅作对照）。")
    else:
        df, removed = validate_calendar(df_raw, cal)
    assert_semantics(df)

    # ---------------- 【零】口径对照
    print("\n" + "=" * 110)
    print("【零】口径对照：同日对齐（含未来函数） vs T+1（无未来函数）｜全段 2025-01-02~2026-11-10")
    print("=" * 110)
    loose_states, sd_l = classify_gate(df, 3.0, 20, 5.0, 2.0)
    strict_states, sd_s = classify_strict(df, 3.0, 20)
    loose_states_raw, sd_lr = classify_gate(df_raw, 3.0, 20, 5.0, 2.0)
    strict_states_raw, sd_sr = classify_strict(df_raw, 3.0, 20)
    bh = bench_only(sd_l, FULL_START, OOS_END)
    bh_is = bench_only(sd_l, IS_START, IS_END)
    bh_oos = bench_only(sd_l, OOS_START, OOS_END)
    print(f"  {'买入持有 BH':<22}{'累计':>10}{'年化':>9}{'最大回撤':>10}{'夏普':>7}{'卡玛':>7}")
    print(f"  {'':<22}{bh['total']:>10.2%}{bh['annual']:>9.2%}{bh['mdd']:>9.2%}"
          f"{bh['sharpe']:>7.2f}{bh['calmar']:>7.2f}")
    print(f"  （分区间）样本内 {bh_is['total']:+.2%}/卡玛 {bh_is['calmar']:.2f}；"
          f"样本外 {bh_oos['total']:+.2%}/卡玛 {bh_oos['calmar']:.2f}")
    print(f"\n  {'闸门':<12}{'对齐':<10}{'累计':>10}{'年化':>9}{'最大回撤':>10}{'夏普':>7}{'卡玛':>7}{'持仓':>7}")
    print("  " + "-" * 74)
    for name, st, sd, st_r, sd_r in (("新宽松(当前)", loose_states, sd_l, loose_states_raw, sd_lr),
                                     ("旧严格", strict_states, sd_s, strict_states_raw, sd_sr)):
        for lag, tag in ((0, "lag=0 错误"), (1, "lag=1 正确")):
            r = backtest_gate(st, sd, FULL_START, OOS_END, cost=0.0, lag=lag)
            print(f"  {name:<12}{tag:<10}{r['total']:>10.2%}{r['annual']:>9.2%}{r['mdd']:>9.2%}"
                  f"{r['sharpe']:>7.2f}{r['calmar']:>7.2f}{r['hold']:>7.1%}")
    print("  注：lag=1 无成本口径与 Lead 独立复核逐位一致（BH +12.30% / 新宽松 +9.38% / 旧严格 +7.24%）。")
    print("      未来函数虚增幅度：新宽松 "
          f"{backtest_gate(loose_states, sd_l, FULL_START, OOS_END, cost=0.0, lag=0)['total'] - backtest_gate(loose_states, sd_l, FULL_START, OOS_END, cost=0.0, lag=1)['total']:+.2%}"
          "，旧严格 "
          f"{backtest_gate(strict_states, sd_s, FULL_START, OOS_END, cost=0.0, lag=0)['total'] - backtest_gate(strict_states, sd_s, FULL_START, OOS_END, cost=0.0, lag=1)['total']:+.2%}")

    # ---------------- 【一】三方风险调整对照
    print("\n" + "=" * 110)
    print("【一】三方风险调整对照（T+1，全段，rf=0）")
    print("=" * 110)
    print(f"  {'策略':<14}{'成本':>6}{'累计':>10}{'年化':>9}{'波动率':>9}{'夏普':>7}{'夏普repo':>9}"
          f"{'最大回撤':>10}{'卡玛':>7}{'持仓':>7}{'交易':>6}")
    for cost in COST_CASES:
        rows = [("买入持有 BH", bh)]
        rows.append(("新宽松(当前)", backtest_gate(loose_states, sd_l, FULL_START, OOS_END, cost=cost)))
        rows.append(("旧严格", backtest_gate(strict_states, sd_s, FULL_START, OOS_END, cost=cost)))
        for name, r in rows:
            print(f"  {name:<14}{cost * 1e4:>4.0f}bp{r['total']:>10.2%}{r['annual']:>9.2%}"
                  f"{r['vol']:>9.2%}{r['sharpe']:>7.2f}{r['sharpe_repo']:>9.2f}{r['mdd']:>10.2%}"
                  f"{r['calmar']:>7.2f}{r['hold']:>7.1%}{r['trades']:>6}")
        print("  " + "-" * 96)
    print("  读法：夏普/卡玛的分子是年化收益（252 日几何年化），分母分别是年化波动与 |最大回撤|；")
    print("        夏普repo 为 backtest/metrics.py 的 sqrt(252)*mean/std，两者仅年化方式不同。")

    # ---------------- 【二】Faber 对照
    print("\n" + "=" * 110)
    print("【二】与 Faber (2007) 10 月均线择时的对照（外部文献数字由 Lead 提供，本任务禁止联网、未独立复核）")
    print("=" * 110)
    r_l = backtest_gate(loose_states, sd_l, FULL_START, OOS_END, cost=COST_ONE_WAY)
    r_s = backtest_gate(strict_states, sd_s, FULL_START, OOS_END, cost=COST_ONE_WAY)
    print(f"  {'':<18}{'收益(BH→择时)':>20}{'波动(BH→择时)':>18}{'MAR/卡玛(BH→择时)':>20}")
    print(f"  {'Faber S&P500':<18}{FABER['cagr_bh']:>8.2%} → {FABER['cagr_timing']:<7.2%}"
          f"{FABER['vol_bh']:>8.2%} → {FABER['vol_timing']:<7.2%}"
          f"{FABER['mar_bh']:>8.2f} → {FABER['mar_timing']:<7.2f}")
    print(f"  {'本系统 新宽松':<18}{bh['annual']:>8.2%} → {r_l['annual']:<7.2%}"
          f"{bh['vol']:>8.2%} → {r_l['vol']:<7.2%}{bh['calmar']:>8.2f} → {r_l['calmar']:<7.2f}"
          f"   (5bp 成本)")
    print(f"  {'本系统 旧严格':<18}{bh['annual']:>8.2%} → {r_s['annual']:<7.2%}"
          f"{bh['vol']:>8.2%} → {r_s['vol']:<7.2%}{bh['calmar']:>8.2f} → {r_s['calmar']:<7.2f}"
          f"   (5bp 成本)")
    print(f"  模式判定：收益 {'基本持平/略降' if abs(r_s['annual'] - bh['annual']) < 0.05 else '明显变化'}，"
          f"波动 {'下降' if r_s['vol'] < bh['vol'] else '上升'}，"
          f"卡玛 {'上升' if r_s['calmar'] > bh['calmar'] else '下降'}"
          f" → 与 Faber 的模式{'一致' if (r_s['vol'] < bh['vol'] and r_s['annual'] <= bh['annual'] + 0.02) else '不一致'}")

    # ---------------- 网格
    specs = grid_specs()
    print(f"\n[网格] {len(specs)} 行（主网格 100 + 旧严格 20 + 深跌敏感性 7），开始回测 ...")
    t0 = time.time()
    recs = []
    for _, spec in specs.iterrows():
        rec = spec.to_dict()
        rec.update(evaluate(df, spec))
        raw = evaluate(df_raw, spec)
        rec.update({f"{k}_raw": raw[k] for k in ("is_excess_pp", "oos_excess_pp",
                                                 "full_excess_pp", "oos_total", "oos_calmar")})
        recs.append(rec)
    grid = pd.DataFrame(recs)
    grid["delta_oos_excess_pp"] = grid["oos_excess_pp"] - grid["oos_excess_pp_raw"]
    print(f"[网格] 完成，用时 {time.time() - t0:.1f}s")

    main_g = grid[grid["grid"] == "main"].copy()
    strict_g = grid[grid["grid"] == "strict"].copy()
    aux = grid[grid["grid"] == "aux_deep"].copy()

    main_g["rank_is_excess"] = main_g["is_excess_pp"].rank(ascending=False, method="min").astype(int)
    main_g["rank_oos_excess"] = main_g["oos_excess_pp"].rank(ascending=False, method="min").astype(int)
    main_g["rank_oos_calmar"] = main_g["oos_calmar"].rank(ascending=False, method="min").astype(int)
    main_g["is_current"] = ((main_g["up_thresh_pct"] == CURRENT["up_thresh_pct"])
                            & (main_g["window_days"] == CURRENT["window_days"])
                            & (main_g["rev_r5_pct"] == CURRENT["rev_r5_pct"])).astype(int)
    strict_g["rank_is_excess"] = strict_g["is_excess_pp"].rank(ascending=False, method="min").astype(int)
    strict_g["rank_oos_excess"] = strict_g["oos_excess_pp"].rank(ascending=False, method="min").astype(int)
    strict_g["rank_oos_calmar"] = strict_g["oos_calmar"].rank(ascending=False, method="min").astype(int)
    strict_g["is_current"] = 0

    i_bi = main_g["is_excess_pp"].idxmax()
    i_bo = main_g["oos_excess_pp"].idxmax()
    i_bc = main_g["oos_calmar"].idxmax()
    for c in ("is_best_is", "is_best_oos", "is_best_calmar"):
        main_g[c] = 0
    main_g.loc[i_bi, "is_best_is"] = 1
    main_g.loc[i_bo, "is_best_oos"] = 1
    main_g.loc[i_bc, "is_best_calmar"] = 1
    best_is, best_oos, best_cal = main_g.loc[i_bi], main_g.loc[i_bo], main_g.loc[i_bc]

    flag_cols = ["rank_is_excess", "rank_oos_excess", "rank_oos_calmar",
                 "is_current", "is_best_is", "is_best_oos", "is_best_calmar"]
    for frame in (strict_g, aux):
        for c in flag_cols:
            if c not in frame:
                frame[c] = -1
    out_df = pd.concat([main_g, strict_g, aux], ignore_index=True)
    lead_cols = ["grid", "mode", "up_thresh_pct", "window_days", "rev_r5_pct", "deep_r5_pct"]
    mid = [c for c in out_df.columns if c not in set(lead_cols) | set(flag_cols)]
    out_df = out_df[lead_cols + mid + flag_cols]
    OUT_CSV.parent.mkdir(parents=True, exist_ok=True)
    out_df.to_csv(OUT_CSV, index=False, float_format="%.6f")
    print(f"[输出] {OUT_CSV.relative_to(ROOT)}  {out_df.shape[0]} 行 × {out_df.shape[1]} 列")

    cur = main_g[main_g["is_current"] == 1].iloc[0]

    def cfg(a) -> str:
        if a.get("mode") == "strict":
            return f"strict {a['up_thresh_pct']:g}%/{int(a['window_days'])}日"
        return f"{a['up_thresh_pct']:g}%/{int(a['window_days'])}日/r5≥{a['rev_r5_pct']:g}%"

    # ---------------- 【三】脏数据敏感性
    print("\n" + "=" * 110)
    print("【三】伪交易日敏感性（指数级，T+1，5bp，样本外超额 pp）")
    print("=" * 110)
    print(f"  {'配置':<26}{'剔除后':>10}{'未剔除':>10}{'Δ':>9}")
    for label, row in (("当前 3%/20日/r5≥2%", cur), (f"样本外最优 {cfg(best_oos)}", best_oos),
                       (f"样本内最优 {cfg(best_is)}", best_is)):
        print(f"  {label:<26}{row['oos_excess_pp']:>10.2f}{row['oos_excess_pp_raw']:>10.2f}"
              f"{row['delta_oos_excess_pp']:>9.2f}")
    dlt = main_g["delta_oos_excess_pp"]
    print(f"\n  100 格 Δ：min {dlt.min():+.2f} / max {dlt.max():+.2f} / 均值 {dlt.mean():+.2f} pp；"
          f"|Δ|>5pp 的格数 {int((dlt.abs() > 5).sum())}/100")
    i_max = dlt.abs().idxmax()
    print(f"  最大敏感格 {cfg(main_g.loc[i_max])}：剔除后 {main_g.loc[i_max, 'oos_excess_pp']:+.2f}pp"
          f" → 未剔除 {main_g.loc[i_max, 'oos_excess_pp_raw']:+.2f}pp（Δ {dlt.loc[i_max]:+.2f}pp）")
    bh_raw = bench_only(df_raw.sort_values("trade_date").reset_index(drop=True), FULL_START, OOS_END)
    bh_raw_conv_b = bench_only(sd_lr, FULL_START, OOS_END)
    print(f"  BH 全段：剔除后 {bh['total']:+.2%}；未剔除 {bh_raw_conv_b['total']:+.2%}"
          f"（Δ {bh['total'] - bh_raw_conv_b['total']:+.2%}，同一窗口约定下仅伪交易日的影响）")

    # ---------------- 【四】状态有效性
    print("\n" + "=" * 110)
    print("【四】闸门状态有效性：各状态下「次日」指数收益（当前配置 3%/20日/r5≥2%）")
    print("=" * 110)
    cur_states, cur_sorted = classify_gate(df, CURRENT["up_thresh_pct"], int(CURRENT["window_days"]),
                                           CURRENT["deep_r5_pct"], CURRENT["rev_r5_pct"])
    for label, s, e in (("样本内", IS_START, IS_END), ("样本外", OOS_START, OOS_END)):
        eff = state_effectiveness(cur_states, cur_sorted, s, e)
        print(f"\n  {label}：{'状态':<6}{'天数':>6}{'次日均值bp':>12}{'次日年化':>11}{'上涨占比':>10}")
        for _, r in eff.iterrows():
            print(f"        {r['state']:<6}{int(r['days']):>6}{r['fwd_mean_bp']:>12.1f}"
                  f"{r['fwd_annual']:>11.1%}{r['hit']:>10.1%}")
        a = eff.loc[eff["state"] == "上涨", "fwd_mean_bp"].iloc[0]
        b = eff.loc[eff["state"] != "上涨", "fwd_mean_bp"]
        print(f"        上涨 vs 非上涨 次日均值差：{a - b.mean():+.1f}bp/日")

    # ---------------- 【五】Top 榜
    print("\n" + "=" * 110)
    print("【五】样本外榜单（T+1，5bp）")
    print("=" * 110)
    print(f"  按超额 Top10：{'阈值':>6}{'窗口':>6}{'r5':>6}{'超额pp':>9}{'累计':>9}{'回撤':>9}"
          f"{'夏普':>7}{'卡玛':>7}{'持仓':>7}{'NW_t':>7}{'IS排名':>8}")
    for i, r in main_g.nlargest(10, "oos_excess_pp").iterrows():
        print(f"  {'':>14}{r['up_thresh_pct']:>6g}{int(r['window_days']):>6}{r['rev_r5_pct']:>6g}"
              f"{r['oos_excess_pp']:>9.2f}{r['oos_total']:>9.2%}{r['oos_mdd']:>9.2%}"
              f"{r['oos_sharpe']:>7.2f}{r['oos_calmar']:>7.2f}{r['oos_hold']:>7.1%}"
              f"{r['oos_excess_daily_nw_t']:>7.2f}{int(main_g.loc[i, 'rank_is_excess']):>8}")
    print(f"\n  按样本外卡玛 Top10：{'阈值':>6}{'窗口':>6}{'r5':>6}{'卡玛':>7}{'年化':>9}{'回撤':>9}"
          f"{'夏普':>7}{'超额pp':>9}{'持仓':>7}{'交易':>6}")
    for i, r in main_g.nlargest(10, "oos_calmar").iterrows():
        print(f"  {'':>14}{r['up_thresh_pct']:>6g}{int(r['window_days']):>6}{r['rev_r5_pct']:>6g}"
              f"{r['oos_calmar']:>7.2f}{r['oos_annual']:>9.2%}{r['oos_mdd']:>9.2%}"
              f"{r['oos_sharpe']:>7.2f}{r['oos_excess_pp']:>9.2f}{r['oos_hold']:>7.1%}{r['oos_trades']:>6}")

    # ---------------- 【六】旧严格 vs 新宽松
    print("\n" + "=" * 110)
    print("【六】旧严格语义 vs 新宽松语义（同一 up_thresh × window，样本外，5bp）")
    print("=" * 110)
    keys = ["up_thresh_pct", "window_days"]
    loose_best = (main_g.sort_values("oos_calmar", ascending=False).groupby(keys, as_index=False).first())
    loose_best = loose_best[keys + ["rev_r5_pct", "oos_calmar", "oos_sharpe", "oos_mdd",
                                    "oos_excess_pp", "oos_hold"]].rename(columns={
        "rev_r5_pct": "loose_best_rev", "oos_calmar": "loose_best_calmar",
        "oos_sharpe": "loose_best_sharpe", "oos_mdd": "loose_best_mdd",
        "oos_excess_pp": "loose_best_excess", "oos_hold": "loose_best_hold"})
    loose_cur = main_g[main_g["rev_r5_pct"] == CURRENT["rev_r5_pct"]][
        keys + ["oos_calmar", "oos_sharpe", "oos_mdd"]].rename(columns={
        "oos_calmar": "loose_cur_calmar", "oos_sharpe": "loose_cur_sharpe",
        "oos_mdd": "loose_cur_mdd"})
    cmp = (strict_g[keys + ["oos_calmar", "oos_sharpe", "oos_mdd", "oos_excess_pp", "oos_hold"]]
           .merge(loose_best, on=keys, how="left").merge(loose_cur, on=keys, how="left"))
    print(f"  {'阈值':>6}{'窗口':>6}{'严格卡玛':>10}{'宽松优卡玛':>12}{'严格MDD':>10}{'宽松MDD':>10}"
          f"{'严格持有':>10}{'宽松持有':>10}{'宽松最优rev':>12}{'严格超额':>10}{'宽松超额':>10}")
    for _, r in cmp.sort_values("oos_calmar", ascending=False).iterrows():
        print(f"  {r['up_thresh_pct']:>6g}{int(r['window_days']):>6}{r['oos_calmar']:>10.2f}"
              f"{r['loose_best_calmar']:>12.2f}{r['oos_mdd']:>10.2%}{r['loose_best_mdd']:>10.2%}"
              f"{r['oos_hold']:>10.1%}{r['loose_best_hold']:>10.1%}{r['loose_best_rev']:>12g}"
              f"{r['oos_excess_pp']:>10.2f}{r['loose_best_excess']:>10.2f}")
    wins_best = int((cmp["oos_calmar"] > cmp["loose_best_calmar"]).sum())
    wins_cur = int((cmp["oos_calmar"] > cmp["loose_cur_calmar"]).sum())
    print(f"\n  严格 > 宽松(每个阈值×窗口下取卡玛最好的 rev_r5)：{wins_best}/20 对")
    print(f"  严格 > 宽松(固定当前 rev_r5=2%)：{wins_cur}/20 对")
    print(f"  均值：严格卡玛 {cmp['oos_calmar'].mean():.2f} / 中位 {cmp['oos_calmar'].median():.2f}  vs  "
          f"宽松最佳 卡玛 {cmp['loose_best_calmar'].mean():.2f} / {cmp['loose_best_calmar'].median():.2f}")
    print(f"  均值：严格夏普 {cmp['oos_sharpe'].mean():.2f}  vs  宽松最佳夏普 {cmp['loose_best_sharpe'].mean():.2f}"
          f"；严格 MDD {cmp['oos_mdd'].mean():.2%} vs 宽松 {cmp['loose_best_mdd'].mean():.2%}")
    print(f"  均值：严格持仓 {cmp['oos_hold'].mean():.1%} vs 宽松 {cmp['loose_best_hold'].mean():.1%}")
    print("  注：严格语义每格只有主维度，宽松语义每格多了 MA5/MA20 与 r5 两个开仓理由 → 持仓更高、回撤更大。")
    print(f"  同一组合的样本内表现：严格 3%/20日 样本内超额 "
          f"{strict_g[(strict_g['up_thresh_pct'] == 3.0) & (strict_g['window_days'] == 20)]['is_excess_pp'].iloc[0]:+.2f}pp"
          f" vs 宽松 3%/20日/r5≥2% {cur['is_excess_pp']:+.2f}pp（样本内宽松更好，样本外严格更好 = 状态反转）")

    # ---------------- 【六b】全段卡玛对比
    print("\n" + "=" * 110)
    print(f"【六b】全段（448 日）卡玛对比：旧严格 vs 新宽松（5bp；BH 全段卡玛 {bh['calmar']:.2f}）")
    print("=" * 110)
    keys2 = ["up_thresh_pct", "window_days"]
    lb = (main_g.sort_values("full_calmar", ascending=False).groupby(keys2, as_index=False).first())
    lb = lb[keys2 + ["rev_r5_pct", "full_calmar", "full_mdd", "full_excess_pp"]].rename(
        columns={"rev_r5_pct": "loose_rev", "full_calmar": "loose_full_calmar",
                 "full_mdd": "loose_full_mdd", "full_excess_pp": "loose_full_excess"})
    cmpf = (strict_g[keys2 + ["full_calmar", "full_mdd", "full_excess_pp"]]
            .merge(lb, on=keys2, how="left"))
    print(f"  {'阈值':>6}{'窗口':>6}{'严格卡玛':>10}{'宽松卡玛':>10}{'严格超额':>10}{'宽松超额':>10}"
          f"{'严格MDD':>10}{'宽松MDD':>10}")
    for _, r in cmpf.sort_values("full_calmar", ascending=False).iterrows():
        print(f"  {r['up_thresh_pct']:>6g}{int(r['window_days']):>6}{r['full_calmar']:>10.2f}"
              f"{r['loose_full_calmar']:>10.2f}{r['full_excess_pp']:>10.2f}"
              f"{r['loose_full_excess']:>10.2f}{r['full_mdd']:>10.2%}{r['loose_full_mdd']:>10.2%}")
    wins_full = int((cmpf["full_calmar"] > cmpf["loose_full_calmar"]).sum())
    print(f"\n  全段 严格卡玛 > 宽松卡玛：{wins_full}/20 对；"
          f"严格 > BH {int((cmpf['full_calmar'] > bh['calmar']).sum())}/20，"
          f"宽松 > BH {int((cmpf['loose_full_calmar'] > bh['calmar']).sum())}/20")
    print(f"  严格卡玛 均值 {cmpf['full_calmar'].mean():.2f} / 中位 {cmpf['full_calmar'].median():.2f}"
          f"；宽松 {cmpf['loose_full_calmar'].mean():.2f} / 中位 {cmpf['loose_full_calmar'].median():.2f}")
    print(f"  网格内全段卡玛 > BH({bh['calmar']:.2f}) 的格数：宽松 "
          f"{int((main_g['full_calmar'] > bh['calmar']).sum())}/100，"
          f"严格 {int((strict_g['full_calmar'] > bh['calmar']).sum())}/20")
    print(f"  网格内全段卡玛最高：宽松 {main_g['full_calmar'].max():.2f}"
          f"（{cfg(main_g.loc[main_g['full_calmar'].idxmax()])}），"
          f"严格 {strict_g['full_calmar'].max():.2f}"
          f"（{cfg(strict_g.loc[strict_g['full_calmar'].idxmax()])}）")

    # ---------------- 【七】一维边际
    print("\n" + "=" * 110)
    print("【七】一维边际（固定当前配置其余两维）：越平越可信")
    print("=" * 110)
    for dim, vals in (("up_thresh_pct", UP_THRESHOLDS), ("window_days", WINDOWS),
                      ("rev_r5_pct", REV_R5S)):
        print(f"\n  {dim}:")
        print(f"  {'取值':>8}{'样本外超额pp':>14}{'样本内超额pp':>14}{'样本外卡玛':>12}"
              f"{'样本外回撤':>12}{'样本外持仓':>12}")
        xs, cs = [], []
        for v in vals:
            m = pd.Series(True, index=main_g.index)
            for other, cv in (("up_thresh_pct", CURRENT["up_thresh_pct"]),
                              ("window_days", CURRENT["window_days"]),
                              ("rev_r5_pct", CURRENT["rev_r5_pct"])):
                if other != dim:
                    m &= main_g[other] == cv
            m &= main_g[dim] == v
            row = main_g[m].iloc[0]
            xs.append(float(row["oos_excess_pp"]))
            cs.append(float(row["oos_calmar"]))
            mark = "  ← 当前" if float(v) == float(CURRENT[dim]) else ""
            print(f"  {v:>8g}{row['oos_excess_pp']:>14.2f}{row['is_excess_pp']:>14.2f}"
                  f"{row['oos_calmar']:>12.2f}{row['oos_mdd']:>12.2%}{row['oos_hold']:>12.1%}{mark}")
        print(f"  超额跨度 {max(xs) - min(xs):.2f}pp，相邻最大跳变 "
              f"{max(abs(xs[i + 1] - xs[i]) for i in range(len(xs) - 1)):.2f}pp；"
              f"卡玛跨度 {max(cs) - min(cs):.2f}")

    # ---------------- 【八】平坦区间
    print("\n" + "=" * 110)
    print("【八】平坦区间判定（样本外）")
    print("=" * 110)

    def span_text(frame: pd.DataFrame, col: str) -> str:
        frame = frame[np.isfinite(frame[col])]
        if frame.empty:
            return "空集（无任何格满足）"
        return (f"{len(frame)} 格；阈值 {frame['up_thresh_pct'].min():g}~{frame['up_thresh_pct'].max():g}%"
                f"（跨度 {frame['up_thresh_pct'].max() - frame['up_thresh_pct'].min():g}pp）；"
                f"窗口 {int(frame['window_days'].min())}~{int(frame['window_days'].max())} 日；"
                f"r5 {frame['rev_r5_pct'].min():g}~{frame['rev_r5_pct'].max():g}%；"
                f"{col} {frame[col].min():+.2f}~{frame[col].max():+.2f}"
                f"（跨度 {frame[col].max() - frame[col].min():.2f}）")

    best_x = float(main_g["oos_excess_pp"].max())
    plateau = main_g[main_g["oos_excess_pp"] >= best_x - PLATEAU_TOL_PP]
    pos_only = main_g[main_g["oos_excess_pp"] > 0]
    beat_calmar = main_g[main_g["full_calmar"] > bh["calmar"]]
    print(f"  超额·定义A（距样本外最优 {PLATEAU_TOL_PP:g}pp 内）：{span_text(plateau, 'oos_excess_pp')}")
    print(f"  超额·定义B（样本外超额 > 0，跑赢 BH）：{span_text(pos_only, 'oos_excess_pp')}")
    print(f"  卡玛·定义C（全段卡玛 > BH {bh['calmar']:.2f}）：{span_text(beat_calmar, 'full_calmar')}")
    print(f"\n  100 格样本外超额：min {main_g['oos_excess_pp'].min():+.2f} / "
          f"中位 {main_g['oos_excess_pp'].median():+.2f} / max {main_g['oos_excess_pp'].max():+.2f}pp；"
          f"极差 {main_g['oos_excess_pp'].max() - main_g['oos_excess_pp'].min():.2f}pp，"
          f"标准差 {main_g['oos_excess_pp'].std(ddof=1):.2f}pp")
    print(f"  跑赢 BH 的格数 {int((main_g['oos_excess_pp'] > 0).sum())}/100"
          f"（>+5pp：{int((main_g['oos_excess_pp'] > 5).sum())}/100，"
          f">+10pp：{int((main_g['oos_excess_pp'] > 10).sum())}/100）")
    print(f"  100 格样本外卡玛：min {main_g['oos_calmar'].min():.2f} / "
          f"中位 {main_g['oos_calmar'].median():.2f} / max {main_g['oos_calmar'].max():.2f}"
          f"（同期 BH 卡玛 {bh_oos['calmar']:.2f}，样本外卡玛 > BH 的格数 "
          f"{int((main_g['oos_calmar'] > bh_oos['calmar']).sum())}/100）")
    print(f"  100 格全段卡玛：min {main_g['full_calmar'].min():.2f} / "
          f"中位 {main_g['full_calmar'].median():.2f} / max {main_g['full_calmar'].max():.2f}"
          f"（同期 BH 卡玛 {bh['calmar']:.2f}，全段卡玛 > BH 的格数 "
          f"{int((main_g['full_calmar'] > bh['calmar']).sum())}/100）")
    rho, rho_p = stats.spearmanr(main_g["is_excess_pp"], main_g["oos_excess_pp"])
    print(f"\n  样本内 vs 样本外 超额 Spearman ρ = {rho:+.3f} (p={rho_p:.4f})"
          f"｜100 格高度重叠，ρ 的有效自由度远小于 100")

    neigh = main_g[
        main_g["up_thresh_pct"].between(CURRENT["up_thresh_pct"] - 1.0, CURRENT["up_thresh_pct"] + 1.0)
        & main_g["window_days"].between(CURRENT["window_days"] - 5, CURRENT["window_days"] + 5)
        & main_g["rev_r5_pct"].between(CURRENT["rev_r5_pct"] - 1.0, CURRENT["rev_r5_pct"] + 1.0)]
    print(f"  当前配置邻域（阈值±1pp/窗口±5日/r5±1pp，{len(neigh)} 格）："
          f"超额 {neigh['oos_excess_pp'].min():+.2f}~{neigh['oos_excess_pp'].max():+.2f}pp，"
          f"卡玛 {neigh['oos_calmar'].min():.2f}~{neigh['oos_calmar'].max():.2f}")
    print("\n  当前配置在每一维上的位置（卡玛·定义C 的覆盖范围）：")
    for dim, label, cv in (("up_thresh_pct", "上涨阈值", CURRENT["up_thresh_pct"]),
                           ("window_days", "窗口", CURRENT["window_days"]),
                           ("rev_r5_pct", "r5 反转阈值", CURRENT["rev_r5_pct"])):
        vals = sorted(beat_calmar[dim].unique().tolist())
        if not vals:
            print(f"    {label}: 定义C 为空集（无任何格的全段卡玛超过 BH）。")
            continue
        interior = float(min(vals)) < float(cv) < float(max(vals))
        print(f"    {label}: 当前={cv:g}；定义C 覆盖 {[float(v) for v in vals]}，"
              f"当前{'位于区间内部' if interior else ('位于区间边界' if float(cv) in [float(v) for v in vals] else '不在区间内')}")
    print(f"    （对照）定义B（样本外超额>0）覆盖：阈值 "
          f"{sorted(pos_only['up_thresh_pct'].unique().tolist())}，窗口 "
          f"{sorted(int(x) for x in pos_only['window_days'].unique())}，r5 "
          f"{sorted(pos_only['rev_r5_pct'].unique().tolist())}")

    # ---------------- 【九】TM
    print("\n" + "=" * 110)
    print("【九】Treynor-Mazuy 择时检验：r_p = α + β·r_m + γ·r_m²（样本外，T+1，5bp，rf=0）")
    print("=" * 110)
    pool = pd.concat([main_g.nlargest(5, "oos_excess_pp"),
                      main_g.nlargest(2, "oos_calmar"),
                      main_g[main_g["is_current"] == 1]])
    pool = pool.drop_duplicates(subset=["up_thresh_pct", "window_days", "rev_r5_pct"])
    tm_rows = []
    for _, r in pool.iterrows():
        states, sd = classify_gate(df, float(r["up_thresh_pct"]), int(r["window_days"]),
                                   float(r["deep_r5_pct"]), float(r["rev_r5_pct"]))
        bt = backtest_gate(states, sd, OOS_START, OOS_END)
        res = treynor_mazuy(bt["net_ret"], bt["bench_ret"])
        res.update({"up_thresh_pct": r["up_thresh_pct"], "window_days": int(r["window_days"]),
                    "rev_r5_pct": r["rev_r5_pct"], "oos_excess_pp": float(r["oos_excess_pp"]),
                    "is_current": int(r["is_current"])})
        tm_rows.append(res)
    tm = pd.DataFrame(tm_rows).sort_values("oos_excess_pp", ascending=False)
    print(f"  {'配置':<20}{'超额pp':>8}{'α年化':>9}{'β':>7}{'γ':>11}{'t(γ)':>8}{'p(γ)':>8}"
          f"{'β_up':>7}{'β_dn':>7}{'R²':>6}")
    for _, r in tm.iterrows():
        label = f"{r['up_thresh_pct']:g}%/{int(r['window_days'])}日/{r['rev_r5_pct']:g}%"
        star = "*" if r["is_current"] == 1 else " "
        print(f"  {label + star:<20}{r['oos_excess_pp']:>8.2f}{r['alpha_annual']:>9.2%}"
              f"{r['beta']:>7.2f}{r['gamma']:>11.2f}{r['t_gamma']:>8.2f}{r['p_gamma']:>8.3f}"
              f"{r['beta_up']:>7.2f}{r['beta_down']:>7.2f}{r['r2']:>6.3f}")
    print("  读法：γ>0 且 p<0.05 = 显著择时能力；β_up > β_dn = 涨时加暴露、跌时降暴露，亦为择时证据。")
    print("        *=当前生产配置。γ 用 OLS t 检验（无 HAC 修正，日频重叠风险低）。")

    # ---------------- 【十】深跌敏感性
    print("\n" + "=" * 110)
    print(f"【十】深跌保护敏感度（固定 {cfg(cur)}，只动 deep_r5；仅参考，不参与选参）")
    print("=" * 110)
    print(f"  {'deep_r5':>9}{'样本外超额pp':>14}{'样本外卡玛':>12}{'样本内超额pp':>14}"
          f"{'样本外累计':>12}{'样本外回撤':>12}")
    for _, r in aux.iterrows():
        mark = "  ← 当前" if float(r["deep_r5_pct"]) == DEEP_R5_FIXED else ""
        print(f"  {r['deep_r5_pct']:>9g}{r['oos_excess_pp']:>14.2f}{r['oos_calmar']:>12.2f}"
              f"{r['is_excess_pp']:>14.2f}{r['oos_total']:>12.2%}{r['oos_mdd']:>12.2%}{mark}")

    # ---------------- 【十一】可引用
    print("\n" + "=" * 110)
    print("【十一】可直接引用的数字（与 CSV 一致；均为剔除伪交易日 + T+1 口径）")
    print("=" * 110)
    print(f"数据：{source}，{len(df)} 行（原 {len(df_raw)} 行，剔除 {len(removed)} 条 {removed}），"
          f"样本外 {int(cur['oos_days'])} 个交易日")
    print(f"⑧ BH：样本内 {bh_is['total']:+.2%}（卡玛 {bh_is['calmar']:.2f}）/ "
          f"样本外 {bh_oos['total']:+.2%}（卡玛 {bh_oos['calmar']:.2f}）/ 全段 {bh['total']:+.2%}"
          f"（卡玛 {bh['calmar']:.2f}，波动 {bh['vol']:.2%}，MDD {bh['mdd']:.2%}）")
    print(f"① 当前 {cfg(cur)}：样本外超额 {cur['oos_excess_pp']:+.2f}pp（5bp）/ "
          f"{backtest_gate(loose_states, sd_l, OOS_START, OOS_END, cost=0.0)['excess_pp']:+.2f}pp（0bp）；"
          f"累计 {cur['oos_total']:+.2%}，年化 {cur['oos_annual']:+.2%}，回撤 {cur['oos_mdd']:.2%}，"
          f"夏普 {cur['oos_sharpe']:.2f}，卡玛 {cur['oos_calmar']:.2f}，持仓 {cur['oos_hold']:.1%}")
    print(f"② 样本外超额最优 {cfg(best_oos)}：{best_oos['oos_excess_pp']:+.2f}pp"
          f"（NW-t {best_oos['oos_excess_daily_nw_t']:.2f}，样本内排名 {int(best_oos['rank_is_excess'])}/100，"
          f"卡玛 {best_oos['oos_calmar']:.2f}）")
    print(f"③ 样本外卡玛最优 {cfg(best_cal)}：卡玛 {best_cal['oos_calmar']:.2f}，"
          f"年化 {best_cal['oos_annual']:+.2%}，回撤 {best_cal['oos_mdd']:.2%}，"
          f"超额 {best_cal['oos_excess_pp']:+.2f}pp")
    print(f"④ 三方（5bp，全段）：BH 卡玛 {bh['calmar']:.2f}/夏普 {bh['sharpe']:.2f}/回撤 {bh['mdd']:.2%}；"
          f"新宽松 卡玛 {r_l['calmar']:.2f}/夏普 {r_l['sharpe']:.2f}/回撤 {r_l['mdd']:.2%}；"
          f"旧严格 卡玛 {r_s['calmar']:.2f}/夏普 {r_s['sharpe']:.2f}/回撤 {r_s['mdd']:.2%}")
    print(f"⑤ 未来函数虚增：全段 新宽松 "
          f"{backtest_gate(loose_states, sd_l, FULL_START, OOS_END, cost=0.0, lag=0)['total']:+.2%} → "
          f"{backtest_gate(loose_states, sd_l, FULL_START, OOS_END, cost=0.0, lag=1)['total']:+.2%}"
          f"（虚增 {backtest_gate(loose_states, sd_l, FULL_START, OOS_END, cost=0.0, lag=0)['total'] - backtest_gate(loose_states, sd_l, FULL_START, OOS_END, cost=0.0, lag=1)['total']:.2%}pp）；"
          f"旧严格 "
          f"{backtest_gate(strict_states, sd_s, FULL_START, OOS_END, cost=0.0, lag=0)['total']:+.2%} → "
          f"{backtest_gate(strict_states, sd_s, FULL_START, OOS_END, cost=0.0, lag=1)['total']:+.2%}")
    print(f"⑥ 网格：样本外跑赢 BH {int((main_g['oos_excess_pp'] > 0).sum())}/100 格"
          f"（中位 {main_g['oos_excess_pp'].median():+.2f}pp）；"
          f"全段卡玛 > BH({bh['calmar']:.2f}) {int((main_g['full_calmar'] > bh['calmar']).sum())}/100 格；"
          f"严格胜宽松 样本外 {wins_cur}/20 对、全段 {wins_full}/20 对"
          f"（严格全段卡玛均值 {cmpf['full_calmar'].mean():.2f} vs 宽松 {cmpf['loose_full_calmar'].mean():.2f}）")
    print(f"⑦ 平坦区间：超额·定义A {span_text(plateau, 'oos_excess_pp')}")
    print(f"           超额·定义B {span_text(pos_only, 'oos_excess_pp')}")
    print(f"           卡玛·定义C {span_text(beat_calmar, 'full_calmar')}")
    print("=" * 110)


if __name__ == "__main__":
    main()
