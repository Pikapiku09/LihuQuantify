"""风控层测试：Checklist 闸门 + 三档止损 + 仓位/板块 + 频率。

基线（docs/开仓前强制Checklist.md 附例）：
    账户总资产 153,619 元，想买 30,000 元，半导体板块现有 37.6%
    → 板块合计 57.1% > 40% → 拒绝买入
"""

from __future__ import annotations

from datetime import date, timedelta

import numpy as np
import pandas as pd
import pytest

from lihu_quantify.risk.checklist import ChecklistGate, CheckContext
from lihu_quantify.risk.frequency import FrequencyGuard
from lihu_quantify.risk.position_limit import PositionLimiter
from lihu_quantify.risk.stop_loss import StopLossManager
from lihu_quantify.types import (
    AccountSnapshot,
    Position,
    Signal,
    TradeRecord,
)


# ============ Checklist 闸门 ============

def test_checklist_sector_reject_baseline():
    """基线：半导体 37.6% + 19.5% = 57.1% > 40% → 拒绝。"""
    total_asset = 153619.0
    # 现有半导体持仓市值 = 37.6% * 153619 ≈ 57761
    semi_mv = 0.376 * total_asset
    positions = [
        Position(ts_code="300001.SZ", volume=1000, cost=50, current_price=57.76, sector="半导体"),
    ]
    # 调整使 market_value = semi_mv
    positions[0].current_price = semi_mv / positions[0].volume

    account = AccountSnapshot(total_asset=total_asset, cash=total_asset - semi_mv, positions=positions)
    signal = Signal(
        kind="buy", ts_code="600584.SH", suggested_price=77.74,
        stop_loss=71.5, take_profit=[86.5, 90.0, 95.0, 103.0],
        suggested_position_pct=0.195, trade_date=date(2026, 8, 19),
    )
    ctx = CheckContext(
        current_price=77.74, ma10=79.35, sector="半导体",
        invest_amount=30000.0,
    )
    result = ChecklistGate().check(signal, account, ctx)
    assert not result.approved, "板块超 40% 应拒绝"
    rejected = result.rejected_items()
    names = [r.name for r in rejected]
    assert "板块集中" in names, f"板块集中应被拒绝，实际拒绝项：{names}"
    # 找到板块项的具体描述
    sector_item = next(r for r in result.items if r.name == "板块集中")
    assert "57" in sector_item.value or "40%" in sector_item.value


def test_checklist_all_pass():
    """构造一个全通过的信号。"""
    account = AccountSnapshot(total_asset=100000, cash=80000, positions=[])
    signal = Signal(
        kind="buy", ts_code="600519.SH", suggested_price=1800,
        stop_loss=1656, take_profit=[1890, 1980, 2070, 2160],
        suggested_position_pct=0.20, trade_date=date(2026, 8, 19),
    )
    ctx = CheckContext(
        current_price=1800, ma10=1790, sector="白酒",
        invest_amount=20000, fundamentals={"logic": "高端白酒回暖"},
    )
    result = ChecklistGate().check(signal, account, ctx)
    assert result.approved, f"应全部通过，拒绝项：{[r.name for r in result.rejected_items()]}"


def test_checklist_missing_stop_loss_rejects():
    """止损未给出 → 拒绝。"""
    account = AccountSnapshot(total_asset=100000, cash=100000, positions=[])
    signal = Signal(
        kind="buy", ts_code="600519.SH", suggested_price=1800,
        stop_loss=None, take_profit=[1890],  # 无止损
        suggested_position_pct=0.20, trade_date=date(2026, 8, 19),
    )
    ctx = CheckContext(current_price=1800, ma10=1790, invest_amount=20000, sector="白酒")
    result = ChecklistGate().check(signal, account, ctx)
    assert not result.approved
    assert any(r.name == "止损预设" for r in result.rejected_items())


def test_checklist_chasing_high_rejects():
    """乖离 10 日线 >8% → 追高拒绝。"""
    account = AccountSnapshot(total_asset=100000, cash=100000, positions=[])
    signal = Signal(
        kind="buy", ts_code="600519.SH", suggested_price=100,
        stop_loss=92, take_profit=[105], suggested_position_pct=0.10,
        trade_date=date(2026, 8, 19),
    )
    # MA10=90, 当前价=100, 乖离 = 100/90-1 = 11.1% > 8%
    ctx = CheckContext(current_price=100, ma10=90, invest_amount=10000, sector="白酒")
    result = ChecklistGate().check(signal, account, ctx)
    assert not result.approved
    assert any(r.name == "追高检查" for r in result.rejected_items())


def test_checklist_psychology_rejects():
    """心理门禁触发 → 拒绝。"""
    account = AccountSnapshot(
        total_asset=100000, cash=100000, psychology_alert=True
    )
    signal = Signal(
        kind="buy", ts_code="600519.SH", suggested_price=1800,
        stop_loss=1656, take_profit=[1890], suggested_position_pct=0.20,
        trade_date=date(2026, 8, 19),
    )
    ctx = CheckContext(current_price=1800, ma10=1790, invest_amount=20000, sector="白酒")
    result = ChecklistGate().check(signal, account, ctx)
    assert not result.approved
    assert any(r.name == "心理门禁" for r in result.rejected_items())


# ============ 三档止损 ============

def test_stop_loss_force_at_minus_8():
    """成本 -8% 触发强制止损。"""
    mgr = StopLossManager()
    pos = Position(ts_code="600584.SH", volume=200, cost=80, current_price=73)
    bar = pd.Series({"close": 73.0, "low": 72.5})
    action = mgr.evaluate(pos, bar, {"ma10": 79, "ma20": 76})
    assert action.kind == "force_stop"


def test_stop_loss_ma_break():
    """修复2：MA10 离场改为收盘判定（原 low<ma10 盘中洗盘过紧）。"""
    mgr = StopLossManager()
    pos = Position(ts_code="600584.SH", volume=200, cost=80, current_price=76)
    # 收盘 76 < MA10 77 → 收盘跌破（不再用 low）
    bar = pd.Series({"close": 76.0, "low": 75.0})
    action = mgr.evaluate(pos, bar, {"ma10": 77, "ma20": 76})
    assert action.kind == "ma_break"


def test_stop_loss_ma_break_low_only_no_trigger():
    """修复2：盘中 low 触及 MA10 但收盘收回 → 不触发（避免洗盘）。"""
    mgr = StopLossManager()
    pos = Position(ts_code="600584.SH", volume=200, cost=80, current_price=79)
    # low=75 触及 MA10=77，但 close=79 收回 → 不触发 ma_break
    bar = pd.Series({"close": 79.0, "low": 75.0})
    action = mgr.evaluate(pos, bar, {"ma10": 77, "ma20": 76})
    assert action.kind != "ma_break"  # 收盘未跌破，不应触发


def test_stop_loss_trailing_stop():
    """修复1：移动止盈——浮盈后从高水位回撤3%触发。"""
    mgr = StopLossManager()
    # 成本 80，高水位 90（浮盈 +12.5%），当前 close=87
    # trail_price = 90 * 0.97 = 87.3，close=87 <= 87.3 → 触发
    pos = Position(ts_code="600584.SH", volume=200, cost=80, current_price=87)
    bar = pd.Series({"close": 87.0, "low": 86.5})
    action = mgr.evaluate(pos, bar, {"ma10": 85, "ma20": 82}, high_water_mark=90.0)
    assert action.kind == "trailing_stop", f"应触发移动止盈，实际 {action.kind}"
    assert "移动止盈" in action.reason


def test_stop_loss_trailing_no_trigger_when_small_profit():
    """修复1：浮盈不足（高水位≤成本）不触发移动止盈。"""
    mgr = StopLossManager()
    # 成本 80，高水位 80（无浮盈），close=79 → 应触发其他（ma_break 或 warn），不是 trailing
    pos = Position(ts_code="600584.SH", volume=200, cost=80, current_price=79)
    bar = pd.Series({"close": 79.0, "low": 78.5})
    action = mgr.evaluate(pos, bar, {"ma10": 78, "ma20": 76}, high_water_mark=80.0)
    assert action.kind != "trailing_stop"


def test_stop_loss_warn_at_minus_3():
    """-3% 预警。"""
    mgr = StopLossManager()
    pos = Position(ts_code="600584.SH", volume=200, cost=80, current_price=77)
    # 成本 80, -3% = 77.6, close=77 < 77.6 → 预警（但 > -5%=76）
    # MA10=76 < low=76.8，不触发破线
    bar = pd.Series({"close": 77.0, "low": 76.8})
    action = mgr.evaluate(pos, bar, {"ma10": 76, "ma20": 75})
    assert action.kind == "warn"


def test_stop_loss_hold_when_profitable():
    """盈利且未触发移动止盈 → 持有。"""
    mgr = StopLossManager()
    pos = Position(ts_code="600584.SH", volume=200, cost=80, current_price=90)
    bar = pd.Series({"close": 90.0, "low": 89.0})
    action = mgr.evaluate(pos, bar, {"ma10": 85, "ma20": 82})
    assert action.kind == "hold"


def test_stop_loss_calc_price():
    """止损价 = min(成本-8%, MA10)。"""
    mgr = StopLossManager()
    # 成本 100, -8%=92; MA10=95 → min(92, 95) = 92
    assert mgr.calc_stop_price(100, ma10=95) == 92.0
    # 成本 100, -8%=92; MA10=88 → min(92, 88) = 88
    assert mgr.calc_stop_price(100, ma10=88) == 88.0


# ============ 仓位/板块限制 ============

def test_position_limiter_single_reject():
    limiter = PositionLimiter()
    # 总资产 10万，已持 2万，再加 1万 → 单票 30% > 25%
    positions = [Position(ts_code="600519.SH", volume=100, cost=180, current_price=200, sector="白酒")]
    account = AccountSnapshot(total_asset=100000, cash=80000, positions=positions)
    ok, reason = limiter.can_add("600519.SH", 10000, account, sector="白酒")
    assert not ok
    assert "25%" in reason


def test_position_limiter_sector_reject():
    limiter = PositionLimiter()
    positions = [Position(ts_code="000001.SZ", volume=1000, cost=10, current_price=30, sector="银行")]
    # 市值 30000 = 30%，再加 15000 银行 → 45% > 40%
    account = AccountSnapshot(total_asset=100000, cash=70000, positions=positions)
    ok, reason = limiter.can_add("600519.SH", 15000, account, sector="银行")
    assert not ok
    assert "40%" in reason or "板块" in reason


def test_position_limiter_pass():
    limiter = PositionLimiter()
    account = AccountSnapshot(total_asset=100000, cash=100000, positions=[])
    ok, reason = limiter.can_add("600519.SH", 20000, account, sector="白酒")
    assert ok, reason


def test_position_limiter_no_averaging_down():
    """铁律：绝不向下补仓。"""
    limiter = PositionLimiter()
    # 持仓亏损
    positions = [Position(ts_code="600584.SH", volume=100, cost=80, current_price=70, sector="半导体")]
    account = AccountSnapshot(total_asset=100000, cash=50000, positions=positions)
    ok, reason = limiter.can_average_down("600584.SH", account)
    assert not ok
    assert "向下补仓" in reason


# ============ 频率控制 ============

def test_frequency_month_limit():
    """同票月内 3 次达上限。"""
    guard = FrequencyGuard()
    today = date(2026, 8, 19)
    trades = [
        TradeRecord("600584.SH", date(2026, 8, 1), "buy", 80, 100),
        TradeRecord("600584.SH", date(2026, 8, 5), "buy", 78, 100),
        TradeRecord("600584.SH", date(2026, 8, 10), "buy", 75, 100),
    ]
    account = AccountSnapshot(total_asset=100000, trades=trades)
    ok, reason = guard.can_trade("600584.SH", account, today)
    assert not ok
    assert "3" in reason or "上限" in reason


def test_frequency_halt_after_losses():
    """连亏 3 笔停手一个月。"""
    guard = FrequencyGuard()
    today = date(2026, 8, 19)
    halt_until = today + timedelta(days=30)
    account = AccountSnapshot(total_asset=100000, halted_until=halt_until)
    ok, reason = guard.can_trade("600584.SH", account, today)
    assert not ok
    assert "停手" in reason


def test_frequency_detect_halt_trigger():
    """检测连亏 3 笔触发停手。"""
    guard = FrequencyGuard()
    today = date(2026, 8, 19)
    trades = [
        TradeRecord("600584.SH", date(2026, 8, 1), "sell", 75, 100, pnl=-500),
        TradeRecord("600584.SH", date(2026, 8, 5), "sell", 74, 100, pnl=-600),
        TradeRecord("600584.SH", date(2026, 8, 10), "sell", 73, 100, pnl=-700),
    ]
    account = AccountSnapshot(total_asset=100000, trades=trades)
    halt, until = guard.should_halt_after_loss("600584.SH", account)
    assert halt
    assert until is not None
    # 停手到期 = 最近亏损日 + 30 天
    assert until == date(2026, 8, 10) + timedelta(days=30)


def test_frequency_pass():
    guard = FrequencyGuard()
    today = date(2026, 8, 19)
    account = AccountSnapshot(total_asset=100000, trades=[])
    ok, _ = guard.can_trade("600519.SH", account, today)
    assert ok
