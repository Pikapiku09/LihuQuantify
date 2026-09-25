"""P2 事件数据管线：拉取池内 198 只的业绩预告（forecast）2019-2026。

输出 data/p2_events.parquet：ts_code / ann_date / type / p_change_mid / summary
事件去重：同一 (ts_code, end_date) 保留最后一条 ann_date（修正公告以最终为准）。
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

OUT = ROOT / "data" / "p2_events.parquet"
PROGRESS = ROOT / "data" / "p2_events_progress.json"


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
        old = duckdb.query(f"SELECT * FROM '{OUT}'").fetchdf()
        frames.append(old)

    for i, code in enumerate(pool):
        if code in done:
            continue
        try:
            df = client.query("forecast", {"ts_code": code, "start_date": "20190101", "end_date": "20260831"})
        except Exception as e:  # noqa: BLE001
            logger.warning(f"{code} 失败: {e}")
            continue
        if df.empty:
            done.add(code)
            continue
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
    # 同一报告期取最后公告（修正覆盖）
    all_df = all_df.sort_values("ann_date").groupby(["ts_code", "end_date"], as_index=False).last()
    all_df["p_change_mid"] = (all_df["p_change_min"].astype(float) + all_df["p_change_max"].astype(float)) / 2
    keep = ["ts_code", "ann_date", "end_date", "type", "p_change_min", "p_change_max", "p_change_mid"]
    all_df = all_df[[c for c in keep if c in all_df.columns]]
    con = duckdb.connect()
    con.register("ev", all_df)
    con.execute(f"COPY ev TO '{OUT}' (FORMAT PARQUET)")
    con.close()
    PROGRESS.write_text(json.dumps(sorted(done)))
    if final:
        logger.info(f"事件表完成：{len(all_df)} 条 → {OUT}")


if __name__ == "__main__":
    main()
