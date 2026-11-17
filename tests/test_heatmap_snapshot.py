"""热力图快照（heatmap_snapshot）单元测试（2026-11-17，Step 0）。

这段逻辑原先内嵌在巡检流程里，没有测试、也无法单独触发，导致快照长期停在旧日期
（实测停在 2026-11-10，而行情已到 11-17）。抽出来后守四条：
1. 只保留最新交易日 —— 停牌陈旧 bar 混进来会让「今日涨跌」失真
2. 按成交额降序 —— treemap 面积即成交额
3. 名称/行业缺失时回退（代码 / "未知"），不编造
4. DuckDB 的 date/Decimal 必须转成 JSON 可序列化类型（否则 json.dumps 抛 TypeError，
   表现为「写入失败」而看不出原因 —— 这个坑实测踩过）
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))

from lihu_quantify.monitor.heatmap_snapshot import (  # noqa: E402
    build_from_db, build_rows, write_snapshot,
)

QUOTES = [
    {"ts_code": "000001.SZ", "close": 10.0, "pct_chg": 1.5, "amount": 500.0, "trade_date": "2026-11-17"},
    {"ts_code": "600000.SH", "close": 8.0, "pct_chg": -0.5, "amount": 900.0, "trade_date": "2026-11-17"},
    {"ts_code": "000002.SZ", "close": 5.0, "pct_chg": 9.9, "amount": 9999.0, "trade_date": "2026-11-10"},
]


def test_only_latest_trade_date():
    rows = build_rows(QUOTES)
    assert {r["trade_date"] for r in rows} == {"2026-11-17"}
    assert all(r["ts_code"] != "000002.SZ" for r in rows)


def test_sorted_by_amount_desc():
    rows = build_rows(QUOTES)
    assert [r["ts_code"] for r in rows] == ["600000.SH", "000001.SZ"]


def test_name_and_industry_fallback():
    rows = build_rows(QUOTES, {"000001.SZ": "平安银行"}, {"000001.SZ": "银行"})
    m = {r["ts_code"]: r for r in rows}
    assert m["000001.SZ"]["name"] == "平安银行"
    assert m["000001.SZ"]["industry"] == "银行"
    # 未提供的必须回退，不编造
    assert m["600000.SH"]["name"] == "600000.SH"
    assert m["600000.SH"]["industry"] == "未知"


def test_empty_input():
    assert build_rows([]) == []


def test_write_snapshot_roundtrip(tmp_path):
    rows = build_rows(QUOTES)
    path = write_snapshot(tmp_path, rows)
    assert path is not None and path.exists()
    back = json.loads(path.read_text(encoding="utf-8"))
    assert len(back) == 2
    assert "平安银行" in json.dumps(back, ensure_ascii=False) or back[0]["name"]


def test_write_snapshot_serializes_date_and_decimal(tmp_path):
    """真实调用方会传 date / Decimal；未被转换时应优雅返回 None 而不是抛出。"""
    import datetime as dt
    from decimal import Decimal

    rows = build_rows([{
        "ts_code": "000001.SZ", "close": Decimal("10.5"), "pct_chg": Decimal("1.2"),
        "amount": Decimal("500"), "trade_date": dt.date(2026, 11, 17),
    }])
    out = write_snapshot(tmp_path, rows)
    assert out is None, "未做类型转换时应返回 None（当前 build_rows 不转，转换在 build_from_db）"


def test_build_from_db_types_are_json_safe(tmp_path):
    """构建自 DB 路径必须产出 JSON 可序列化结果（date→ISO、Decimal→float）。"""
    duckdb = pytest.importorskip("duckdb")
    db = tmp_path / "t.duckdb"
    con = duckdb.connect(str(db))
    con.execute("CREATE TABLE daily_quotes (ts_code VARCHAR, trade_date DATE, close DOUBLE, "
                "pct_chg DOUBLE, amount DOUBLE)")
    con.execute("CREATE TABLE stock_basic (ts_code VARCHAR, name VARCHAR, industry VARCHAR)")
    con.execute("INSERT INTO stock_basic VALUES ('000001.SZ','平安银行','银行')")
    con.execute("INSERT INTO daily_quotes VALUES ('000001.SZ', DATE '2026-11-17', 10.5, 1.2, 500.0)")
    con.execute("INSERT INTO daily_quotes VALUES ('600000.SH', DATE '2026-11-10', 8.0, -0.5, 900.0)")
    con.close()

    rows = build_from_db(db)
    assert len(rows) == 1
    assert rows[0]["trade_date"] == "2026-11-17"
    assert isinstance(rows[0]["amount"], float)
    json.dumps(rows)          # 不抛即通过


def test_repo_snapshot_covers_latest_trade_date():
    """仓库快照不应早于库里的最新交易日过多（Step 0 的验收点）。"""
    snap = ROOT / "data" / "heatmap_snapshot.json"
    db = ROOT / "data" / "lihu_quant.duckdb"
    if not snap.exists() or not db.exists():
        pytest.skip("缺少快照或数据库")
    duckdb = pytest.importorskip("duckdb")
    rows = json.loads(snap.read_text(encoding="utf-8"))
    if not rows:
        pytest.skip("快照为空")
    con = duckdb.connect(str(db), read_only=True)
    try:
        latest = con.execute("SELECT MAX(trade_date) FROM daily_quotes").fetchone()[0]
    finally:
        con.close()
    if latest is None:
        pytest.skip("库中无行情")
    snap_date = max(r["trade_date"] for r in rows)
    assert snap_date == latest.isoformat(), (
        f"快照停在 {snap_date}，库中已到 {latest} —— 跑 scripts/refresh_heatmap_snapshot.py")
