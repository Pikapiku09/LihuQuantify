"""P7-1：周K聚合——日线面板 → 周频面板。

聚合规则（W-FRI，周五收周）：
  open=周首开盘  high=周内最高  low=周内最低  close=周五收盘
  vol/amount=周内合计  周内日收益和/日内收益和 → 供周频因子使用
用法：python scripts/build_weekly_panel.py [--full]（默认用 390 只面板，--full 用全量面板）
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import duckdb
from loguru import logger


def build(src: str, dst: str) -> None:
    df = duckdb.query(f"""
        SELECT ts_code, trade_date, open, high, low, close, vol, amount,
               close / NULLIF(lag(close) OVER (PARTITION BY ts_code ORDER BY trade_date), 0) - 1 AS day_ret,
               ln(close / NULLIF(open, 0)) AS day_intraday
        FROM '{src}'
    """).fetchdf()
    df["trade_date"] = __import__("pandas").to_datetime(df["trade_date"])
    df["week"] = df["trade_date"].dt.to_period("W-FRI").dt.end_time.dt.date
    g = df.groupby(["ts_code", "week"], as_index=False).agg(
        trade_date=("trade_date", "max"),
        open=("open", "first"), high=("high", "max"), low=("low", "min"),
        close=("close", "last"), vol=("vol", "sum"), amount=("amount", "sum"),
        week_ret=("day_ret", lambda s: (1 + s.fillna(0)).prod() - 1),
        week_intraday=("day_intraday", "sum"),
    )
    g = g.drop(columns=["week"]).sort_values(["ts_code", "trade_date"]).reset_index(drop=True)
    con = duckdb.connect()
    con.register("w", g)
    con.execute(f"COPY w TO '{dst}' (FORMAT PARQUET)")
    con.close()
    logger.info(f"周频面板：{len(g)} 行 × {g['ts_code'].nunique()} 只 → {dst}")


if __name__ == "__main__":
    full = "--full" in sys.argv
    src = str(ROOT / "data" / ("research_panel_full.parquet" if full else "research_panel.parquet"))
    dst = str(ROOT / "data" / ("weekly_panel_full.parquet" if full else "weekly_panel.parquet"))
    build(src, dst)
