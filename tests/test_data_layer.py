"""数据层测试：Tushare 样本加载 + DuckDB 落库。"""

from __future__ import annotations

from datetime import date

import pandas as pd
import pytest

from lihu_quantify.data.duckdb_store import DuckDBStore, _parse_trade_date


def test_parse_trade_date():
    assert _parse_trade_date("20260818") == date(2026, 8, 18)
    assert _parse_trade_date("2026-08-18") == date(2026, 8, 18)
    assert _parse_trade_date(date(2026, 8, 18)) == date(2026, 8, 18)
    assert _parse_trade_date(None) is None
    assert _parse_trade_date("") is None


def test_duckdb_schema_init(tmp_duckdb: DuckDBStore):
    """所有表应已创建。"""
    tables = tmp_duckdb.query(
        "SELECT table_name FROM information_schema.tables WHERE table_schema='main'"
    )["table_name"].tolist()
    expected = {
        "daily_quotes", "daily_basic", "weekly_quotes", "index_daily", "sw_daily",
        "limit_list_d", "moneyflow", "moneyflow_dc", "income", "fina_indicator",
        "forecast", "express", "major_news", "trade_cal", "fund_daily", "stock_basic",
    }
    assert expected.issubset(set(tables)), f"缺失表: {expected - set(tables)}"


def test_duckdb_upsert_and_query_daily(tmp_duckdb: DuckDBStore, daily_600584):
    """upsert daily_quotes 后能按区间查询。"""
    df = daily_600584.copy()
    n = tmp_duckdb.upsert("daily_quotes", df)
    assert n == len(df)

    # 查最近 5 根
    recent = tmp_duckdb.get_latest_n("600584.SH", 5)
    assert len(recent) == 5
    # 升序
    assert recent["trade_date"].is_monotonic_increasing
    # 8/18 close=85.42
    last = recent.iloc[-1]
    assert last["close"] == pytest.approx(85.42, abs=0.01)


def test_duckdb_upsert_idempotent(tmp_duckdb: DuckDBStore, daily_600584):
    """重复 upsert 不应产生重复行（INSERT OR REPLACE）。"""
    df = daily_600584.copy()
    tmp_duckdb.upsert("daily_quotes", df)
    tmp_duckdb.upsert("daily_quotes", df)  # 再写一次
    assert tmp_duckdb.count_rows("daily_quotes", "600584.SH") == len(df)


def test_duckdb_latest_trade_date(tmp_duckdb: DuckDBStore, index_daily_000001):
    """index_daily 落库后能取最新交易日。"""
    tmp_duckdb.upsert("index_daily", index_daily_000001)
    latest = tmp_duckdb.get_latest_trade_date("000001.SH")
    assert latest == date(2026, 8, 19)


def test_tushare_client_load_local_sample():
    """TushareClient.load_local_sample 能解析 samples JSON。"""
    from pathlib import Path

    from lihu_quantify.data.tushare_client import TushareClient

    path = Path(__file__).parent / "fixtures" / "tushare" / "daily_600584.SH_20260818.json"
    df = TushareClient.load_local_sample(path)
    assert not df.empty
    assert "close" in df.columns
    assert "ts_code" in df.columns
    assert len(df) == 56  # samples 含 56 根日线（6/1~8/18）
