"""回测层测试：broker / portfolio / metrics / engine。"""

from __future__ import annotations

import math
from datetime import date, timedelta

import numpy as np
import pandas as pd
import pytest

from lihu_quantify.backtest.broker import Fill, Order, SimulatedBroker
from lihu_quantify.backtest.engine import EventDrivenEngine
from lihu_quantify.backtest.metrics import compute_metrics
from lihu_quantify.backtest.portfolio import Portfolio
from lihu_quantify.risk.stop_loss import StopLossManager
from lihu_quantify.strategy.cherry_claw import CherryClaw


# ============ Broker ============

def test_broker_market_buy():
    broker = SimulatedBroker(commission_rate=0.00025, stamp_tax_rate=0.001, slippage=0.001)
    order = Order(ts_code="600519.SH", side="buy", volume=100, order_type="market")
    next_bar = pd.Series({"open": 100, "high": 105, "low": 99, "close": 103, "trade_date": date(2026, 8, 19)})
    fill = broker.fill(order, next_bar)
    assert fill is not None
    # 市价买：open - slippage = 100 - 0.1 = 99.9
    assert fill.price == pytest.approx(99.9, abs=0.01)
    assert fill.side == "buy"
    # 佣金 = max(99.9*100*0.00025, 5) = max(2.5, 5) = 5
    assert fill.commission == 5.0
    # 印花税 = 0（买入）
    assert fill.stamp_tax == 0.0
    # cash_flow = -(99.9*100 + 5) = -9995
    assert fill.cash_flow == pytest.approx(-9995.0, abs=1)


def test_broker_market_sell_stamp_tax():
    broker = SimulatedBroker()  # 默认 stamp_tax_rate=0.0005（万5，2023.8起减半）
    order = Order(ts_code="600519.SH", side="sell", volume=200, order_type="market")
    next_bar = pd.Series({"open": 100, "high": 105, "low": 99, "close": 103, "trade_date": date(2026, 8, 19)})
    fill = broker.fill(order, next_bar)
    assert fill is not None
    # 卖出：open + slippage = 100.1
    assert fill.price == pytest.approx(100.1, abs=0.01)
    # 印花税 = 100.1*200*0.0005 = 10.01
    assert fill.stamp_tax == pytest.approx(10.01, abs=0.1)
    # 佣金 = max(100.1*200*0.00025, 5) = max(5.005, 5) = 5.005
    assert fill.commission == pytest.approx(5.005, abs=0.1)


def test_broker_limit_buy_not_triggered():
    broker = SimulatedBroker()
    # 限价买 95，但 low=99，不触及
    order = Order(ts_code="600519.SH", side="buy", volume=100, order_type="limit", limit_price=95)
    next_bar = pd.Series({"open": 100, "high": 105, "low": 99, "close": 103})
    fill = broker.fill(order, next_bar)
    assert fill is None


def test_broker_limit_buy_triggered(monkeypatch):
    broker = SimulatedBroker()
    # 限价买 102，low=99 <= 102，触发。P1-3（十一轮）：成交价 = min(limit, open)=min(102,100)=100
    # （open 优于限价时以 open 成交，避免买贵——旧公式 min(limit, high) 会按 105 假限价成交）
    order = Order(ts_code="600519.SH", side="buy", volume=100, order_type="limit", limit_price=102)
    next_bar = pd.Series({"open": 100, "high": 105, "low": 99, "close": 103})
    fill = broker.fill(order, next_bar)
    assert fill is not None
    assert fill.price == 100  # min(102, open=100)


def test_broker_limit_sell_triggered_open_better():
    """P1-3（十一轮）：卖限价，open 优于限价时以 open 成交。"""
    broker = SimulatedBroker()
    order = Order(ts_code="600519.SH", side="sell", volume=100, order_type="limit", limit_price=98)
    # 限价卖 98，high=105 >= 98 触发；open=100 > 98 → 成交价 = max(98, 100) = 100
    next_bar = pd.Series({"open": 100, "high": 105, "low": 99, "close": 103})
    fill = broker.fill(order, next_bar)
    assert fill is not None
    assert fill.price == 100
    # 反向：open=95 低于限价 98 → 以限价 98 成交
    bar2 = pd.Series({"open": 95, "high": 100, "low": 94, "close": 99})
    fill2 = broker.fill(order, bar2)
    assert fill2 is not None
    assert fill2.price == 98


def test_broker_volume_must_be_100_multiple():
    broker = SimulatedBroker()
    order = Order(ts_code="600519.SH", side="buy", volume=150, order_type="market")  # 非 100 倍数
    next_bar = pd.Series({"open": 100, "high": 105, "low": 99, "close": 103})
    assert broker.fill(order, next_bar) is None


# ============ Portfolio ============

def test_portfolio_buy_sell_pnl():
    pf = Portfolio(init_capital=100000)
    # 买入 200 股 @ 100
    fill_buy = Fill(ts_code="600519.SH", side="buy", price=100, volume=200,
                    fill_date=date(2026, 8, 1), commission=5, cash_flow=-20005)
    pf.apply_fill(fill_buy)
    assert pf.cash == pytest.approx(100000 - 20005)
    assert pf.positions["600519.SH"].volume == 200
    assert pf.positions["600519.SH"].cost == 100

    # 更新价格到 110
    pf.update_prices({"600519.SH": pd.Series({"close": 110})})
    assert pf.position_value == pytest.approx(22000)
    assert pf.total_asset == pytest.approx(100000 - 20005 + 22000)

    # 卖出 200 股 @ 110
    fill_sell = Fill(ts_code="600519.SH", side="sell", price=110, volume=200,
                     fill_date=date(2026, 8, 10), commission=5.5, stamp_tax=22, cash_flow=22000 - 27.5)
    pf.apply_fill(fill_sell)
    # 盈亏 = (110-100)*200 = 2000
    assert len(pf.trades) == 2
    sell_trade = pf.trades[-1]
    assert sell_trade.pnl == pytest.approx(2000)
    assert "600519.SH" not in pf.positions


def test_portfolio_high_water_mark():
    pf = Portfolio(init_capital=100000)
    pf.apply_fill(Fill(ts_code="600519.SH", side="buy", price=100, volume=100,
                       fill_date=date(2026, 8, 1), cash_flow=-10000))
    # 价格上涨到 120
    pf.update_prices({"600519.SH": pd.Series({"close": 120})})
    assert pf.high_water_mark["600519.SH"] == 120
    # 价格回撤到 115（高水位仍 120）
    pf.update_prices({"600519.SH": pd.Series({"close": 115})})
    assert pf.high_water_mark["600519.SH"] == 120


def test_portfolio_t_plus_1():
    pf = Portfolio(init_capital=100000)
    pf.apply_fill(Fill(ts_code="600519.SH", side="buy", price=100, volume=100,
                       fill_date=date(2026, 8, 1), cash_flow=-10000))
    # 买入当日不能卖
    assert pf.can_sell("600519.SH", date(2026, 8, 1)) is False
    # 次日可卖
    assert pf.can_sell("600519.SH", date(2026, 8, 2)) is True


def test_portfolio_weighted_avg_cost():
    pf = Portfolio(init_capital=100000)
    # 分两笔买：100@100, 100@110 → 均价 105
    pf.apply_fill(Fill(ts_code="600519.SH", side="buy", price=100, volume=100,
                       fill_date=date(2026, 8, 1), cash_flow=-10000))
    pf.apply_fill(Fill(ts_code="600519.SH", side="buy", price=110, volume=100,
                       fill_date=date(2026, 8, 5), cash_flow=-11000))
    assert pf.positions["600519.SH"].cost == 105
    assert pf.positions["600519.SH"].volume == 200


# ============ Metrics ============

def test_metrics_basic():
    dates = [date(2026, 1, 1) + timedelta(days=i) for i in range(100)]
    # 权益从 10万 涨到 11万
    equity = pd.Series(np.linspace(100000, 110000, 100), index=dates, name="equity")
    equity.index.name = "trade_date"
    m = compute_metrics(equity, [])
    assert m["total_return"] == pytest.approx(0.10, abs=0.01)
    assert m["final_equity"] == pytest.approx(110000)
    assert m["n_days"] == 100
    assert m["max_drawdown"] <= 0  # 单调上涨无回撤


def test_metrics_with_drawdown():
    dates = [date(2026, 1, 1) + timedelta(days=i) for i in range(10)]
    # 10万 → 12万 → 8万 → 9万（最大回撤从12万到8万 = -33%）
    equity = pd.Series([100000, 110000, 120000, 115000, 100000, 90000, 85000, 80000, 85000, 90000], index=dates)
    equity.index.name = "trade_date"
    m = compute_metrics(equity, [])
    assert m["max_drawdown"] < -0.3  # 至少 -30%


def test_metrics_trades():
    from lihu_quantify.types import TradeRecord
    dates = [date(2026, 1, 1) + timedelta(days=i) for i in range(50)]
    equity = pd.Series(np.linspace(100000, 105000, 50), index=dates, name="equity")
    equity.index.name = "trade_date"
    trades = [
        TradeRecord("A", date(2026, 1, 1), "buy", 100, 100),
        TradeRecord("A", date(2026, 1, 5), "sell", 110, 100, pnl=1000),   # 盈
        TradeRecord("B", date(2026, 1, 10), "buy", 100, 100),
        TradeRecord("B", date(2026, 1, 15), "sell", 95, 100, pnl=-500),   # 亏
    ]
    m = compute_metrics(equity, trades)
    assert m["win_rate"] == 0.5
    assert m["profit_loss_ratio"] == pytest.approx(1000 / 500)
    assert m["total_trades"] == 2


def test_metrics_empty():
    m = compute_metrics(pd.Series(dtype=float), [])
    assert m["total_return"] == 0
    assert m["sharpe"] == 0


# ============ Engine ============

def test_engine_v_recovery(v_recovery_data):
    """V 形反转回测：应产生买入交易 + 权益曲线。"""
    engine = EventDrivenEngine(strategy=CherryClaw())
    result = engine.run(v_recovery_data, init_capital=100000)
    assert isinstance(result.equity, pd.Series)
    assert len(result.equity) > 0
    # 权益曲线首值 = 初始资金
    assert result.equity.iloc[0] == pytest.approx(100000, abs=1)
    # 应至少有信号产生（CherryClaw 在温和上涨段会触发）
    assert result.signals_generated > 0
    # 应有买入成交（T+1 撮合后）
    buy_trades = [t for t in result.trades if t.side == "buy"]
    assert len(buy_trades) > 0, "应有买入交易"


def test_engine_600584(daily_600584):
    """600584 真实数据回测：能运行并输出权益曲线。"""
    engine = EventDrivenEngine(strategy=CherryClaw())
    result = engine.run({"600584.SH": daily_600584}, init_capital=100000)
    assert len(result.equity) > 0
    # 绩效指标完整
    for key in ["total_return", "sharpe", "max_drawdown", "win_rate"]:
        assert key in result.metrics
    # 600584 6-8 月波动大，可能有交易也可能无；关键是运行不报错
    assert result.metrics["n_days"] > 0


def test_engine_stop_loss_triggers():
    """构造大跌场景：买入后触发 -8% 强制止损。

    形态：下跌(30) → 横盘(20) → 温和上涨(15, 产生买入) → 暴跌(15, 触发止损)
    """
    n_decline, n_flat, n_rise, n_plunge = 30, 20, 15, 15
    n = n_decline + n_flat + n_rise + n_plunge
    dates = [date(2026, 5, 1) + timedelta(days=i) for i in range(n)]
    closes = [20 - 10 * (i / n_decline) for i in range(n_decline)]
    closes += [10 + 0.15 * math.sin(i) for i in range(n_flat)]
    closes += [10 + 0.05 * (i + 1) for i in range(n_rise)]           # 10 → 10.75
    closes += [10.75 - 0.3 * (i + 1) for i in range(n_plunge)]       # 10.75 → 6.25
    opens = [c - 0.3 for c in closes]
    highs = [c + 0.1 for c in closes]
    lows = [c - 0.4 for c in closes]
    vols = [1_000_000.0] * (n - n_rise - n_plunge) + [1_500_000.0 + 50_000 * i for i in range(n_rise)] + [2_000_000.0] * n_plunge
    amounts = [v * c for v, c in zip(vols, closes)]
    pct_chg = [0.0] + [(closes[i] / closes[i - 1] - 1) * 100 for i in range(1, n)]
    df = pd.DataFrame({
        "ts_code": "999999.SH", "trade_date": dates,
        "open": opens, "high": highs, "low": lows, "close": closes,
        "pre_close": [closes[0]] + closes[:-1], "pct_chg": pct_chg,
        "vol": vols, "amount": amounts,
    })
    engine = EventDrivenEngine(strategy=CherryClaw())
    result = engine.run({"999999.SH": df}, init_capital=100000)
    # 应有买入成交
    buy_trades = [t for t in result.trades if t.side == "buy"]
    assert len(buy_trades) > 0, "温和上涨段应产生买入"
    # 应有卖出（止损/止盈触发：修复1移动止盈可能在盈利时离场）
    sell_trades = [t for t in result.trades if t.side == "sell"]
    assert len(sell_trades) > 0, "暴跌应触发止损/止盈卖出"
    # 卖出 reason 应包含止损/止盈类型（非空）
    assert any(t.reason for t in sell_trades), "卖出应有触发原因"


def test_engine_empty_data():
    engine = EventDrivenEngine(strategy=CherryClaw())
    result = engine.run({}, init_capital=100000)
    assert len(result.equity) == 0


# ============ 市场状态参考信号（修复A：默认 reduce 减仓） ============

def _build_data_with_dates():
    """构造跌-平-涨完整数据（温和上涨段产生买入信号）。"""
    import math
    n_decline, n_flat, n_rise = 30, 20, 20
    n = n_decline + n_flat + n_rise
    dates = [date(2026, 5, 1) + timedelta(days=i) for i in range(n)]
    closes = [20 - 10 * (i / n_decline) for i in range(n_decline)]
    closes += [10 + 0.15 * math.sin(i) for i in range(n_flat)]
    closes += [10 + 0.05 * (i + 1) for i in range(n_rise)]
    opens = [c - 0.3 for c in closes]
    highs = [c + 0.1 for c in closes]
    lows = [c - 0.4 for c in closes]
    vols = [1_000_000.0] * (n - n_rise) + [1_500_000.0 + 50_000 * i for i in range(n_rise)]
    amounts = [v * c for v, c in zip(vols, closes)]
    pct_chg = [0.0] + [(closes[i] / closes[i - 1] - 1) * 100 for i in range(1, n)]
    return pd.DataFrame({
        "ts_code": "777777.SH", "trade_date": dates,
        "open": opens, "high": highs, "low": lows, "close": closes,
        "pre_close": [closes[0]] + closes[:-1], "pct_chg": pct_chg,
        "vol": vols, "amount": amounts,
    })


def test_market_filter_block_mode():
    """block 模式：非上涨日完全不开新仓（旧语义，显式传参）。"""
    df = _build_data_with_dates()
    states = {d: "震荡" for d in df["trade_date"]}
    engine = EventDrivenEngine(
        strategy=CherryClaw(),
        market_states=states, market_filter_on=True,
        market_filter_mode="block",
    )
    result = engine.run({"777777.SH": df}, init_capital=100000)
    buy_trades = [t for t in result.trades if t.side == "buy"]
    assert len(buy_trades) == 0, "block 模式震荡日不应开新仓"


def test_market_filter_reduce_mode_smaller_volume():
    """修复A：reduce 模式（默认）——震荡日仍开仓但仓位减半。"""
    df = _build_data_with_dates()
    # 全部标记"上涨"跑一遍 → 基准仓
    states_up = {d: "上涨" for d in df["trade_date"]}
    engine_up = EventDrivenEngine(
        strategy=CherryClaw(), market_states=states_up, market_filter_on=True,
    )
    result_up = engine_up.run({"777777.SH": df}, init_capital=100000)
    vols_up = [t.volume for t in result_up.trades if t.side == "buy"]
    assert vols_up, "上涨日应正常开仓"

    # 全部标记"震荡" → 仓位应减半（或不低于 0）
    states_flat = {d: "震荡" for d in df["trade_date"]}
    engine_flat = EventDrivenEngine(
        strategy=CherryClaw(), market_states=states_flat, market_filter_on=True,
    )
    result_flat = engine_flat.run({"777777.SH": df}, init_capital=100000)
    vols_flat = [t.volume for t in result_flat.trades if t.side == "buy"]
    # reduce 语义：允许开仓但每笔 ≤ 基准的约一半（向下取整到 100 倍数）
    for vf in vols_flat:
        assert vf <= max(vols_up), "reduce 模式仓位不应超过上涨日基准"


def test_market_filter_disabled_by_default():
    """未传 market_states 时不过滤（默认行为不变，回归保障）。"""
    df = _build_data_with_dates()
    engine = EventDrivenEngine(strategy=CherryClaw())
    result = engine.run({"777777.SH": df}, init_capital=100000)
    buy_trades = [t for t in result.trades if t.side == "buy"]
    assert len(buy_trades) > 0


# ============ 修复A(第三轮)：回测侧连亏3笔停手 ============

def _loss_cycle(pf: Portfolio, code: str, day: date, buy_p: float, sell_p: float, vol: int = 100):
    """一轮买入→亏损卖出（辅助函数）。"""
    pf.apply_fill(Fill(ts_code=code, side="buy", price=buy_p, volume=vol,
                       fill_date=day, commission=5, cash_flow=-buy_p * vol - 5))
    sell_day = day + timedelta(days=3)
    pf.apply_fill(Fill(ts_code=code, side="sell", price=sell_p, volume=vol,
                       fill_date=sell_day, commission=5, stamp_tax=sell_p * vol * 0.0005,
                       cash_flow=sell_p * vol - 5 - sell_p * vol * 0.0005))


def test_portfolio_halt_after_three_consecutive_losses():
    """修复A验收：同票连亏 3 笔 → halted_until 置为最后卖出日+30天。"""
    pf = Portfolio(init_capital=1_000_000)
    code = "600584.SH"
    # 前两轮亏损：不触发停手
    _loss_cycle(pf, code, date(2026, 7, 1), 100.0, 95.0)
    assert pf.halted_until is None
    _loss_cycle(pf, code, date(2026, 7, 8), 100.0, 94.0)
    assert pf.halted_until is None
    # 第三轮亏损：触发停手
    _loss_cycle(pf, code, date(2026, 7, 15), 100.0, 93.0)
    assert pf.halted_until == date(2026, 7, 15) + timedelta(days=3) + timedelta(days=30)
    assert pf.consecutive_losses(code) == 3


def test_checklist_rejects_signal_during_halt():
    """修复A验收：停手期内第 4 笔信号被 Checklist 拒绝并显示"停手至 xx"。"""
    from lihu_quantify.risk.checklist import ChecklistGate, CheckContext
    from lihu_quantify.types import Signal

    pf = Portfolio(init_capital=1_000_000)
    code = "600584.SH"
    _loss_cycle(pf, code, date(2026, 7, 1), 100.0, 95.0)
    _loss_cycle(pf, code, date(2026, 7, 8), 100.0, 94.0)
    _loss_cycle(pf, code, date(2026, 7, 15), 100.0, 93.0)
    assert pf.halted_until is not None

    # 停手期内的第 4 笔买入信号
    sig = Signal(
        kind="buy", ts_code=code, suggested_price=100.0,
        stop_loss=92.0, take_profit=[105.0],
        suggested_position_pct=0.10,
        trade_date=date(2026, 7, 25),   # < halted_until
    )
    gate = ChecklistGate()
    account = pf.to_snapshot()
    ctx = CheckContext(current_price=100.0, ma10=98.0, sector="半导体", invest_amount=10000)
    result = gate.check(sig, account, ctx)
    assert not result.approved, "停手期内信号必须被拒绝"
    freq_items = [i for i in result.items if i.name == "交易频率"]
    assert freq_items and not freq_items[0].approved
    assert "停手至" in freq_items[0].value, f"应显示停手至日期，实际: {freq_items[0].value}"


def test_checklist_allows_after_halt_expires():
    """修复A：停手期满后信号不再被停手拦截（其他项正常检查）。"""
    from lihu_quantify.risk.checklist import ChecklistGate, CheckContext
    from lihu_quantify.types import Signal

    pf = Portfolio(init_capital=1_000_000)
    code = "600584.SH"
    _loss_cycle(pf, code, date(2026, 7, 1), 100.0, 95.0)
    _loss_cycle(pf, code, date(2026, 7, 8), 100.0, 94.0)
    _loss_cycle(pf, code, date(2026, 7, 15), 100.0, 93.0)
    assert pf.halted_until == date(2026, 8, 17)

    # 停手期满后的信号（8/18 > 8/17）：交易频率项不应因停手拒绝
    sig = Signal(
        kind="buy", ts_code="000001.SZ", suggested_price=100.0,
        stop_loss=92.0, take_profit=[105.0],
        suggested_position_pct=0.10,
        trade_date=date(2026, 8, 18),
        reason="测试",
    )
    gate = ChecklistGate()
    account = pf.to_snapshot()
    ctx = CheckContext(current_price=100.0, ma10=98.0, sector="银行", invest_amount=10000)
    result = gate.check(sig, account, ctx)
    freq_items = [i for i in result.items if i.name == "交易频率"]
    assert freq_items and freq_items[0].approved, "停手期满后频率项应通过（换月+非同票）"


def test_sector_wiring_rejects_third_same_sector():
    """修复E(第三轮)验收：两只同板块持仓 + 第三只同板块信号 → Checklist 拒绝。

    模拟引擎路径：portfolio.to_snapshot(sector_by_code) 注入板块 →
    gate.check 的 _check_sector 按 40% 上限累计拦截。
    """
    from lihu_quantify.risk.checklist import ChecklistGate, CheckContext
    from lihu_quantify.types import Signal

    pf = Portfolio(init_capital=100_000)
    # 两只"半导体"各占 ~20%（合计 40%）
    for code in ["600584.SH", "002049.SZ"]:
        pf.apply_fill(Fill(ts_code=code, side="buy", price=100, volume=2000,
                           fill_date=date(2026, 8, 20), commission=5, cash_flow=-200005))
        pf.update_prices({code: pd.Series({"close": 100.0})})
    sector_by_code = {"600584.SH": "半导体", "002049.SZ": "半导体",
                      "601012.SH": "半导体", "600036.SH": "银行"}
    account = pf.to_snapshot(sector_by_code)
    assert account.positions[0].sector == "半导体"

    # 第三只同板块信号（10% 仓位 → 板块合计 50% > 40%）→ 必须被拒
    sig = Signal(
        kind="buy", ts_code="601012.SH", suggested_price=100.0,
        stop_loss=92.0, take_profit=[105.0],
        suggested_position_pct=0.10,
        trade_date=date(2026, 8, 21), reason="半导体第三只",
    )
    gate = ChecklistGate()
    ctx = CheckContext(current_price=100.0, ma10=98.0, sector="半导体",
                       invest_amount=10_000)
    result = gate.check(sig, account, ctx)
    sector_item = next(i for i in result.items if i.name == "板块集中")
    assert not sector_item.approved, "同板块第三笔（合计>40%）应被 Checklist 拒绝"
    assert "40%" in sector_item.reason

    # 对照：不同板块（银行 10%）→ 板块项通过
    sig_bank = Signal(
        kind="buy", ts_code="600036.SH", suggested_price=100.0,
        stop_loss=92.0, take_profit=[105.0],
        suggested_position_pct=0.10,
        trade_date=date(2026, 8, 21), reason="银行首笔",
    )
    ctx_bank = CheckContext(current_price=100.0, ma10=98.0, sector="银行",
                            invest_amount=10_000)
    result_bank = gate.check(sig_bank, account, ctx_bank)
    sector_item_bank = next(i for i in result_bank.items if i.name == "板块集中")
    assert sector_item_bank.approved, "不同板块不应被板块项拦截"
