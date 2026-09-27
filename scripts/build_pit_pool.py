"""PIT-1：退市股票清单 + PIT 池构建。

幸存者偏差修复：当前 strat_pool 是当前时点抽样的（不含 2019-2026 间退市股）。
方法：
  1. stock_basic(list_status='D') 拉退市清单，筛主板（60/00）+ 2019 后退市
  2. 拉退市股日线（2019-01~退市日）
  3. PIT 池 = 现有池 + 全部合格退市股（主板退市率低，全加比重无偏）
输出：data/pit_delisted.parquet（退市股日线）+ data/pit_pool.json（池清单）
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
from lihu_quantify.config import get_settings


def main() -> None:
    cfg = get_settings()
    raw = Path(cfg.tushare.token_file).read_text(encoding="utf-8").strip()
    m = re.search(r"token=([A-Za-z0-9]+)", raw)
    token = m.group(1) if raw.startswith("{") else raw
    client = TushareClient(token=token, cache_dir=cfg.tushare.cache_dir)

    # 1) 退市清单：stock_basic 无 delist_date → 用日线结束日判定（代码可能被复用）
    basic = client.query("stock_basic", {"list_status": "D"})
    basic["code"] = basic["ts_code"].str.replace("^T", "", regex=True)   # 去 T 前缀
    basic = basic[basic["code"].str.match(r"^(60|00)")]                  # 主板
    basic = basic[basic["list_date"] < "20181201"]                        # 研究期前上市
    logger.info(f"退市候选（主板+2019前上市）: {len(basic)} 只，逐只拉日线判定真实退市区间")

    # 2) 拉退市股日线（每股一次调用）
    frames = []
    codes = basic["ts_code"].tolist()
    for i, code in enumerate(codes):
        try:
            df = client.query("daily", {"ts_code": code, "start_date": "20190101", "end_date": "20260831"})
        except Exception as e:  # noqa: BLE001
            logger.warning(f"{code} 失败: {e}")
            continue
        if not df.empty:
            frames.append(df)
        if i % 20 == 0:
            logger.info(f"进度 {i}/{len(codes)}")
    all_df = pd.concat(frames, ignore_index=True)
    # 复权因子
    adj_frames = []
    for i, code in enumerate(codes):
        try:
            adj = client.query("adj_factor", {"ts_code": code, "start_date": "20190101", "end_date": "20260831"})
            if not adj.empty:
                adj_frames.append(adj)
        except Exception:
            continue
    adj_all = pd.concat(adj_frames, ignore_index=True)
    all_df = all_df.merge(adj_all, on=["ts_code", "trade_date"], how="left")
    all_df["adj_factor"] = all_df.groupby("ts_code")["adj_factor"].ffill().bfill()
    base = all_df.groupby("ts_code")["adj_factor"].last().rename("adj_base")
    all_df = all_df.merge(base, on="ts_code")
    for c in ("open", "high", "low", "close"):
        all_df[c] = all_df[c] * all_df["adj_factor"] / all_df["adj_base"]
    keep = ["ts_code", "trade_date", "open", "high", "low", "close", "vol", "amount"]
    all_df = all_df[keep]
    con = duckdb.connect()
    con.register("d", all_df)
    con.execute(f"COPY d TO '{ROOT / 'data' / 'pit_delisted.parquet'}' (FORMAT PARQUET)")
    con.close()
    logger.info(f"退市股日线: {len(all_df)} 行 × {all_df['ts_code'].nunique()} 只")

    # 3) PIT 池
    pool = json.loads((ROOT / "data" / "strat_pool_n200_L5_seed42.json").read_text())
    pit_pool = sorted(set(pool) | set(codes))
    (ROOT / "data" / "pit_pool.json").write_text(json.dumps(pit_pool))
    logger.info(f"PIT 池: {len(pit_pool)} 只（原 {len(pool)} + 退市 {len(codes)}）")


if __name__ == "__main__":
    main()
