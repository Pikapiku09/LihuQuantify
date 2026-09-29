"""research 模块冒烟测试（Phase 0 脚手架验证）。

用合成面板数据验证：因子注册/计算、IC、分组回测、DSR 边界、
walkforward 切分、验收单 PASS/FAIL 判定。
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from lihu_quantify.research import (
    FACTOR_REGISTRY,
    acceptance,
    calc_ic,
    compute_factor,
    deflated_sharpe,
    evaluate_factor,
    momentum,
    quantile_backtest,
    reversal,
    split_walkforward,
    turnover_premium,
)
from lihu_quantify.research.regime import classify_regime


@pytest.fixture(scope="module")
def panel() -> pd.DataFrame:
    """合成面板：30 只股票 × 400 日，注入微弱动量效应。"""
    rng = np.random.default_rng(42)
    n_stock, n_day = 30, 400
    dates = pd.bdate_range("2023-01-01", periods=n_day)
    rows = []
    for i in range(n_stock):
        # 股票 i 的漂移与动量强度微弱正相关 → momentum 因子应有正 IC
        drift = (i / n_stock - 0.5) * 0.0004
        ret = rng.normal(drift, 0.02, n_day)
        ret[1:] += 0.05 * ret[:-1]          # 一阶自相关 → 动量
        close = 10.0 * np.exp(np.cumsum(ret))
        for t in range(n_day):
            rows.append({
                "trade_date": dates[t], "ts_code": f"{600000 + i:06d}.SH",
                "open": close[t] * 0.99, "high": close[t] * 1.01,
                "low": close[t] * 0.98, "close": close[t],
                "vol": 1e6, "amount": 1e7 * (1 + 0.1 * rng.normal()),
            })
    return pd.DataFrame(rows)


def test_factor_registry(panel):
    assert "momentum_20" in FACTOR_REGISTRY and "reversal_5" in FACTOR_REGISTRY
    f = compute_factor(panel, "momentum_20")
    assert len(f) == len(panel)
    assert f.notna().sum() > len(panel) * 0.9
    with pytest.raises(KeyError):
        compute_factor(panel, "no_such_factor")


def test_calc_ic_shape_and_range(panel):
    f = momentum(panel)
    ics = calc_ic(panel, f, horizon=5)
    assert {"ic", "rank_ic", "n"}.issubset(ics.columns)
    assert ics["rank_ic"].dropna().between(-1, 1).all()
    assert len(ics) > 300


def test_quantile_backtest(panel):
    f = momentum(panel)
    qb = quantile_backtest(panel, f, n_groups=5, horizon=5, cost_bps=15)
    grp = qb["group_returns"]
    assert grp.shape[1] == 5
    assert len(qb["long_short"].dropna()) > 300
    # 注入的动量应让 G5 均值 >= G1 均值（统计上）
    assert grp["G5"].mean() >= grp["G1"].mean()


def test_deflated_sharpe_boundaries():
    # 强证据：高 SR、多样本 → DSR 接近 1
    assert deflated_sharpe(0.1, 0.05, n_obs=500, n_trials=1) > 0.95
    # 弱证据：低 SR、少样本、试了 100 次 → DSR 显著下降
    weak = deflated_sharpe(0.02, 0.05, n_obs=100, n_trials=100)
    assert weak < 0.95
    # 单次试验 sr0=0 → DSR=普通显著性
    assert 0.0 <= weak <= 1.0


def test_walkforward():
    folds = split_walkforward(n=1000, n_folds=5, train_frac=0.6, purge=5, embargo=2)
    assert 4 <= len(folds) <= 5
    for k, fo in enumerate(folds):
        tr, te = fo.train, fo.test
        assert tr[1] <= te[0]                 # 训练在前测试在后
        assert te[0] - tr[1] >= 5 - 1         # purge 生效（间隔>=purge）
        assert te[1] > te[0]
        if k > 0:
            assert te[0] >= folds[k - 1].test[0]  # 测试段随折推进
    with pytest.raises(ValueError):
        split_walkforward(n=5)


def test_evaluate_factor_verdict(panel):
    # 有真实效应的因子应至少通过 IC 类门（合成数据 DSR/分组也可能通过）
    f = momentum(panel)
    res = evaluate_factor(panel, f, name="momentum_20", horizon=5, n_trials=1)
    d = res.as_dict()
    assert d["factor_name"] == "momentum_20"
    assert d["rank_ic_mean"] > 0
    assert isinstance(d["pass_all"], bool)
    assert isinstance(d["gates"], dict) and len(d["gates"]) >= 6
    # 纯噪声因子应 FAIL
    rng = np.random.default_rng(7)
    noise = pd.Series(rng.normal(0, 1, len(panel)))
    res2 = evaluate_factor(panel, noise, name="noise", horizon=5, n_trials=10)
    assert res2.pass_all is False


def test_regime_classify():
    idx = pd.DataFrame({
        "trade_date": pd.bdate_range("2022-01-01", periods=300),
        "close": np.concatenate([np.linspace(3000, 3600, 150), np.linspace(3600, 2600, 150)]),
    })
    reg = classify_regime(idx)
    assert len(reg) == 300
    assert set(reg.unique()) <= {"bull", "bear", "range"}
    assert (reg.iloc[60:100] == "bull").mean() > 0.8  # 预热期后前段上涨以 bull 为主
    assert (reg.iloc[-100:] == "bear").mean() > 0.8  # 后段深跌以 bear 为主


def test_ic_alignment_unsorted():
    """IC 对齐回归（评审 P2）：df 打乱顺序后 IC 应与排序后一致（原 .values 位置贴会错位）。"""
    import numpy as np
    import pandas as pd
    from lihu_quantify.research.ic import calc_ic

    rng = np.random.default_rng(7)
    n = 450
    n_days = 30
    df = pd.DataFrame({
        "ts_code": [f"60{i // n_days:04d}.SH" for i in range(n)],
        "trade_date": list(pd.date_range("2024-01-01", periods=n_days, freq="B")) * (n // n_days),
        "close": 10.0 + rng.normal(0, 1, n).cumsum() * 0.1,
    })
    df = df.sort_values(["ts_code", "trade_date"]).reset_index(drop=True)
    factor = pd.Series(rng.normal(0, 1, n), index=df.index)
    ic_sorted = calc_ic(df, factor, horizon=3)

    # 打乱行序（同因子语义：因子跟着行走）
    shuffled_idx = df.sample(frac=1.0, random_state=1).index
    df_shuf = df.loc[shuffled_idx].reset_index(drop=True)
    factor_shuf = factor.loc[shuffled_idx].reset_index(drop=True)
    ic_shuf = calc_ic(df_shuf, factor_shuf, horizon=3)

    pd.testing.assert_frame_equal(ic_sorted.sort_index(), ic_shuf.sort_index())
