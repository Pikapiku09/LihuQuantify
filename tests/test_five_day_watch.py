"""五日观察哨包装策略（FiveDayWatchStrategy）的单元测试。

守的是三条语义：
1. 第 N 根 bar 收益 < 阈值时才发 sell，达标不发
2. sell 信号的日期落在「信号 bar + N 根」（不是信号当天，也不是成交日）
3. 窗口不够长（观察哨未到期）时不发信号
"""

from __future__ import annotations

import sys
from datetime import date, timedelta
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))

from lihu_quantify.strategy.base import StrategyBase  # noqa: E402
from lihu_quantify.strategy.five_day_watch import FiveDayWatchStrategy  # noqa: E402
from lihu_quantify.types import Signal  # noqa: E402


class _OneBuy(StrategyBase):
    """桩策略：只在第一根 bar 发一个买入信号，成交价 = 该 bar 收盘价。"""

    name = "stub_one_buy"

    def __init__(self, **params):
        super().__init__(**params)

    def _evaluate(self, df, indicators):
        first = df.iloc[0]
        return [Signal(kind="buy", ts_code="600519.SH",
                       suggested_price=float(first["close"]),
                       trade_date=pd.Timestamp(first["trade_date"]).date(),
                       stop_loss=float(first["close"]) * 0.92)]


def _df(closes: list[float]) -> pd.DataFrame:
    d0 = date(2026, 1, 5)
    return pd.DataFrame({
        "trade_date": [d0 + timedelta(days=i) for i in range(len(closes))],
        "close": closes,
    })


def test_triggers_sell_when_day5_return_below_threshold():
    # 第 5 根（index 5）收盘 90 < 100 → 触发
    df = _df([100, 101, 102, 103, 104, 90, 95])
    w = FiveDayWatchStrategy(_OneBuy(), days=5, threshold=0.0)
    sigs = w.scan(df)
    sells = [s for s in sigs if s.kind == "sell"]
    assert len(sells) == 1
    assert sells[0].trade_date == date(2026, 1, 10)      # 信号日 +5 天
    assert abs(sells[0].suggested_price - 90.0) < 1e-9
    assert w.n_triggered == 1


def test_no_sell_when_day5_return_meets_threshold():
    df = _df([100, 101, 102, 103, 104, 106, 95])
    w = FiveDayWatchStrategy(_OneBuy(), days=5, threshold=0.0)
    assert not [s for s in w.scan(df) if s.kind == "sell"]
    assert w.n_triggered == 0


def test_no_signal_when_window_too_short():
    # 只有 4 根 bar，观察哨没到期
    df = _df([100, 90, 88, 86])
    w = FiveDayWatchStrategy(_OneBuy(), days=5, threshold=0.0)
    assert not [s for s in w.scan(df) if s.kind == "sell"]


def test_threshold_is_respected():
    # 第 5 根 98：thr=0 触发、thr=-0.05 不触发
    df = _df([100, 100, 100, 100, 100, 98])
    assert len([s for s in FiveDayWatchStrategy(_OneBuy(), 5, 0.0).scan(df) if s.kind == "sell"]) == 1
    assert not [s for s in FiveDayWatchStrategy(_OneBuy(), 5, -0.05).scan(df) if s.kind == "sell"]


def test_buy_signals_are_passed_through():
    """包装不能吞掉内层的买入信号。"""
    df = _df([100, 100, 100, 100, 100, 100])
    sigs = FiveDayWatchStrategy(_OneBuy(), 5, 0.0).scan(df)
    assert [s for s in sigs if s.kind == "buy"]
