"""IntradayReversal 策略冒烟测试。"""
import numpy as np
import pandas as pd
import pytest

from lihu_quantify.strategy.intraday_reversal import IntradayReversal


@pytest.fixture
def df() -> pd.DataFrame:
    """单只股票：先连续日内下跌 60 日（制造超卖）再横盘。"""
    rng = np.random.default_rng(3)
    n = 400
    dates = pd.bdate_range("2023-01-01", periods=n)
    close = 20.0 * np.exp(np.cumsum(np.concatenate([
        rng.normal(-0.004, 0.005, 120),   # 深跌段
        rng.normal(0.0, 0.004, n - 120),
    ])))
    open_ = close * (1 + rng.normal(0, 0.004, n))   # open 与 close 有偏离 → 日内收益有变化
    return pd.DataFrame({
        "trade_date": dates, "ts_code": "600001.SH",
        "open": open_, "high": np.maximum(open_, close) * 1.01,
        "low": np.minimum(open_, close) * 0.99, "close": close,
        "vol": 1e6, "amount": 2e5,      # 千元口径 → 2e5千元=2亿 > 1亿阈值
    })


def test_signals_generated(df):
    strat = IntradayReversal()
    sigs = strat.scan(df)
    assert len(sigs) > 0
    s = sigs[0]
    assert s.kind == "buy"
    assert s.stop_loss is not None and s.stop_loss < s.suggested_price
    assert len(s.take_profit) == 4
    assert s.suggested_position_pct <= 0.25
    assert s.strategy_name == "IntradayReversal"


def test_excludes_chinext(df):
    df2 = df.copy(); df2["ts_code"] = "300001.SZ"
    assert IntradayReversal().scan(df2) == []
