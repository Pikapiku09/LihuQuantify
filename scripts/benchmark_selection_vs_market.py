"""选股风格基准对比：我买的票，在买的那一刻，落在全市场的哪个分位？

回答的问题
----------
「我选股时是否系统性偏好高 ROE / 高成长 / 低估值 / 小市值？」
这与「基本面能否预测盈亏」（H-003，已判 failed）是**两个不同问题**：

- H-003 问：基本面好的波是不是更赚？     → 答：不是
- 本脚本问：我是不是专挑基本面好的票买？ → 才是本条要测的

方法
----
对每个波，取买入日的横截面分位：
- 财务类（ROE / 净利同比 / 营收同比）：同报告期 cohort 内，且只算
  ``ann_date <= buy_date`` 的公司（PIT，避免用到尚未公告的财报）
- 估值类（PB / PE_TTM / 总市值）：买入日（或之前最近交易日）全市场横截面
分位 50% = 与市场中位数持平，>60% = 系统性偏高，<40% = 系统性偏低。

用法
----
    python scripts/benchmark_selection_vs_market.py

数据口径说明
------------
Tushare 的 ``fina_indicator`` 必须带 ``ts_code``（code=50101），**无法按报告期拉全市场横截面**，
``fina_indicator_vip`` 需要更高积分。因此财务类基准采用**全市场固定种子抽样池**
（默认 500 只，剔除 688/300/301/北交所/ST/退市），逐只拉全历史后按报告期重组横截面。
抽样池不是全市场，会带来抽样误差，所以分位计算的门槛设成「同报告期已公告公司 >=30 家」。
估值类（PB/PE/市值）用 ``daily_basic(trade_date=)`` 的真全市场横截面，不受抽样限制。
"""

from __future__ import annotations

import sys
from datetime import date, timedelta
from pathlib import Path

import numpy as np
import pandas as pd
from scipy import stats

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from lihu_quantify.data.tushare_client import TushareClient

ROOT = Path(__file__).resolve().parents[1]
PANEL = ROOT / "data" / "wave_entry_panel.parquet"
TOKEN_FILE = Path("E:/Dsh_WorkSapce/Dify_Agents/.dsh-invest/tushare.token")
OUT = ROOT / "data" / "selection_style_benchmark.parquet"

FIN_FIELDS = ["ts_code", "ann_date", "end_date", "roe", "netprofit_yoy", "or_yoy"]
BASIC_FIELDS = ["ts_code", "trade_date", "pb", "pe_ttm", "total_mv"]
SAMPLE_N = 500      # 财务基准的抽样池大小（固定种子，可复现）
SEED = 42


def read_panel(path: Path) -> pd.DataFrame:
    import duckdb

    con = duckdb.connect()
    df = con.execute(f"SELECT * FROM read_parquet('{path.as_posix()}')").df()
    con.close()
    return df


def write_panel(df: pd.DataFrame, path: Path) -> None:
    import duckdb

    con = duckdb.connect()
    con.register("panel", df)
    con.execute(f"COPY panel TO '{path.as_posix()}' (FORMAT PARQUET)")
    con.close()


def pct_rank(series: pd.Series, value: float) -> float:
    """value 在 series 中的分位（0~1）；样本不足 30 家返回 NaN。"""
    s = series.dropna()
    if len(s) < 30 or value != value:
        return float("nan")
    return float((s <= value).mean())


def norm_date(d) -> str:
    """把 date / 'YYYY-MM-DD' / 'YYYYMMDD' 统一成 'YYYYMMDD'。"""
    s = str(d).strip()
    if "-" in s:
        s = s.replace("-", "")
    if "/" in s:
        s = s.replace("/", "")
    return s[:8]


def sample_universe(client: TushareClient, n: int = 500, seed: int = 42) -> list[str]:
    """全市场固定种子抽样池：剔除 688/300/301/ST/退市，保证可复现。"""
    import duckdb

    con = duckdb.connect(str(ROOT / "data" / "lihu_quant.duckdb"), read_only=True)
    df = con.execute("SELECT ts_code, name, list_date, delist_date FROM stock_basic").df()
    con.close()
    if df.empty:
        df = client.query("stock_basic", {"list_status": "L"}, fields=["ts_code", "name", "list_date", "delist_date"])
    df = df[df["ts_code"].notna()]
    code = df["ts_code"].astype(str)
    mask = ~code.str.startswith(("688", "300", "301", "4", "8"))
    name = df["name"].astype(str)
    mask &= ~name.str.upper().str.contains("ST", na=False)
    mask &= df["delist_date"].isna() if "delist_date" in df.columns else True
    pool = sorted(code[mask].tolist())
    rng = np.random.default_rng(seed)
    pick = rng.choice(pool, size=min(n, len(pool)), replace=False)
    return sorted(pick.tolist())


def main() -> int:
    if not PANEL.exists():
        print("缺少 data/wave_entry_panel.parquet，先跑 build_wave_entry_panel.py")
        return 1
    panel = read_panel(PANEL)
    panel = panel[panel["roe"].notna() | panel["pb"].notna()].copy()
    panel["buy_date"] = panel["buy_date"].astype(str)
    print(f"样本：{len(panel)} 个波（含 PIT 基本面或估值）")

    token = TOKEN_FILE.read_text(encoding="utf-8").strip()
    client = TushareClient(token, cache_dir=ROOT / "data" / "cache" / "tushare")

    # ---- 1) 用全市场抽样池构造财务横截面（fina_indicator 不支持按 period 拉全市场）----
    periods = sorted({norm_date(p) for p in panel["fina_end"] if str(p) and str(p) != "nan"})
    print(f"报告期 {len(periods)} 个：{periods}")
    universe = sample_universe(client, n=SAMPLE_N)
    print(f"抽样池：{len(universe)} 只（固定种子 {SEED}，剔除 688/300/301/北交所/ST/退市）")
    frames = []
    for i, code in enumerate(universe, 1):
        try:
            df = client.query("fina_indicator", {"ts_code": code}, fields=FIN_FIELDS)
        except Exception as e:
            print(f"  [warn] fina_indicator {code}: {e}")
            continue
        if not df.empty:
            frames.append(df)
        if i % 100 == 0:
            print(f"  fina_indicator {i}/{len(universe)}")
    allfina = pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()
    cohort: dict[str, pd.DataFrame] = {}
    if not allfina.empty:
        allfina["ann_date"] = allfina["ann_date"].astype(str)
        allfina["end_date"] = allfina["end_date"].astype(str)
        for p in periods:
            sub = allfina[allfina["end_date"] == p]
            if len(sub):
                # 同一 (ts_code, end_date) 可能有多次公告（更正/调整），保留最后一次
                sub = (sub.sort_values("ann_date")
                          .drop_duplicates(subset=["ts_code"], keep="last"))
                cohort[p] = sub
                print(f"  {p}: 抽样池内 {len(sub):,} 家")

    # ---- 2) 按买入日拉全市场估值 ----
    dates = sorted(set(norm_date(x) for x in panel["buy_date"]))
    basic: dict[str, pd.DataFrame] = {}
    for i, d in enumerate(dates, 1):
        got = None
        cur = date(int(d[:4]), int(d[4:6]), int(d[6:]))
        for _ in range(8):  # 非交易日往前找
            key = cur.strftime("%Y%m%d")
            try:
                df = client.query("daily_basic", {"trade_date": key}, fields=BASIC_FIELDS)
            except Exception:
                df = pd.DataFrame()
            if not df.empty:
                got = (key, df)
                break
            cur -= timedelta(days=1)
        if got:
            basic[d] = got[1]
        if i % 25 == 0:
            print(f"  daily_basic {i}/{len(dates)}")
    print(f"  估值横截面覆盖 {len(basic)}/{len(dates)} 个买入日")

    # ---- 3) 逐波算分位 ----
    rows = []
    for _, r in panel.iterrows():
        bd = norm_date(r["buy_date"])
        out = {"ts_code": r["ts_code"], "name": r["name"], "buy_date": bd,
               "ret": r["ret"], "win": r["win"], "pnl": r["pnl"]}
        fe = norm_date(r["fina_end"])
        coh = cohort.get(fe)
        if coh is not None and len(coh):
            # PIT：同报告期内，只保留买入日之前已公告的公司
            alive = coh[coh["ann_date"] <= bd]
            # 注意列名不一致：面板里叫 np_yoy，fina_indicator 里叫 netprofit_yoy
            for panel_col, coh_col, dst in (
                ("roe", "roe", "roe_pct"),
                ("np_yoy", "netprofit_yoy", "yoy_pct"),
                ("or_yoy", "or_yoy", "or_pct"),
            ):
                out[dst] = pct_rank(alive[coh_col], r[panel_col])
        b = basic.get(bd)
        if b is not None and len(b):
            for src, dst in (("pb", "pb_pct"), ("pe_ttm", "pe_pct"), ("total_mv", "mv_pct")):
                out[dst] = pct_rank(b[src], r[src])
        rows.append(out)

    bench = pd.DataFrame(rows)
    write_panel(bench, OUT)
    print(f"\n写出 {OUT}｜{len(bench)} 行")

    # ---- 4) 结论：我的分位是不是 50% ----
    labels = {"roe_pct": "ROE 分位", "yoy_pct": "净利同比分位", "or_pct": "营收同比分位",
              "pb_pct": "PB 分位", "pe_pct": "PE 分位", "mv_pct": "市值分位"}
    print("\n" + "=" * 78)
    print("一、我买入时的横截面分位（50% = 与市场中位持平）")
    print(f"{'指标':14s} {'样本':>4s} {'中位分位':>8s} {'均值':>8s} {'vs50%的p值':>10s}  判定")
    for k, lab in labels.items():
        s = bench[k].dropna()
        if len(s) < 10:
            print(f"{lab:14s} {len(s):>4d}  样本不足")
            continue
        med = float(s.median())
        # Wilcoxon 符号秩：分位是否系统性偏离 50%
        try:
            p = float(stats.wilcoxon(s - 0.5, zero_method="wilcox").pvalue)
        except ValueError:
            p = float("nan")
        if med >= 0.60:
            verdict = "系统性偏高"
        elif med <= 0.40:
            verdict = "系统性偏低"
        else:
            verdict = "无明显偏好"
        star = " *" if p < 0.05 else ""
        print(f"{lab:14s} {len(s):>4d} {med:>8.1%} {s.mean():>8.1%} {p:>10.4f}  {verdict}{star}")

    print("\n" + "=" * 78)
    print("二、盈利波 vs 亏损波，选股分位是否不同（补充）")
    for k, lab in labels.items():
        a = bench[bench["win"] == 1][k].dropna()
        b = bench[bench["win"] == 0][k].dropna()
        if len(a) < 8 or len(b) < 8:
            continue
        p = float(stats.mannwhitneyu(a, b, alternative="two-sided").pvalue)
        print(f"{lab:14s} 盈利波 {a.median():.1%}(n={len(a)}) vs 亏损波 {b.median():.1%}(n={len(b)})  p={p:.4f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
