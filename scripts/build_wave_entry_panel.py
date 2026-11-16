"""波级入场面板：为每个已清仓波取「买入那一刻」的基本面 / 估值 / 消息面快照。

为什么要这个
============
用户问题：赚钱的波段，与我买时的基本面和消息面有关吗？技术面好但基本面烂的票值不值得介入？

回答它需要**PIT（point-in-time）面板**：每个波只能用「买入日之前已经公告」的信息，
否则会引入前视偏差（公告日之后才知道的财报被当成了买入依据）。

PIT 铁律
--------
1. 财务指标：取 ``ann_date <= buy_date`` 中 ``end_date`` 最大的一期，**不是** buy_date 所属报告期
2. 估值分位：只用 buy_date 之前 1095 个自然日的历史值
3. 消息面：业绩预告同样按 ``ann_date <= buy_date`` 过滤，且只回看 60 个自然日

输出
----
``data/wave_entry_panel.parquet``：一行一个波，列含收益与全部入场特征。
本脚本可重复运行（Tushare 请求走文件缓存，12h TTL）。

用法
----
    python scripts/build_wave_entry_panel.py
"""

from __future__ import annotations

import sys
from datetime import date, timedelta
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from lihu_quantify.behavior.ingest import load_fills
from lihu_quantify.behavior.wave import build_waves
from lihu_quantify.data.tushare_client import TushareClient

ROOT = Path(__file__).resolve().parents[1]
FILLS = [
    Path("E:/Dsh_WorkSapce/Dify_Agents/投研反思/Table_1010.csv"),
    Path("E:/Dsh_WorkSapce/Dify_Agents/投研反思/Table_1254.csv"),
]
TOKEN_FILE = Path("E:/Dsh_WorkSapce/Dify_Agents/.dsh-invest/tushare.token")
OUT = ROOT / "data" / "wave_entry_panel.parquet"


def make_client() -> TushareClient:
    token = TOKEN_FILE.read_text(encoding="utf-8").strip()
    return TushareClient(token, cache_dir=ROOT / "data" / "cache" / "tushare")


def load_quotes() -> pd.DataFrame:
    """本地行情（80/81 覆盖），用于算进场涨幅与 5 日涨幅。"""
    import duckdb

    con = duckdb.connect(str(ROOT / "data" / "lihu_quant.duckdb"), read_only=True)
    df = con.execute(
        "SELECT ts_code, strftime(CAST(trade_date AS DATE), '%Y%m%d') AS trade_date, high, close "
        "FROM daily_quotes ORDER BY ts_code, trade_date"
    ).df()
    con.close()
    return df


def fetch_fundamentals(client: TushareClient, codes: list[str]) -> pd.DataFrame:
    frames = []
    for i, code in enumerate(codes, 1):
        try:
            df = client.query(
                "fina_indicator",
                {"ts_code": code},
                fields=["ts_code", "ann_date", "end_date", "roe", "netprofit_yoy",
                        "or_yoy", "grossprofit_margin", "debt_to_assets"],
            )
        except Exception as e:  # 单只失败不拖垮整体
            print(f"  [warn] fina_indicator {code}: {e}")
            continue
        if not df.empty:
            frames.append(df)
        if i % 20 == 0:
            print(f"  fina_indicator {i}/{len(codes)}")
    if not frames:
        return pd.DataFrame()
    out = pd.concat(frames, ignore_index=True)
    out["ann_date"] = out["ann_date"].astype(str)
    return out


def fetch_daily_basic(client: TushareClient, codes: list[str]) -> pd.DataFrame:
    frames = []
    for i, code in enumerate(codes, 1):
        try:
            df = client.query(
                "daily_basic",
                {"ts_code": code},
                fields=["ts_code", "trade_date", "pe_ttm", "pb", "total_mv"],
            )
        except Exception as e:
            print(f"  [warn] daily_basic {code}: {e}")
            continue
        if not df.empty:
            frames.append(df)
        if i % 20 == 0:
            print(f"  daily_basic {i}/{len(codes)}")
    if not frames:
        return pd.DataFrame()
    out = pd.concat(frames, ignore_index=True)
    out["trade_date"] = out["trade_date"].astype(str)
    return out


def fetch_forecast(client: TushareClient, codes: list[str]) -> pd.DataFrame:
    frames = []
    for i, code in enumerate(codes, 1):
        try:
            df = client.query(
                "forecast",
                {"ts_code": code},
                fields=["ts_code", "ann_date", "end_date", "type",
                        "p_change_min", "p_change_max"],
            )
        except Exception as e:
            print(f"  [warn] forecast {code}: {e}")
            continue
        if not df.empty:
            frames.append(df)
        if i % 20 == 0:
            print(f"  forecast {i}/{len(codes)}")
    if not frames:
        return pd.DataFrame()
    out = pd.concat(frames, ignore_index=True)
    out["ann_date"] = out["ann_date"].astype(str)
    return out


def ymd(d: date) -> str:
    return d.strftime("%Y%m%d")


def _f(v) -> float:
    """容错取数：None / 空串 / NaN 一律返回 NaN，不抛异常。"""
    if v is None:
        return float("nan")
    try:
        out = float(v)
    except (TypeError, ValueError):
        return float("nan")
    return out


def pb_percentile(hist: pd.DataFrame, buy: str) -> float:
    """buy 当日 PB 在之前 1095 自然日里的分位（0=最便宜）。"""
    if hist.empty:
        return float("nan")
    start = (date(int(buy[:4]), int(buy[4:6]), int(buy[6:])) - timedelta(days=1095)).strftime("%Y%m%d")
    w = hist[(hist["trade_date"] >= start) & (hist["trade_date"] <= buy)]
    if len(w) < 60:
        return float("nan")
    cur = hist[hist["trade_date"] <= buy]
    if cur.empty:
        return float("nan")
    v = cur.iloc[-1]["pb"]
    if pd.isna(v):
        return float("nan")
    return float((w["pb"] <= v).mean())


def main() -> int:
    print("=" * 70)
    print("1) 载入交割单 → 波")
    fills = load_fills([str(p) for p in FILLS])
    waves = [w for w in build_waves(fills) if not w.open]
    codes = sorted({w.ts_code for w in waves})
    print(f"   已清仓波 {len(waves)}｜标的 {len(codes)}")

    print("2) 本地行情")
    quotes = load_quotes()
    qmap = {c: g for c, g in quotes.groupby("ts_code")}
    print(f"   行情 {len(quotes):,} 行，覆盖 {len(qmap)} 只")

    client = make_client()
    print("3) 拉 PIT 财务（fina_indicator）")
    fina = fetch_fundamentals(client, codes)
    print(f"   {len(fina):,} 行")
    print("4) 拉估值（daily_basic）")
    basic = fetch_daily_basic(client, codes)
    print(f"   {len(basic):,} 行")
    print("5) 拉业绩预告（forecast）")
    fc = fetch_forecast(client, codes)
    print(f"   {len(fc):,} 行")

    fina_by = {c: g.sort_values("ann_date") for c, g in fina.groupby("ts_code")} if not fina.empty else {}
    basic_by = {c: g.sort_values("trade_date") for c, g in basic.groupby("ts_code")} if not basic.empty else {}
    fc_by = {c: g for c, g in fc.groupby("ts_code")} if not fc.empty else {}

    print("6) 逐波拼装面板")
    rows = []
    for w in waves:
        buy = w.buy_date
        bs = ymd(buy) if buy else None
        vol = sum(int(f.get("volume") or 0) for f in w.fills if f["side"] == "buy")
        buy_px = (w.buy_amount / vol) if vol else float("nan")

        # --- 价格衍生：进场涨幅 / 5 日涨幅 ---
        entry_gain = ret5 = float("nan")
        g = qmap.get(w.ts_code)
        if g is not None and bs:
            win = g[(g["trade_date"] >= bs) & (g["trade_date"] <= ymd(w.sell_date))]
            if not win.empty and buy_px == buy_px:
                entry_gain = float(win["high"].max()) / buy_px - 1
            after = g[g["trade_date"] >= bs].reset_index(drop=True)
            if len(after) >= 6 and buy_px == buy_px:
                ret5 = float(after.iloc[5]["close"]) / buy_px - 1

        # --- PIT 基本面 ---
        roe = np_yoy = or_yoy = gpm = debt = float("nan")
        fina_end = ""
        fina_ann_days = float("nan")
        fg = fina_by.get(w.ts_code)
        if fg is not None and bs:
            pit = fg[fg["ann_date"] <= bs]
            if not pit.empty:
                last = pit.iloc[-1]
                roe = _f(last.get("roe"))
                np_yoy = _f(last.get("netprofit_yoy"))
                or_yoy = _f(last.get("or_yoy"))
                gpm = _f(last.get("grossprofit_margin"))
                debt = _f(last.get("debt_to_assets"))
                fina_end = str(last.get("end_date", ""))
                ad = str(last.get("ann_date", ""))
                if len(ad) == 8 and buy is not None:
                    fina_ann_days = (buy - date(int(ad[:4]), int(ad[4:6]), int(ad[6:]))).days

        # --- PIT 估值 ---
        pb = pe = mv = pb_pct = float("nan")
        bg = basic_by.get(w.ts_code)
        if bg is not None and bs:
            hist = bg[bg["trade_date"] <= bs]
            if not hist.empty:
                last = hist.iloc[-1]
                pb = _f(last.get("pb"))
                pe = _f(last.get("pe_ttm"))
                mv = _f(last.get("total_mv"))
                pb_pct = pb_percentile(bg, bs)

        # --- PIT 消息面：近 60 日业绩预告 ---
        fc_type = ""
        fc_mid = float("nan")
        fc_days = float("nan")
        cg = fc_by.get(w.ts_code)
        if cg is not None and bs:
            lo = (buy - timedelta(days=120)).strftime("%Y%m%d")
            recent = cg[(cg["ann_date"] >= lo) & (cg["ann_date"] <= bs)]
            if not recent.empty:
                r = recent.sort_values("ann_date").iloc[-1]
                fc_type = str(r.get("type", ""))
                pmin = r.get("p_change_min")
                pmax = r.get("p_change_max")
                if pd.notna(pmin) and pd.notna(pmax):
                    fc_mid = float((pmin + pmax) / 2)
                fc_days = (buy - date(int(r["ann_date"][:4]), int(r["ann_date"][4:6]), int(r["ann_date"][6:]))).days

        rows.append({
            "ts_code": w.ts_code, "name": w.name,
            "buy_date": buy, "sell_date": w.sell_date,
            "ret": w.ret, "pnl": w.pnl, "cost": w.cost,
            "hold_days": w.hold_days, "n_buys": w.n_buys,
            "buy_px": buy_px, "entry_gain": entry_gain, "ret5": ret5,
            "roe": roe, "np_yoy": np_yoy, "or_yoy": or_yoy,
            "gpm": gpm, "debt": debt, "fina_end": fina_end,
            "fina_ann_days": fina_ann_days,
            "pb": pb, "pe_ttm": pe, "total_mv": mv, "pb_pct3y": pb_pct,
            "fc_type": fc_type, "fc_mid": fc_mid, "fc_days": fc_days,
        })

    panel = pd.DataFrame(rows)
    panel["win"] = (panel["pnl"] > 0).astype(int)
    OUT.parent.mkdir(parents=True, exist_ok=True)
    # 本环境没装 pyarrow，parquet 走 duckdb 写；失败则回退 CSV（两者后续都能读）
    try:
        import duckdb

        con = duckdb.connect()
        con.register("panel", panel)
        con.execute(f"COPY panel TO '{OUT.as_posix()}' (FORMAT PARQUET)")
        con.close()
        written = OUT
    except Exception as e:
        written = OUT.with_suffix(".csv")
        panel.to_csv(written, index=False, encoding="utf-8-sig")
        print(f"   [warn] parquet 写入失败({e})，回退 CSV")
    print(f"   写出 {written}｜{len(panel)} 行 x {len(panel.columns)} 列")
    print("\n7) 覆盖率")
    for c in ["entry_gain", "ret5", "roe", "np_yoy", "pb", "pb_pct3y", "fc_mid"]:
        print(f"   {c:10s} {panel[c].notna().sum():>4}/{len(panel)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
