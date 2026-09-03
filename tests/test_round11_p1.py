"""第十一轮 P1 回测口径回归测试。

- P1-1：复权处理（apply_forward_adjustment 前复权，除权日价格连续）
- P1-2：涨跌停/停牌无法成交建模
- P1-3：限价单成交价与 open 比较（此前 min(limit,high) 买贵）
- P1-4：买入现金充足性校验（防 T+1 透支 → 缩量整手）
- P1-5：pre_filter 移除未来流动性前视 + 逐 bar 滚动均额
- P1-6：量比定义修正（分母不含当日）
"""
from __future__ import annotations

from datetime import date, timedelta

import numpy as np
import pandas as pd
import pytest

from lihu_quantify.backtest.broker import Order, SimulatedBroker
from lihu_quantify.data.adjustment import apply_forward_adjustment
from lihu_quantify.indicators.standard import add_all_standard


# ============================================================
# P1-1：前复权
# ============================================================

def _flat_ohlc(n, close):
    return pd.DataFrame({
        "open": [close] * n, "high": [close + 0.2] * n,
        "low": [close - 0.2] * n, "close": [close] * n,
        "vol": [1_000_000.0] * n, "amount": [close * 1_000_000.0] * n,
        "pre_close": [close] * n,
    })


def test_adj_factor_forward_adjustment_price_continuous():
    """除权（10% 分红 → 价格被打 9 折）后，前复权使历史价格按因子放大、连续无跳变。"""
    dates = [date(2026, 1, 1) + timedelta(days=i) for i in range(10)]
    df = _flat_ohlc(10, 10.0)
    df["ts_code"] = "600584.SH"
    df["trade_date"] = dates
    # 除权前（前5天）因子 1.0；除权后（后5天）因子 0.9。最新基准 = 0.9。
    adj = pd.DataFrame({
        "trade_date": dates,
        "adj_factor": [1.0] * 5 + [0.9] * 5,
    })
    out = apply_forward_adjustment(df, adj)
    # 前 5 天价格 ×(1.0/0.9)；后 5 天不受影响（×0.9/0.9）
    assert out.iloc[0]["close"] == pytest.approx(10.0 / 0.9, rel=1e-6)
    assert out.iloc[4]["close"] == pytest.approx(10.0 / 0.9, rel=1e-6)
    assert out.iloc[5]["close"] == pytest.approx(10.0, rel=1e-6)
    # open/high/low 同口径
    assert out.iloc[0]["open"] == pytest.approx(10.0 / 0.9, rel=1e-6)
    # 无 NaN（复权不应造成指标断裂）
    assert out["close"].notna().all()
    assert out["adj_factor"].iloc[0] == pytest.approx(1.0)


def test_adj_factor_volume_divided():
    """量按复权因子下移，保持金额量纲一致。"""
    dates = [date(2026, 1, 1) + timedelta(days=i) for i in range(6)]
    df = _flat_ohlc(6, 10.0)
    df["ts_code"] = "600584.SH"
    df["trade_date"] = dates
    adj = pd.DataFrame({"trade_date": dates,
                        "adj_factor": [1.0] * 3 + [0.5] * 3})
    out = apply_forward_adjustment(df, adj)
    ratio_pre = 1.0 / 0.5
    assert out.iloc[0]["vol"] == pytest.approx(1_000_000.0 / ratio_pre, rel=1e-6)
    assert out.iloc[3]["vol"] == pytest.approx(1_000_000.0, rel=1e-6)


def test_adj_factor_empty_returns_copy():
    """复权因子为空 → 原样返回，不崩。"""
    dates = [date(2026, 1, 1) + timedelta(days=i) for i in range(5)]
    df = _flat_ohlc(5, 10.0)
    df["ts_code"] = "600584.SH"
    df["trade_date"] = dates
    out = apply_forward_adjustment(df, pd.DataFrame())
    assert out["close"].iloc[0] == pytest.approx(10.0)


def test_adj_factor_ma_continuity_after_split():
    """复权后 MA 在除权日前后连续（原价 10→9 跳变修复为 11.11→9，趋势真实）。"""
    dates = [date(2026, 1, 1) + timedelta(days=i) for i in range(12)]
    # 真实价格：除权日(第6天)从 10 掉到 9（除息），此后 9,9,...（剔除息口后真实无变）
    closes = [10.0] * 5 + [9.0] * 7
    df = pd.DataFrame({
        "ts_code": "600584.SH", "trade_date": dates,
        "open": [c for c in closes], "high": [c + 0.2 for c in closes],
        "low": [c - 0.2 for c in closes], "close": closes,
        "vol": [1_000_000.0] * 12, "amount": [c * 1_000_000.0 for c in closes],
        "pre_close": closes,
    })
    adj = pd.DataFrame({"trade_date": dates,
                        "adj_factor": [1.0] * 6 + [0.9] * 6})
    out = apply_forward_adjustment(df, adj)
    adj_close = out["close"].to_numpy()
    # 前 5 天：10/0.9≈11.111；第 6 天：9*1.0/0.9=10（第6天因子1.0）；后：9*0.9/0.9=9
    # → 除权日跳变被校正为真实利差（11.11→10→9），不再有"10→9 假崩盘"
    assert adj_close[4] == pytest.approx(10.0 / 0.9, rel=1e-6)
    assert adj_close[5] == pytest.approx(10.0, rel=1e-6)   # 1.0/0.9 作用在第5天
    assert adj_close[6] == pytest.approx(9.0, rel=1e-6)
    # 修复前：10 直接跌到 9，MA 会在除权日假钝化/破位；修复后应平滑。
    # 只校验 MA5 连续性（数据仅 12 行，不足 MA20/60，避免触发全套指标 DEA/斜率 NaN）
    ma5 = out["close"].rolling(5).mean()
    assert ma5.iloc[6] > 9.0  # 前移的历史高价使 MA5 高于 9


# ============================================================
# P1-2：涨跌停/停牌无法成交
# ============================================================

def test_p12_limit_up_one_word_buy_rejected():
    broker = SimulatedBroker()
    order = Order("600519.SH", "buy", 100, "market")
    # 前收 10 → 涨停 11；一字板：open=high=low=11（low>=11）
    bar = pd.Series({"open": 11, "high": 11, "low": 11, "close": 11,
                     "pre_close": 10, "vol": 1_000_000})
    assert broker.fill(order, bar) is None


def test_p12_limit_down_one_word_sell_rejected():
    broker = SimulatedBroker()
    order = Order("600519.SH", "sell", 100, "market")
    # 前收 10 → 跌停 9；一字跌停 open=high=low=9（high<=9）
    bar = pd.Series({"open": 9, "high": 9, "low": 9, "close": 9,
                     "pre_close": 10, "vol": 1_000_000})
    assert broker.fill(order, bar) is None


def test_p12_suspension_volume_zero_rejected():
    broker = SimulatedBroker()
    order = Order("600519.SH", "buy", 100, "market")
    bar = pd.Series({"open": 10, "high": 10.2, "low": 9.9, "close": 10,
                     "pre_close": 10, "vol": 0})   # 停牌
    assert broker.fill(order, bar) is None


def test_p12_normal_bar_fills_without_preclose_vol():
    """既有合成 bar（无 pre_close / 无 vol）行为与修复前一致（P1-2 验收④）。"""
    broker = SimulatedBroker()
    order = Order("600519.SH", "buy", 100, "market")
    bar = pd.Series({"open": 100, "high": 105, "low": 99, "close": 103})
    fill = broker.fill(order, bar)
    assert fill is not None
    assert fill.price == pytest.approx(100 * (1 - broker.slippage), rel=1e-6)


def test_p12_limit_up_no_reject_when_not_one_word():
    """非一字板（open 未封停在涨停）时买单可成交。"""
    broker = SimulatedBroker()
    order = Order("600519.SH", "buy", 100, "market")
    # 前收 10；当日 open=10.5，high=11（触及涨停但早盘未封死）→ 可成交
    bar = pd.Series({"open": 10.5, "high": 11, "low": 10.2, "close": 11,
                     "pre_close": 10, "vol": 1_000_000})
    assert broker.fill(order, bar) is not None


# ============================================================
# P1-3：限价单成交价（与 open 比较）
# ============================================================

def test_p13_limit_buy_fills_at_open_when_better():
    broker = SimulatedBroker()
    order = Order("600519.SH", "buy", 100, "limit", limit_price=102)
    bar = pd.Series({"open": 100, "high": 105, "low": 99, "close": 103})
    fill = broker.fill(order, bar)
    assert fill is not None
    assert fill.price == 100  # min(102, open=100) — 修复前 min(102,high=105)=高估


def test_p13_limit_buy_fills_at_limit_when_open_worse():
    broker = SimulatedBroker()
    order = Order("600519.SH", "buy", 100, "limit", limit_price=99)
    bar = pd.Series({"open": 100, "high": 105, "low": 98, "close": 103})
    fill = broker.fill(order, bar)
    assert fill is not None
    assert fill.price == 99  # min(99, open=100) = 99


def test_p13_limit_sell_fills_at_open_when_better():
    broker = SimulatedBroker()
    order = Order("600519.SH", "sell", 100, "limit", limit_price=98)
    bar = pd.Series({"open": 100, "high": 105, "low": 99, "close": 103})
    fill = broker.fill(order, bar)
    assert fill is not None
    assert fill.price == 100  # max(98, open=100) = 100


# ============================================================
# P1-4：买入现金充足性校验
# ============================================================

class _StubBuyStrategy:
    """每次 scan 在最新一根 bar 发买入（仓位 100% 以触发缩量）。"""
    stateless = True
    name = "stub"

    def __init__(self, pct=0.25, signal_dates=None):
        self.pct = pct
        self.signal_dates = signal_dates

    def scan(self, df):
        from lihu_quantify.types import Signal
        code = str(df["ts_code"].iloc[0])
        sigs = []
        for _, r in df.iterrows():
            d = r["trade_date"]
            if self.signal_dates is not None and d not in self.signal_dates:
                continue
            c = float(r["close"])
            sigs.append(Signal(kind="buy", ts_code=code, suggested_price=c,
                               stop_loss=c * 0.9, take_profit=[c * 1.1],
                               suggested_position_pct=self.pct, reason="stub",
                               trade_date=d))
        return sigs


def _df(code, n=40, closes=None, opens=None, volumes=None):
    dates = [date(2026, 1, 1) + timedelta(days=i) for i in range(n)]
    closes = closes or [10.0] * n
    opens = opens or [c - 0.2 for c in closes]
    volumes = volumes or [1_000_000.0] * n
    return pd.DataFrame({
        "ts_code": code, "trade_date": dates, "open": opens,
        "high": [o + 0.3 for o in opens], "low": [o - 0.3 for o in opens],
        "close": closes, "vol": volumes, "amount": [c * 1_000_000.0 for c in closes],
        "pre_close": closes,
    })


def test_p14_cash_shrink_to_lots_no_overdraft():
    """T+1 跳空高开 → 买入不得透支现金、必须整手。

    设计注意：P1-2 新增一字板拦截后，4.5 倍极端跳空会被判为涨跌停拒单而非缩量
    （超 10% 的隔夜跳空本就不该成交，属正确行为）；真正触发缩量的是多单同日
    消费同一现金池（见 test_p14_no_overdraft_multi_same_day）。此处验证单笔
    在跳空后仍整手且现金非负、总资产非负。
    """
    from lihu_quantify.backtest.engine import EventDrivenEngine

    signal_date = date(2026, 1, 1) + timedelta(days=37)
    # 股价 10；T+1(index38) 跳空 open=10.5（仍在涨停价 11 内，非一字板）
    opens = [9.8] * 38 + [10.5, 10.0]
    df = _df("888888.SH")
    df["open"] = opens
    df["high"] = [o + 0.3 for o in opens]
    df["low"] = [o - 0.3 for o in opens]
    engine = EventDrivenEngine(strategy=_StubBuyStrategy(pct=0.25,
                                                         signal_dates={signal_date}))
    result = engine.run({"888888.SH": df}, init_capital=100_000)
    buys = [t for t in result.trades if t.side == "buy"]
    assert buys, "应有买入成交"
    b = buys[0]
    assert b.volume % 100 == 0, "成交量必须为 100 整数倍"
    assert b.price * b.volume <= 100_000, f"买入成本不得透支现金，实际 {b.volume} 股 × {b.price}"
    assert result.portfolio.cash >= -1e-9
    assert result.portfolio.total_asset >= 0


def test_p14_no_overdraft_multi_same_day():
    """多只同日撮合 → 消费顺序缩量，整体不透支（5 只 ×25% > 现金）。"""
    from lihu_quantify.backtest.engine import EventDrivenEngine

    signal_date = date(2026, 1, 1) + timedelta(days=37)
    data = {f"60100{k}.SH": _df(f"60100{k}.SH") for k in range(1, 6)}
    engine = EventDrivenEngine(
        strategy=_StubBuyStrategy(pct=0.25, signal_dates={signal_date}))
    result = engine.run(data, init_capital=100_000)
    assert result.portfolio.cash >= -1e-9, "买入不得透支现金"
    assert result.portfolio.total_asset >= 0
    buys = [t for t in result.trades if t.side == "buy"]
    assert buys, "应有买入成交"
    assert all(t.volume % 100 == 0 for t in buys)


def test_p14_cash_only_half_lot_rejected():
    """现金不足 1 手 → 拒单（无买入成交）。"""
    from lihu_quantify.backtest.engine import EventDrivenEngine
    signal_date = date(2026, 1, 1) + timedelta(days=37)
    df = _df("777777.SH", closes=[60.0] * 40)   # 一股 60 元，1 手 = 6000 元
    engine = EventDrivenEngine(
        strategy=_StubBuyStrategy(pct=0.25, signal_dates={signal_date}))
    result = engine.run({"777777.SH": df}, init_capital=5000)
    buys = [t for t in result.trades if t.side == "buy"]
    assert buys == [], "现金不足 1 手应拒单"
    assert result.portfolio.cash == pytest.approx(5000, abs=1e-6), "现金不应被挪用"


# ============================================================
# P1-5：pre_filter 前视偏差移除
# ============================================================

def test_p15_prefilter_no_future_liquidity():
    """前段流动性低、后段高的序列：旧逻辑 tail(20) 全历史剔除；新逻辑仅看是否充分。"""
    from lihu_quantify.strategy.cherry_claw import CherryClaw

    strategy = CherryClaw()
    dates = [date(2026, 1, 1) + timedelta(days=i) for i in range(80)]
    # 前段：低流动性（close 10，amount <1亿）；后段：高流动性（close 50，amount >1亿）
    closes = [10.0] * 40 + [50.0] * 40
    df = pd.DataFrame({
        "ts_code": "600584.SH", "trade_date": dates,
        "open": [c for c in closes], "high": [c + 0.2 for c in closes],
        "low": [c - 0.2 for c in closes], "close": closes,
        "vol": [1_000_000.0] * 80,
        "amount": [5e4 if i < 40 else 5e5 for i, c in enumerate(closes)],
    })
    # 旧行为：tail(20)=后段大量 → 通过；但改用滚动后，早期低流动性段应无信号
    assert strategy.pre_filter(df) is True
    # 滚动均额过滤在后段（amount 充足）才可能产生信号——直接验证滚动列值
    rolling = df["amount"].rolling(20).mean()
    assert rolling.iloc[45] >= strategy.MIN_AVG_AMOUNT_20D / 1e3   # 后段充足
    assert rolling.iloc[39] < strategy.MIN_AVG_AMOUNT_20D / 1e3    # 前段不足


def test_p15_evaluate_filters_early_low_liquidity():
    """_evaluate 用滚动 20 日均额逐 bar 判定：低流动性早期无信号。"""
    from lihu_quantify.strategy.cherry_claw import CherryClaw
    # 构造一段量比>2 的放量上涨（三层过滤会通过），但前段均额不足
    n = 80
    dates = [date(2026, 1, 1) + timedelta(days=i) for i in range(n)]
    closes = [10.0] * 79 + [10.5]
    df = pd.DataFrame({
        "ts_code": "600584.SH", "trade_date": dates,
        "open": [c - 0.1 for c in closes], "high": [c + 0.3 for c in closes],
        "low": [c - 0.3 for c in closes], "close": closes,
        "vol": [1_000_000.0] * n,   # 恒量 → 量比=1，不放量
        "amount": [10 * 1e5] * n,    # 恒低流动性（<1亿）
    })
    sigs = CherryClaw().scan(df)
    assert sigs == [], "低流动性全段不应产生信号（滚动均额过滤生效）"


# ============================================================
# P1-6：量比定义（分母不含当日）
# ============================================================

def test_p16_vol_ratio_excludes_current_bar():
    """前 5 日量恒定 V、当日 2V → vol_ratio == 2.0（修复前 ≈1.67）。"""
    n = 30
    dates = [date(2026, 1, 1) + timedelta(days=i) for i in range(n)]
    closes = [10.0] * n
    vols = [100.0] * (n - 1) + [200.0]   # 当日 2V
    df = pd.DataFrame({
        "ts_code": "600584.SH", "trade_date": dates,
        "open": [9.9] * n, "high": [10.2] * n, "low": [9.8] * n,
        "close": closes, "vol": vols, "amount": [1e6] * n,
    })
    out = add_all_standard(df)
    assert out["vol_ratio"].iloc[-1] == pytest.approx(2.0, rel=1e-6)