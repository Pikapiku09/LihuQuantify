"""VolTargetSizer 冒烟测试。"""
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
    sig = Signal(kind="buy", ts_code="600001.SH", suggested_price=10.0,
                 stop_loss=9.2, suggested_position_pct=0.25)
    close = pd.Series(np.linspace(10, 8, 100))  # 平稳
    VolTargetSizer(target_vol=0.01).apply([sig], pd.DataFrame({"close": close}))
    assert sig.suggested_position_pct <= 0.25    # 只减不加
    assert "vol_target" in sig.reason
