"""pytest 全局 fixtures。

加载 samples/tushare/ 中的真实 Tushare 响应作为测试基线。
"""

from __future__ import annotations

import json
import math
from datetime import date, timedelta
from pathlib import Path

import pandas as pd
import pytest

FIXTURES_DIR = Path(__file__).parent / "fixtures" / "tushare"


def load_tushare_sample(filename: str) -> pd.DataFrame:
    """加载 samples/tushare/*.json 为 DataFrame。"""
    path = FIXTURES_DIR / filename
    with path.open(encoding="utf-8") as f:
        resp = json.load(f)
    data = resp.get("data") or {}
    fields = data.get("fields") or []
    items = data.get("items") or []
    if not fields:
        return pd.DataFrame()
    return pd.DataFrame(items, columns=fields)


def parse_trade_date(s: str) -> date:
    """'20260818' → date(2026,8,18)。"""
    s = str(s)
    if len(s) == 8 and s.isdigit():
        return date(int(s[:4]), int(s[4:6]), int(s[6:8]))
    return pd.to_datetime(s).date()


@pytest.fixture
def daily_600584() -> pd.DataFrame:
    """600584.SH 日线（samples，到 8/18，已转 trade_date 为 date 并升序）。"""
    df = load_tushare_sample("daily_600584.SH_20260818.json")
    df["trade_date"] = df["trade_date"].apply(parse_trade_date)
    df = df.sort_values("trade_date").reset_index(drop=True)
    # 数值列
    for c in ["open", "high", "low", "close", "pre_close", "change", "pct_chg", "vol", "amount"]:
        if c in df.columns:
            df[c] = pd.to_numeric(df[c], errors="coerce")
    return df


@pytest.fixture
def daily_600584_with_819(daily_600584) -> pd.DataFrame:
    """在 600584 日线后追加 8/19 bar（来自 samples/reports 描述，samples daily 只到 8/18）。

    8/19：开 81.11/高 83.23/低 76.95/收 77.74，-8.99%，天量大阴线。
    pre_close 取 8/18 close=85.42；vol/amount 参照 8/18 量级。
    """
    bar = pd.DataFrame([{
        "ts_code": "600584.SH",
        "trade_date": date(2026, 8, 19),
        "open": 81.11, "high": 83.23, "low": 76.95, "close": 77.74,
        "pre_close": 85.42, "change": -7.68, "pct_chg": -8.99,
        "vol": 1500000.0, "amount": 12690000.0,
    }])
    df = pd.concat([daily_600584, bar], ignore_index=True)
    return df.sort_values("trade_date").reset_index(drop=True)


@pytest.fixture
def index_daily_000001() -> pd.DataFrame:
    """000001.SH 指数日线（交易日锚定基线）。"""
    df = load_tushare_sample("index_daily_000001.SH_20260819.json")
    df["trade_date"] = df["trade_date"].apply(parse_trade_date)
    return df.sort_values("trade_date").reset_index(drop=True)


@pytest.fixture
def tmp_duckdb(tmp_path) -> "object":
    """临时 DuckDBStore 实例。"""
    from lihu_quantify.data.duckdb_store import DuckDBStore

    return DuckDBStore(tmp_path / "test.duckdb")


def build_v_recovery(n_decline=30, n_flat=20, n_rise=20) -> pd.DataFrame:
    """构造"下跌→横盘→温和上涨"形态（满足 CherryClaw 三层过滤）。"""
    n = n_decline + n_flat + n_rise
    dates = [date(2026, 5, 1) + timedelta(days=i) for i in range(n)]
    closes = [20 - 10 * (i / n_decline) for i in range(n_decline)]
    closes += [10 + 0.15 * math.sin(i) for i in range(n_flat)]
    closes += [10 + 0.05 * (i + 1) for i in range(n_rise)]
    opens = [c - 0.3 for c in closes]
    highs = [c + 0.1 for c in closes]
    lows = [c - 0.4 for c in closes]
    vols = [1_000_000.0] * (n_decline + n_flat) + [1_500_000.0 + 50_000 * i for i in range(n_rise)]
    amounts = [v * c for v, c in zip(vols, closes)]
    pct_chg = [0.0] + [(closes[i] / closes[i - 1] - 1) * 100 for i in range(1, n)]
    pre_close = [closes[0]] + closes[:-1]
    return pd.DataFrame({
        "ts_code": "888888.SH", "trade_date": dates,
        "open": opens, "high": highs, "low": lows, "close": closes,
        "pre_close": pre_close, "pct_chg": pct_chg,
        "vol": vols, "amount": amounts,
    })


@pytest.fixture
def v_recovery_data() -> dict:
    """V 形反转数据（dict 形式，供回测引擎用）。"""
    return {"888888.SH": build_v_recovery()}
