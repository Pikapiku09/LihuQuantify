"""热力图快照（``data/heatmap_snapshot.json``）的构建与落盘（2026-11-17）。

为什么抽出来
------------
原来这段逻辑只存在于 ``scheduler._write_heatmap_snapshot`` 里，是**巡检流程的内嵌步骤**。
后果：想在不跑整轮巡检的情况下刷新热力图（例如行情刚补完、或每日 16:30 巡检没赶上）时
无路可走，只能让快照一直停在旧日期（实测停在 2026-11-10，而行情已到 11-17）。

现在抽成纯函数 + 一个独立脚本，巡检与手动刷新共用同一份实现，schema 只有一处定义。

schema（web 端唯一数据源，切断 DuckDB 写锁依赖）
------------------------------------------------
``[{ts_code, name, industry, close, pct_chg, amount, trade_date}]``

两条规则来自原实现，必须保留：
1. **只保留最新交易日** —— 长期停牌的陈旧 bar 混进来会让「今日涨跌」失真；
2. 按 ``amount`` 降序 —— 前端 treemap 的面积即成交额，顺序影响可读性。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Iterable, Optional

SNAPSHOT_FILE = Path("data") / "heatmap_snapshot.json"


def build_rows(
    quotes: Iterable[dict],
    name_map: Optional[dict] = None,
    sector_map: Optional[dict] = None,
) -> list[dict]:
    """把行情行转成快照行；只保留最新交易日，按成交额降序。"""
    name_map = name_map or {}
    sector_map = sector_map or {}
    rows: list[dict] = []
    for q in quotes:
        code = q.get("ts_code")
        if not code:
            continue
        rows.append({
            "ts_code": code,
            "name": name_map.get(code) or code,
            "industry": sector_map.get(code) or "未知",
            "close": q.get("close"),
            "pct_chg": q.get("pct_chg"),
            "amount": q.get("amount"),
            "trade_date": q.get("trade_date"),
        })
    if rows:
        max_td = max(r["trade_date"] for r in rows if r["trade_date"] is not None)
        rows = [r for r in rows if r["trade_date"] == max_td]
    rows.sort(key=lambda r: -(r.get("amount") or 0))
    return rows


def write_snapshot(root: Path, rows: list[dict]) -> Optional[Path]:
    """写 UTF-8 快照（ensure_ascii=False，编码链路单一可控）。失败返回 None，不抛。"""
    path = Path(root) / SNAPSHOT_FILE
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(rows, ensure_ascii=False), encoding="utf-8")
    except (OSError, TypeError):
        return None
    return path


def build_from_db(db_path: Path, min_amount: float = 0.0) -> list[dict]:
    """从 DuckDB 构建：取最新交易日全部标的，join stock_basic 补名称与行业。

    不读 Tushare —— 库里有就够了；补数请先跑 ``scripts/backfill_market_data.py``。
    """
    import duckdb  # 局部导入：调用方若走巡检路径就不必加载

    con = duckdb.connect(str(db_path), read_only=True)
    try:
        latest = con.execute("SELECT MAX(trade_date) FROM daily_quotes").fetchone()[0]
        if latest is None:
            return []
        rows = con.execute(
            "SELECT q.ts_code, q.close, q.pct_chg, q.amount, q.trade_date, "
            "       b.name, b.industry "
            "FROM daily_quotes q LEFT JOIN stock_basic b USING (ts_code) "
            "WHERE q.trade_date = ? AND q.amount >= ?",
            [latest, min_amount],
        ).fetchall()
    finally:
        con.close()

    def _f(v):
        """DuckDB 常返回 Decimal；json.dumps 不认，统一转 float。"""
        try:
            return float(v) if v is not None else None
        except (TypeError, ValueError):
            return None

    def _d(v):
        """trade_date 是 datetime.date；转 ISO 字符串，否则 json.dumps 抛 TypeError。"""
        if v is None:
            return None
        return v.isoformat() if hasattr(v, "isoformat") else str(v)

    quotes = [{
        "ts_code": r[0], "close": _f(r[1]), "pct_chg": _f(r[2]),
        "amount": _f(r[3]), "trade_date": _d(r[4]),
    } for r in rows]
    name_map = {r[0]: r[5] for r in rows if r[5]}
    sector_map = {r[0]: r[6] for r in rows if r[6]}
    return build_rows(quotes, name_map, sector_map)
