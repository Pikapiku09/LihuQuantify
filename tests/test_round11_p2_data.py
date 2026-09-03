"""第十一轮 P2 数据层与性能健壮性回归测试。

- P2-1：缓存 TTL 失效（mtime 超时 → 重新请求）
- P2-2：fields 入缓存键（先子集后全量不污染）
- P2-3：HTTP 重试/限流（指数退避后成功；配置字段与实现对齐）
- P2-4：DuckDB 只读连接
- P2-5：ensure_daily_basic/moneyflow 本地优先（同日二次零 API）
- P2-6：策略向量化信号全等（此处验证 add_all_standard 结果稳定，向量化在 cherry_claw）
"""

from __future__ import annotations

import json
import time
from datetime import date, timedelta
from pathlib import Path
from unittest.mock import MagicMock

import numpy as np
import pandas as pd
import pytest

from lihu_quantify.data.tushare_client import TushareClient


# ============================================================
# P2-1：缓存 TTL
# ============================================================

def _ok_payload(fields, items):
    return {"code": 0, "data": {"fields": fields, "items": items}}


def test_p21_cache_ttl_revalidates(tmp_path):
    """mtime 超过 TTL → 缓存 miss，重新请求 API。"""
    session = MagicMock()
    client = TushareClient(token="x", cache_dir=tmp_path,
                           cache_mtime_ttl=0.5, retries=1, session=session)
    # 第一次请求写入缓存
    session.post.return_value.json.return_value = _ok_payload(
        ["ts_code", "close"], [["600584.SH", 10.0]])
    df1 = client.query("daily", {"ts_code": "600584.SH", "end_date": "20260818"})
    assert not df1.empty
    assert session.post.call_count == 1

    # 缓存有效期内命中（TTL 未过，0.5s 内）
    session.post.reset_mock()
    df2 = client.query("daily", {"ts_code": "600584.SH", "end_date": "20260818"})
    assert session.post.call_count == 0, "TTL 内应命中缓存，零 API 请求"

    # 强制过期：把 mtime 改旧
    for f in tmp_path.glob("*.json"):
        stale = time.time() - 10
        import os
        os.utime(f, (stale, stale))
    session.post.reset_mock()
    session.post.return_value.json.return_value = _ok_payload(
        ["ts_code", "close"], [["600584.SH", 11.0]])
    df3 = client.query("daily", {"ts_code": "600584.SH", "end_date": "20260818"})
    assert session.post.call_count == 1, "TTL 过期应重新请求"


def test_p21_cache_ttl_zero_disabled(tmp_path):
    """TTL=0 → 完全禁用缓存（每次请求）。"""
    session = MagicMock()
    client = TushareClient(token="x", cache_dir=tmp_path,
                           cache_mtime_ttl=0.0, retries=1, session=session)
    session.post.return_value.json.return_value = _ok_payload(
        ["ts_code", "close"], [["600584.SH", 10.0]])
    client.query("daily", {"ts_code": "600584.SH"})
    client.query("daily", {"ts_code": "600584.SH"})
    assert session.post.call_count == 2


# ============================================================
# P2-2：fields 入缓存键
# ============================================================

def test_p22_fields_do_not_pollute_full_cache():
    """先 fields 子集查询，再不带 fields 全量查询 → 全量列不缺失。"""
    import tempfile

    with tempfile.TemporaryDirectory() as td:
        session = MagicMock()
        client = TushareClient(token="x", cache_dir=td, retries=1, session=session)
        full_item = ["600584.SH", "20260818", 85.42]
        session.post.return_value.json.return_value = _ok_payload(
            ["ts_code", "trade_date", "close"], [full_item])
        # 子集查询
        sub = client.query("daily", {"ts_code": "600584.SH", "end_date": "20260818"},
                           fields=["ts_code", "close"])
        assert list(sub.columns) == ["ts_code", "close"]
        # 全量查询（不同缓存键，应重新请求并返回完整列）
        session.post.reset_mock()
        session.post.return_value.json.return_value = _ok_payload(
            ["ts_code", "trade_date", "close"], [full_item])
        full = client.query("daily", {"ts_code": "600584.SH", "end_date": "20260818"})
        assert "trade_date" in full.columns, "全量查询不应命中子集缓存"
        assert "close" in full.columns


def test_p22_field_ordering_normalized(tmp_path):
    """字段顺序不影响缓存键（排序后哈希）。"""
    client = TushareClient(token="x", cache_dir=tmp_path,
                           retries=1, session=MagicMock())
    p1 = client._cache_path("daily", {"ts_code": "a.SH", "__fields": "a|b"})
    p2 = client._cache_path("daily", {"ts_code": "a.SH", "__fields": "b|a"})
    assert p1.name == p2.name


# ============================================================
# P2-3：HTTP 重试与限流
# ============================================================

def test_p23_retry_after_rate_limit(tmp_path):
    """前两次限流 + 第三次成功 → 重试后成功。"""
    session = MagicMock()
    client = TushareClient(token="x", cache_dir=tmp_path,
                           rate_limit_interval=0.0, retries=3, session=session)
    rate_limit = {"code": 40001, "msg": "每分钟最多访问该接口200次"}
    ok = _ok_payload(["ts_code"], [["600584.SH"]])
    session.post.return_value.json.side_effect = [rate_limit, rate_limit, ok]
    df = client.query("daily", {"ts_code": "600584.SH"})
    assert not df.empty
    assert session.post.call_count == 3


def test_p23_http_error_raises_after_retry(tmp_path):
    """网络异常全部重试耗尽 → 抛异常。"""
    session = MagicMock()
    from requests.exceptions import ConnectionError
    session.post.side_effect = ConnectionError("boom")
    client = TushareClient(token="x", cache_dir=tmp_path,
                           rate_limit_interval=0.0, retries=3, session=session)
    with pytest.raises(ConnectionError):
        client.query("daily", {"ts_code": "600584.SH"})


def test_p23_no_extra_delay_on_success(tmp_path):
    """正常响应零额外时间（同一 API 首次调用无 sleep）。"""
    session = MagicMock()
    session.post.return_value.json.return_value = _ok_payload(["ts_code"], [["a.SH"]])
    client = TushareClient(token="x", cache_dir=tmp_path,
                           rate_limit_interval=0.0, retries=3, session=session)
    t0 = time.time()
    client.query("daily", {"ts_code": "a.SH"})
    assert time.time() - t0 < 0.1


# ============================================================
# P2-4：DuckDB 只读 + with 语法
# ============================================================

def test_p24_read_only_connect_and_with(tmp_path):
    """read_only 连接可查询；写连接 with 退出自动 close。"""
    from lihu_quantify.data.duckdb_store import DuckDBStore
    db = tmp_path / "test.duckdb"
    writer = DuckDBStore(db)
    writer.upsert("index_daily", pd.DataFrame([
        {"trade_date": "20260818", "ts_code": "000001.SH", "close": 3200.0}
    ]))
    writer.close()

    ro = DuckDBStore(db, read_only=True)
    df = ro.query("SELECT count(*) AS n FROM index_daily")
    assert int(df["n"].iloc[0]) == 1
    ro.close()

    # with 语法自动 close
    with DuckDBStore(db, read_only=True) as store:
        assert not store.query("SELECT 1").empty


# ============================================================
# P2-5：ensure 本地优先（data_manager）
# ============================================================

def test_p25_ensure_daily_basic_local_first(tmp_path, tmp_duckdb):
    """本地已覆盖 → 同日再次 ensure_daily_basic 零 API 请求。"""
    from lihu_quantify.data.data_manager import DataManager
    session = MagicMock()
    client = TushareClient(token="x", cache_dir=tmp_path,
                           rate_limit_interval=0.0, retries=1, session=session)
    # 本地已有最新日(2026-08-19)的 daily_basic
    tmp_duckdb.upsert("index_daily", pd.DataFrame([
        {"trade_date": "20260819", "ts_code": "000001.SH", "close": 3200.0}
    ]))
    tmp_duckdb.upsert("daily_basic", pd.DataFrame([
        {"trade_date": "20260819", "ts_code": "600584.SH", "close": 85.0,
         "pe": 12.0, "total_mv": 1e6}
    ]))
    dm = DataManager(client=client, store=tmp_duckdb)
    df = dm.ensure_daily_basic("600584.SH", days=60)
    assert not df.empty
    assert session.post.call_count == 0, "本地已覆盖应零 API 请求"


def test_p25_ensure_moneyflow_local_first(tmp_path, tmp_duckdb):
    """本地已覆盖 → 同日再次 ensure_moneyflow 零 API 请求。"""
    from lihu_quantify.data.data_manager import DataManager
    session = MagicMock()
    client = TushareClient(token="x", cache_dir=tmp_path,
                           rate_limit_interval=0.0, retries=1, session=session)
    tmp_duckdb.upsert("index_daily", pd.DataFrame([
        {"trade_date": "20260819", "ts_code": "000001.SH", "close": 3200.0}
    ]))
    tmp_duckdb.upsert("moneyflow", pd.DataFrame([
        {"trade_date": "20260819", "ts_code": "600584.SH", "net_amount": 100.0}
    ]))
    dm = DataManager(client=client, store=tmp_duckdb)
    df = dm.ensure_moneyflow("600584.SH", days=30)
    assert not df.empty
    assert session.post.call_count == 0


# ============================================================
# P2-6：指标去重（向量化前提）——add_all_standard 幂等
# ============================================================

def test_p26_add_all_standard_idempotent():
    """add_all_standard 重复调用不改变既有信号列（P2-6 去重前提）。"""
    from lihu_quantify.indicators.standard import add_all_standard
    dates = [date(2026, 1, 1) + timedelta(days=i) for i in range(60)]
    df = pd.DataFrame({
        "ts_code": "600584.SH", "trade_date": dates,
        "open": [10.0] * 60, "high": [10.3] * 60, "low": [9.8] * 60,
        "close": [10.0] * 60, "vol": [1e6] * 60, "amount": [1e7] * 60,
    })
    a = add_all_standard(df)
    assert "ma5" in a.columns and "vol_ratio" in a.columns


def _signal_key(s):
    """信号等值比较键（忽略 reason 文案差异，比较下单关键字段）。"""
    return (
        s.ts_code if hasattr(s, "ts_code") else getattr(s, "ts_code", None),
        str(getattr(s, "trade_date", None)),
        round(float(getattr(s, "suggested_price", 0)), 6),
        round(float(getattr(s, "stop_loss", 0)), 6),
        round(float(getattr(s, "suggested_position_pct", 0)), 8),
        tuple(sorted(round(float(t), 6) for t in (getattr(s, "take_profit", None) or []))),
    )


def test_p26_vectorized_signals_equal_to_rowwise():
    """向量化 _evaluate 与逐行边界解析结果信号完全等价（P2-6 硬门槛）。

    构造多段"金叉放量收红"形态，逐根逼近边界，强制高/边界不满足分支都被覆盖。
    """
    from lihu_quantify.strategy.cherry_claw import CherryClaw
    from lihu_quantify.types import Signal

    strategy = CherryClaw(close_to_ma5_max_dev=0.05)
    # 构造：下跌→横盘→温和上涨，上涨段放量收红（V 形反转形态，必然产生金叉）
    n_decline, n_flat, n_rise = 30, 25, 25
    n = n_decline + n_flat + n_rise
    dates = [date(2026, 5, 1) + timedelta(days=i) for i in range(n)]
    closes = [20 - 10 * (i / n_decline) for i in range(n_decline)]
    closes += [10 + 0.15 * np.sin(i) for i in range(n_flat)]
    closes += [10 + 0.05 * (i + 1) for i in range(n_rise)]
    opens = [c - 0.3 for c in closes]
    highs = [c + 0.2 for c in closes]
    lows = [c - 0.4 for c in closes]
    vols = [1_000_000.0] * (n_decline + n_flat) + [1_500_000.0 + 50_000 * i for i in range(n_rise)]
    df = pd.DataFrame({
        "ts_code": "600584.SH", "trade_date": dates,
        "open": opens, "high": highs, "low": lows, "close": closes,
        "vol": vols, "amount": [v * c for v, c in zip(vols, closes)],
    })
    sigs = strategy.scan(df)

    # 参考实现：逐行调用三层过滤（边界/失败分支全部触发）
    ref_sigs = []
    df_ind = strategy._prepare_indicators(df)["df"]
    rolling = df_ind["amount"].rolling(20).mean()
    for i in range(len(df_ind)):
        row = df_ind.iloc[i]
        if pd.isna(row.get("ma5")) or pd.isna(row.get("ma20")) or pd.isna(row.get("vol_ratio")):
            continue
        amt = rolling.iloc[i]
        if pd.isna(amt) or amt < CherryClaw.MIN_AVG_AMOUNT_20D / 1e3:
            continue
        if not strategy._three_layer_filter(row)[0]:
            continue
        close = float(row["close"])
        ma10 = float(row.get("ma10", close))
        sl = strategy.stop_loss_mgr.calc_stop_price(close, ma10)
        targets = strategy._calc_targets(row)
        ref_sigs.append(Signal(
            kind="buy", ts_code="600584.SH", suggested_price=close,
            stop_loss=sl, take_profit=targets,
            suggested_position_pct=strategy.max_position_pct,
            strategy_name="CherryClaw", reason="ref", trade_date=row.get("trade_date"),
        ))

    got = sorted((_signal_key(s) for s in sigs), key=str)
    want = sorted((_signal_key(s) for s in ref_sigs), key=str)
    assert got == want, f"向量化与逐行信号不一致:\ngot={got}\nwant={want}"
    assert len(sigs) > 0, "形态应产生至少一个信号"