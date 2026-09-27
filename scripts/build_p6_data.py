"""P6 数据建库：limit_list_d（涨跌停）+ top_inst（机构席位）全历史 2019-2026.8。

按交易日历逐日拉取（各约 1800 次调用），断点续跑（progress 记录已完成日期）。
输出：data/p6_limit.parquet + data/p6_topinst.parquet
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


def trade_days() -> list[str]:
    con = duckdb.connect(str(ROOT / "data" / "lihu_quant.duckdb"), read_only=True)
    df = con.execute("SELECT DISTINCT trade_date FROM index_daily WHERE ts_code='000001.SH' "
                     "AND trade_date BETWEEN '2019-01-01' AND '2026-08-31' ORDER BY trade_date").fetchdf()
    con.close()
    return pd.to_datetime(df["trade_date"]).dt.strftime("%Y%m%d").tolist()


def main() -> None:
    cfg = get_settings()
    raw = Path(cfg.tushare.token_file).read_text(encoding="utf-8").strip()
    m = re.search(r"token=([A-Za-z0-9]+)", raw)
    token = m.group(1) if raw.startswith("{") else raw
    client = TushareClient(token=token, cache_dir=cfg.tushare.cache_dir)
    days = trade_days()
    logger.info(f"交易日 {len(days)} 天（{days[0]}~{days[-1]}）")

    prog_path = ROOT / "data" / "p6_progress.json"
    done = set(json.loads(prog_path.read_text())) if prog_path.exists() else set()
    lim_frames, ti_frames = [], []
    # 断点恢复：读已有 parquet
    for name, path in (("limit", ROOT / "data" / "p6_limit.parquet"),
                       ("topinst", ROOT / "data" / "p6_topinst.parquet")):
        if path.exists():
            d = duckdb.query(f"SELECT * FROM '{path}'").fetchdf()
            (lim_frames if name == "limit" else ti_frames).append(d)
            done |= set(d["trade_date"].astype(str).unique().tolist())
            logger.info(f"恢复 {name}: {len(d)} 行")

    todo = [d for d in days if d not in done]
    logger.info(f"待拉 {len(todo)} 天")
    for i, td in enumerate(todo):
        try:
            lim = client.query("limit_list_d", {"trade_date": td})
            if not lim.empty:
                lim_frames.append(lim)
            ti = client.query("top_inst", {"trade_date": td})
            if not ti.empty:
                ti_frames.append(ti)
        except Exception as e:  # noqa: BLE001
            logger.warning(f"{td} 失败: {e}")
            continue
        if i % 60 == 0:
            logger.info(f"进度 {i}/{len(todo)}")
            _flush(lim_frames, ti_frames)
    _flush(lim_frames, ti_frames, final=True)


def _flush(lim_frames, ti_frames, final: bool = False) -> None:
    prog = set()
    if lim_frames:
        lim = pd.concat(lim_frames, ignore_index=True).drop_duplicates(
            subset=["trade_date", "ts_code"] + (["limit"] if any("limit" in f.columns for f in lim_frames[:1]) else []))
        con = duckdb.connect(); con.register("t", lim)
        con.execute(f"COPY t TO '{ROOT / 'data' / 'p6_limit.parquet'}' (FORMAT PARQUET)"); con.close()
        prog |= set(lim["trade_date"].astype(str))
    if ti_frames:
        ti = pd.concat(ti_frames, ignore_index=True).drop_duplicates(subset=["trade_date", "ts_code", "exalter", "side"])
        con = duckdb.connect(); con.register("t", ti)
        con.execute(f"COPY t TO '{ROOT / 'data' / 'p6_topinst.parquet'}' (FORMAT PARQUET)"); con.close()
        prog |= set(ti["trade_date"].astype(str))
    (ROOT / "data" / "p6_progress.json").write_text(json.dumps(sorted(prog)))
    if final:
        logger.info(f"完成：limit {sum(len(f) for f in lim_frames)} 行 / topinst {sum(len(f) for f in ti_frames)} 行")


if __name__ == "__main__":
    main()
