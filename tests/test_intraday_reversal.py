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


def test_entry_direction_uses_high_percentile(df):
    """P0-B 回归：入场必须取因子【高分位】。

    因子 ir20 = -Σ ln(close/open)，数值越大代表日内跌得越多、越超卖、越看多。
    因此 mask 应为 `ir20_pct >= 1 - entry_pct`（默认 ≥0.80），而非 `<= entry_pct`。
    历史 bug：误用低分位，回测 -25.18%（胜率 29.4%）vs 高分位 +13.39%（40.9%）。
    """
    strat = IntradayReversal()
    sigs = strat.scan(df)
    assert sigs, "fixture 应产生信号"

    d = strat._prepare_indicators(df)["df"]
    pct = dict(zip(d["trade_date"], d["ir20_pct"]))
    threshold = 1.0 - strat.entry_pct
    for s in sigs:
        p = pct.get(s.trade_date)
        assert p is not None and not pd.isna(p), f"{s.trade_date} 缺 ir20_pct"
        assert p >= threshold - 1e-9, (
            f"{s.trade_date} ir20_pct={p:.3f} 低于高分位阈值 {threshold:.2f}，方向疑似又反了"
        )

