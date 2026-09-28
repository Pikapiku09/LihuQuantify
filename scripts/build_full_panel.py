"""扩池：沪深主板全量（2019前上市 2634 只 + 190 退市股）研究面板。

输出 data/research_panel_full.parquet（与现有 390 只面板同口径：2019-01~2026-08 前复权）
断点续跑：progress 记录已完成股票。预计 API ~5600 次。
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import duckdb
import pandas as pd
from loguru import logger

from lihu_quantify.data.tushare_client import TushareClient
from lihu_quantify.data.duckdb_store import DuckDBStore
from lihu_quantify.config import get_settings

START, END = "20190101", "20260831"
OUT = ROOT / "data" / "research_panel_full.parquet"
PROGRESS = ROOT / "data" / "full_panel_progress.json"


def main() -> None:
    cfg = get_settings()
    raw = Path(cfg.tushare.token_file).read_text(encoding="utf-8").strip()
    m = re.search(r"token=([A-Za-z0-9]+)", raw)
    token = m.group(1) if raw.startswith("{") else raw
    client = TushareClient(token=token, cache_dir=cfg.tushare.cache_dir)
    store = DuckDBStore(cfg.duckdb.path)

    # 池：主板现存(2019前上市) + 退市清单
    basic = client.query("stock_basic", {"list_status": "L"})
    hs = basic[basic["ts_code"].str.match(r"^(60|00)") & (basic["list_date"] < "20181201")]
    pool = hs["ts_code"].tolist()
    try:
        delisted = open(ROOT / "data" / "pit_delisted_list.txt").read().split()
        pool += [c for c in delisted if c not in pool]
    except FileNotFoundError:
        pass
    logger.info(f"目标池：{len(pool)} 只")

    done = set(json.loads(PROGRESS.read_text())) if PROGRESS.exists() else set()
    todo = [c for c in pool if c not in done]
    logger.info(f"已完成 {len(done)}，待拉 {len(todo)}")

    frames = []
    batch = []

    def flush(final=False):
        if not batch:
            return
        frames.extend(batch)
        batch.clear()
        all_df = pd.concat(frames, ignore_index=True)
        keep = [c for c in ("trade_date", "ts_code", "open", "high", "low", "close", "vol", "amount") if c in all_df.columns]
        all_df = all_df[keep].sort_values(["ts_code", "trade_date"]).reset_index(drop=True)
        con = duckdb.connect()
        con.register("p", all_df)
        con.execute(f"COPY p TO '{OUT}' (FORMAT PARQUET)")
        con.close()
        PROGRESS.write_text(json.dumps(sorted(set(pool) & set(all_df["ts_code"].unique()))))
        if final:
            logger.info(f"全量面板完成：{len(all_df)} 行 × {all_df['ts_code'].nunique()} 只 → {OUT}")

    for i, code in enumerate(todo):
        try:
            raw_q = client.query("daily", {"ts_code": code, "start_date": START, "end_date": END})
            if raw_q.empty or len(raw_q) < 200:
                continue
            adj = client.query("adj_factor", {"ts_code": code, "start_date": START, "end_date": END})
            df = raw_q.copy()
            if not adj.empty:
                adj = adj[["trade_date", "adj_factor"]]
                df["trade_date"] = df["trade_date"].astype(str)
                adj["trade_date"] = adj["trade_date"].astype(str)
                df = df.merge(adj, on="trade_date", how="left")
                df["adj_factor"] = df.groupby("ts_code")["adj_factor"].ffill().bfill()
                base = df["adj_factor"].iloc[-1]
                for col in ("open", "high", "low", "close"):
                    df[col] = df[col] * df["adj_factor"] / base
                df = df.drop(columns=["adj_factor"])
            batch.append(df)
        except Exception as e:  # noqa: BLE001
            logger.warning(f"{code} 失败: {e}")
            continue
        if (i + 1) % 100 == 0:
            logger.info(f"进度 {i+1}/{len(todo)}")
            flush()
    flush(final=True)


if __name__ == "__main__":
    main()
