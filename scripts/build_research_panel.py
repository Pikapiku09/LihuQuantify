"""构建研究面板（P1 门禁2 真实数据源）。

股票池：data/strat_pool_n200_L5_seed42.json（成交额分层抽样 200 只，与实盘体系同池）
区间：2019-01-01 ~ 2024-12-31（前复权）
输出：data/research_panel.parquet（列：trade_date/ts_code/open/high/low/close/vol/amount）

用法：.venv/Scripts/python.exe scripts/build_research_panel.py
注意：200 只 × 2 接口（daily+adj_factor）≈ 400 次调用，限流 0.3s/次 ≈ 3-5 分钟。
已完成的股票记录在 data/research_panel_progress.json，可断点续跑。
"""

from __future__ import annotations

import json
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import pandas as pd
from loguru import logger

from lihu_quantify.data.data_manager import DataManager
from lihu_quantify.data.duckdb_store import DuckDBStore
from lihu_quantify.data.tushare_client import TushareClient
from lihu_quantify.config import get_settings

START, END = date(2019, 1, 1), date(2026, 8, 31)
DAYS = (END - START).days + 10
OUT = ROOT / "data" / "research_panel.parquet"
PROGRESS = ROOT / "data" / "research_panel_progress.json"


def _read_token(path: str) -> str:
    """从 token_file 提取 token：兼容纯文本与 MCP JSON（url 参数 token=xxx）两种格式。"""
    raw = Path(path).read_text(encoding="utf-8").strip()
    if raw.startswith("{"):
        import json, re
        m = re.search(r"token=([A-Za-z0-9]+)", raw)
        if not m:
            raise ValueError(f"无法从 {path} 的 MCP url 中提取 token")
        return m.group(1)
    return raw


def main() -> None:
    cfg = get_settings()
    token = cfg.tushare.token or _read_token(cfg.tushare.token_file)
    client = TushareClient(token=token, cache_dir=cfg.tushare.cache_dir)
    store = DuckDBStore(cfg.duckdb.path)
    dm = DataManager(client, store)

    pool = json.loads((ROOT / "data" / "strat_pool_n200_L5_seed42.json").read_text())
    done: set[str] = set()
    if PROGRESS.exists():
        done = set(json.loads(PROGRESS.read_text()))
    logger.info(f"池 {len(pool)} 只，已完成 {len(done)}，本次需拉 {len([c for c in pool if c not in done])}")

    frames = []
    if OUT.exists():  # 断点续跑：先读已有（duckdb 读，无 pyarrow 依赖）
        import duckdb as _ddb0
        old = _ddb0.query(f"SELECT * FROM '{OUT}'").fetchdf()
        frames.append(old)

    for i, code in enumerate(pool):
        if code in done:
            continue
        try:
            # 直接全区间拉取入库（绕开 ensure_daily 的 Timestamp/date 比较 bug；
            # client 缓存键含日期区间，同区间重复调用零成本）
            raw_remote = client.query("daily", {
                "ts_code": code,
                "start_date": START.strftime("%Y%m%d"),
                "end_date": END.strftime("%Y%m%d"),
            })
            if not raw_remote.empty:
                store.upsert("daily_quotes", raw_remote)
            raw = store.get_daily(code, start=START, end=END)
            if raw.empty or len(raw) < 200:
                logger.warning(f"{code} 日线不足（{len(raw)} 行），跳过")
                continue
            # 直接拉复权因子（绕过 get_daily_adjusted 的 store.query 参数 bug）
            adj = client.query("adj_factor", {
                "ts_code": code,
                "start_date": START.strftime("%Y%m%d"),
                "end_date": END.strftime("%Y%m%d"),
            })
            if adj.empty or adj["adj_factor"].isna().all():
                logger.warning(f"{code} 无复权因子，用未复权价")
                df = raw.copy()
            else:
                adj = adj[["trade_date", "adj_factor"]].copy()
                adj["trade_date"] = pd.to_datetime(adj["trade_date"])
                if not pd.api.types.is_datetime64_any_dtype(raw["trade_date"]):
                    raw = raw.copy()
                    raw["trade_date"] = pd.to_datetime(raw["trade_date"])
                df = raw.merge(adj, on="trade_date", how="left")
                df["adj_factor"] = df["adj_factor"].ffill().bfill()
                base = df["adj_factor"].iloc[-1]
                for col in ("open", "high", "low", "close"):
                    df[col] = df[col] * df["adj_factor"] / base
                df = df.drop(columns=["adj_factor"])
        except Exception as e:  # noqa: BLE001
            logger.warning(f"{code} 拉取失败: {e}")
            continue
        if df.empty or len(df) < 200:
            logger.warning(f"{code} 数据不足（{len(df)} 行），跳过")
            continue
        frames.append(df)
        done.add(code)
        if i % 20 == 0:
            logger.info(f"进度 {i}/{len(pool)}")
            _flush(frames, done)
    _flush(frames, done, final=True)


def _flush(frames: list, done: set, final: bool = False) -> None:
    if not frames:
        return
    panel = pd.concat(frames, ignore_index=True)
    cols = [c for c in ("trade_date", "ts_code", "open", "high", "low", "close", "vol", "amount") if c in panel.columns]
    panel = panel[cols].sort_values(["ts_code", "trade_date"]).reset_index(drop=True)
    import duckdb as _ddb
    _con = _ddb.connect()
    _con.register("panel_v", panel)
    _con.execute(f"COPY panel_v TO '{str(OUT)}' (FORMAT PARQUET)")
    _con.close()
    PROGRESS.write_text(json.dumps(sorted(done)))
    if final:
        logger.info(f"面板完成：{len(panel)} 行 × {panel['ts_code'].nunique()} 只 → {OUT}")


if __name__ == "__main__":
    main()
