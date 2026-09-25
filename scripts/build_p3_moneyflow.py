"""P3 数据管线：池内 198 只 moneyflow_dc（东财资金流）→ p3_moneyflow.parquet。"""
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

OUT = ROOT / "data" / "p3_moneyflow.parquet"
PROGRESS = ROOT / "data" / "p3_mf_progress.json"


def main() -> None:
    cfg = get_settings()
    raw = Path(cfg.tushare.token_file).read_text(encoding="utf-8").strip()
    m = re.search(r"token=([A-Za-z0-9]+)", raw)
    token = m.group(1) if raw.startswith("{") else raw
    client = TushareClient(token=token, cache_dir=cfg.tushare.cache_dir)

    panel = duckdb.query(f"SELECT DISTINCT ts_code FROM '{ROOT / 'data' / 'research_panel.parquet'}'").fetchdf()
    pool = panel["ts_code"].tolist()
    done = set(json.loads(PROGRESS.read_text())) if PROGRESS.exists() else set()
    logger.info(f"池 {len(pool)} 只，已完成 {len(done)}")

    frames = []
    if OUT.exists():
        frames.append(duckdb.query(f"SELECT * FROM '{OUT}'").fetchdf())

    for i, code in enumerate(pool):
        if code in done:
            continue
        try:
            df = client.query("moneyflow_dc", {"ts_code": code, "start_date": "20240101", "end_date": "20260831"})
        except Exception as e:  # noqa: BLE001
            logger.warning(f"{code} 失败: {e}")
            continue
        if not df.empty:
            frames.append(df)
        done.add(code)
        if i % 40 == 0:
            logger.info(f"进度 {i}/{len(pool)}")
            _flush(frames, done)

    _flush(frames, done, final=True)


def _flush(frames, done, final: bool = False) -> None:
    if not frames:
        return
    all_df = pd.concat(frames, ignore_index=True)
    keep = ["ts_code", "trade_date", "net_amount", "net_amount_rate",
            "buy_elg_amount", "buy_elg_amount_rate", "buy_lg_amount", "buy_sm_amount"]
    all_df = all_df[[c for c in keep if c in all_df.columns]].drop_duplicates(["ts_code", "trade_date"])
    con = duckdb.connect()
    con.register("mf", all_df)
    con.execute(f"COPY mf TO '{OUT}' (FORMAT PARQUET)")
    con.close()
    PROGRESS.write_text(json.dumps(sorted(done)))
    if final:
        logger.info(f"资金流完成：{len(all_df)} 条 → {OUT}")


if __name__ == "__main__":
    main()
