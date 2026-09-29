"""VolTargetSizer 冒烟测试。"""
from datetime import date, timedelta

import numpy as np
import pandas as pd
import pytest

from lihu_quantify.risk.vol_target import VolTargetSizer
from lihu_quantify.types import Signal


def test_scale_high_vol_reduces():
    rng = np.random.default_rng(1)
    close = pd.Series(10 * np.exp(np.cumsum(rng.normal(0, 0.04, 100))))  # 高波动
    s = VolTargetSizer(target_vol=0.20).scale(close)
    assert 0.3 <= s < 1.0      # 高波动 → 减仓

def test_scale_low_vol_keeps():
    rng = np.random.default_rng(2)
    close = pd.Series(10 * np.exp(np.cumsum(rng.normal(0, 0.005, 100))))  # 低波动
    s = VolTargetSizer(target_vol=0.35).scale(close)
    assert s == 1.0            # 低波动 → 不加仓（cap=1）

def test_apply_scales_buy_signal():
    """高波动股 → 缩放生效；逐信号按信号日窗口（前视修复回归）。"""
    import pandas as pd
    sig = Signal(kind="buy", ts_code="600001.SH", suggested_price=10.0,
                 stop_loss=9.2, suggested_position_pct=0.25,
                 trade_date=date(2026, 9, 28))
    # 高波动：锯齿 ±10%（年化波动 >> target 1%）
    close = pd.Series([10.0 * (1.1 if i % 2 else 0.9) for i in range(100)])
    df = pd.DataFrame({"close": close,
                       "trade_date": [date(2026, 5, 1) + timedelta(days=i)
                                      for i in range(100)]})
    VolTargetSizer(target_vol=0.01).apply([sig], df)
    assert sig.suggested_position_pct < 0.25    # 高波动 → 缩到下限
    assert sig.suggested_position_pct >= 0.25 * 0.3  # 不低于 min_scale
    assert "vol_target" in sig.reason


def test_apply_no_lookahead_by_signal_date():
    """前视回归：同一 df，早期信号的 scale ≠ 末窗 scale（逐日窗口生效）。"""
    import pandas as pd
    # 前 80 天平稳（低波动），后 20 天剧烈波动
    close = list(np.linspace(10, 9.5, 80)) + \
        [10.0 if i % 2 else 9.0 for i in range(20)]
    dates = [date(2026, 1, 1) + timedelta(days=i) for i in range(100)]
    df = pd.DataFrame({"close": close, "trade_date": dates})
    early = Signal(kind="buy", ts_code="600001.SH", suggested_price=10.0,
                   suggested_position_pct=0.25, trade_date=dates[50])
    late = Signal(kind="buy", ts_code="600001.SH", suggested_price=10.0,
                  suggested_position_pct=0.25, trade_date=dates[-1])
    sizer = VolTargetSizer(target_vol=0.05, min_scale=0.3)
    sizer.apply([early, late], df)
    # 早期（平稳段）应几乎不缩放或缩放轻；末尾（高波动段）应重缩放
    assert early.suggested_position_pct > late.suggested_position_pct
    assert late.suggested_position_pct == pytest.approx(0.25 * 0.3, abs=1e-6)
