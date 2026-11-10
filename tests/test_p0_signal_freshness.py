"""P0-A 回归：latest_signal 只认【最新 bar】的信号，不得回退历史信号。

背景（2026-11-10）：
    原实现 ``return signals[-1]`` 返回「窗口内最后一根合格 bar」的信号，
    不校验它是否属于最新 bar。对带限频的策略（IntradayReversal 距上一信号
    ≥10 交易日）该信号可滞后数十个交易日，造成：

      - 实测 605178.SH 于 2026-10-23 取到 2026-07-16 的信号（滞后 65 交易日），
        旧止损 90.62 对今日执行价 64.15 → 「铁律1：止损价不低于买入价」拦截；
      - 池内 200 只几乎「每天都有信号」（196-200 个），僵尸信号堆积的假象。

修复口径与事件驱动路径 on_bar()（修复5b）一致。
"""
from __future__ import annotations

from datetime import date

import pandas as pd
import pytest

from lihu_quantify.strategy.base import StrategyBase
from lihu_quantify.types import Signal


class _EmitOnDates(StrategyBase):
    """测试替身：只在指定日期产出信号（用于模拟"历史有信号、今日没有"）。"""

    name = "emit_on_dates"
    stateless = True

    def __init__(self, emit_dates):
        super().__init__()
        self.emit_dates = set(emit_dates)

    def _evaluate(self, df: pd.DataFrame, indicators: dict) -> list[Signal]:
        out = []
        for _, row in df.iterrows():
            # 用基类的宽松日期比较，兼容 date / Timestamp / 'YYYYMMDD'
            if any(self._same_date(row["trade_date"], d) for d in self.emit_dates):
                close = float(row["close"])
                out.append(Signal(
                    kind="buy", ts_code="600001.SH", suggested_price=close,
                    stop_loss=close * 0.92, trade_date=row["trade_date"],
                ))
        return out


def _df(dates, closes=None):
    closes = closes or [10.0 + i * 0.1 for i in range(len(dates))]
    return pd.DataFrame({"trade_date": list(dates), "close": closes})


D = [date(2026, 7, 16), date(2026, 7, 17), date(2026, 7, 20), date(2026, 10, 23)]


def test_latest_signal_returns_today_when_today_qualifies():
    """今日 bar 有信号 → 返回今日信号。"""
    strat = _EmitOnDates({D[-1]})
    sig = strat.latest_signal(_df(D))
    assert sig is not None
    assert sig.trade_date == D[-1]


def test_latest_signal_drops_stale_history():
    """仅有历史信号、今日无信号 → 必须返回 None（核心修复）。"""
    strat = _EmitOnDates({D[0], D[1], D[2]})     # 只有 7 月的信号
    df = _df(D)
    assert strat.scan(df), "前置条件：scan 应能扫出历史信号"
    assert strat.latest_signal(df) is None, "不得把 7 月的旧信号当作今日信号返回"


def test_latest_signal_none_when_no_signal_at_all():
    assert _EmitOnDates(set()).latest_signal(_df(D)) is None


def test_latest_signal_empty_df():
    strat = _EmitOnDates({D[-1]})
    assert strat.latest_signal(pd.DataFrame()) is None
    assert strat.latest_signal(None) is None


def test_latest_signal_prefers_today_over_older():
    """今日与历史同时有信号 → 取今日，不能取 signals[-1] 之外的历史项。"""
    strat = _EmitOnDates({D[0], D[-1]})
    sig = strat.latest_signal(_df(D))
    assert sig is not None and sig.trade_date == D[-1]


def test_latest_signal_tolerates_mixed_date_types():
    """trade_date 为 Timestamp / YYYYMMDD 字符串时也应正确判定"同一交易日"。"""
    dates_ts = pd.to_datetime(["2026-10-22", "2026-10-23"])
    strat = _EmitOnDates({date(2026, 10, 23)})
    df = pd.DataFrame({"trade_date": dates_ts, "close": [10.0, 10.1]})
    # Signal.trade_date 是 date(2026,10,23)，df 是 Timestamp → 需宽松比较
    assert strat.latest_signal(df) is not None

    strat2 = _EmitOnDates({"20261022"})
    df2 = pd.DataFrame({"trade_date": ["20261022", "20261023"], "close": [10.0, 10.1]})
    assert strat2.latest_signal(df2) is None, "字符串日期下旧信号仍应被丢弃"


def test_on_bar_same_semantics():
    """事件驱动路径口径应与 latest_signal 一致（同修复5b）。"""
    from lihu_quantify.strategy.base import BarContext

    strat = _EmitOnDates({D[0]})     # 只有 7 月信号
    df = _df(D)
    ctx = BarContext(
        bar=df.iloc[-1], history=df, indicators={},
        position=None, account=None, trade_date=D[-1],
    )
    assert strat.on_bar(ctx) is None


def test_same_date_helper():
    from lihu_quantify.strategy.base import StrategyBase as SB

    assert SB._same_date(date(2026, 10, 23), date(2026, 10, 23)) is True
    assert SB._same_date(pd.Timestamp("2026-10-23"), "20261023") is True
    assert SB._same_date(date(2026, 7, 16), date(2026, 10, 23)) is False
    assert SB._same_date(None, date(2026, 10, 23)) is False
