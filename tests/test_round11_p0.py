"""第十一轮 P0 回归测试（资金安全与链路）。

- P0-3：止损失败单保留（failed_count 递增，≥3 次升级 ERROR 告警）
- P0-4：json 导入统一（filter_stats.json 损坏不再 NameError）
- P0-5：账户快照补全（频率/停手/心理门禁三项闸门恢复生效）
- P0-6：仓位闸门含存量市值（25% 口径补全）
"""
from __future__ import annotations

import json
from datetime import date, timedelta
from types import SimpleNamespace

import pytest


# ============================================================
# P0-3：止损失败单不再被静默清空
# ============================================================

def _stop_scanner(tmp_path, monkeypatch, sell_results, open_price=10.0):
    """构造最小 DailyScanner（绕过 __init__），pending 文件预置 1 条止损。"""
    from lihu_quantify.monitor import scheduler as sched_mod
    from lihu_quantify.monitor.alerts import Alerter
    from lihu_quantify.monitor.scheduler import DailyScanner

    monkeypatch.setattr(sched_mod, "_ROOT", tmp_path)
    (tmp_path / "data").mkdir(parents=True, exist_ok=True)
    (tmp_path / "data" / "pending_stops.json").write_text(json.dumps([
        {"ts_code": "600000.SH", "volume": 1000,
         "stop_price": 9.0, "reason": "price_stop"},
    ]), encoding="utf-8")

    s = DailyScanner.__new__(DailyScanner)
    s.alerter = Alerter()
    it = iter(sell_results)
    s.broker = SimpleNamespace(
        sell=lambda code, price, vol, reason="": next(it),
        trades=[],
    )
    monkeypatch.setattr(
        s, "_fetch_open", lambda code, d: open_price, raising=False)
    return s


def test_stop_failure_kept_with_failed_count(tmp_path, monkeypatch):
    """卖单失败 → 条目保留且 failed_count 跨日递增；第 3 次升级 ERROR 告警。"""
    fail = SimpleNamespace(success=False, msg="资金不足")
    s = _stop_scanner(tmp_path, monkeypatch, [])
    s.broker = SimpleNamespace(sell=lambda *a, **k: fail, trades=[])
    for expect_count in (1, 2, 3):
        s._execute_pending_stops(date(2026, 9, 2))
        pending = json.loads(
            (tmp_path / "data" / "pending_stops.json").read_text(encoding="utf-8"))
        assert len(pending) == 1, "失败单被清空（P0-3 回归）"
        assert pending[0]["ts_code"] == "600000.SH"
        assert pending[0]["failed_count"] == expect_count
    # 第 3 次失败 → ERROR 级告警（触发即时邮件通道）
    errs = [a for a in s.alerter.history if a["level"] == "error"]
    assert errs and "600000.SH" in errs[-1]["title"]


def test_stop_success_removed(tmp_path, monkeypatch):
    """卖单成功 → 条目移除，文件为空列表。"""
    s = _stop_scanner(tmp_path, monkeypatch,
                      [SimpleNamespace(success=True, msg="ok")])
    executed = s._execute_pending_stops(date(2026, 9, 2))
    assert len(executed) == 1 and executed[0]["ts_code"] == "600000.SH"
    pending = json.loads(
        (tmp_path / "data" / "pending_stops.json").read_text(encoding="utf-8"))
    assert pending == []


def test_stop_no_open_price_kept(tmp_path, monkeypatch):
    """无开盘价（停牌/数据缺失）→ 保留不丢弃。"""
    s = _stop_scanner(tmp_path, monkeypatch, [], open_price=0.0)
    s.broker = SimpleNamespace(sell=lambda *a, **k: pytest.fail("不应调用卖单"),
                               trades=[])
    executed = s._execute_pending_stops(date(2026, 9, 2))
    assert executed == []
    pending = json.loads(
        (tmp_path / "data" / "pending_stops.json").read_text(encoding="utf-8"))
    assert len(pending) == 1 and pending[0]["failed_count"] == 1


# ============================================================
# P0-4：json 导入统一（损坏 filter_stats.json 不再 NameError）
# ============================================================

def test_append_filter_stats_survives_corrupt_json(tmp_path, monkeypatch):
    from lihu_quantify.monitor import scheduler as sched_mod

    monkeypatch.setattr(sched_mod, "_ROOT", tmp_path)
    (tmp_path / "data").mkdir(parents=True, exist_ok=True)
    (tmp_path / "data" / "filter_stats.json").write_text("{bad json", encoding="utf-8")
    # 修复前：NameError（局部 _json 与 except json.JSONDecodeError 不一致）
    sched_mod._append_filter_stats({"date": "2026-09-02", "signals": 1})
    records = json.loads(
        (tmp_path / "data" / "filter_stats.json").read_text(encoding="utf-8"))
    assert records and records[-1]["date"] == "2026-09-02"


# ============================================================
# P0-5：账户快照补全 → 三项闸门恢复生效
# ============================================================

def _snapshot_broker(tmp_path, *, trades=3):
    from lihu_quantify.execution.paper_trade import PaperBroker

    b = PaperBroker(init_capital=100_000.0, persist=False,
                    state_file=str(tmp_path / "state.json"))
    for _ in range(trades):
        b.buy("600000.SH", 10.0, 100)   # 同月同票 3 笔买入
    return b


def test_snapshot_carries_trades_and_halt(tmp_path):
    from lihu_quantify.monitor.scheduler import build_account_snapshot

    b = _snapshot_broker(tmp_path)
    snap = build_account_snapshot(b)
    assert len(snap.trades) == 3
    assert all(t.ts_code == "600000.SH" for t in snap.trades)
    assert snap.psychology_alert is None          # 无数据来源 → None（非 False）


def test_frequency_gate_rejects_4th_trade_in_month(tmp_path):
    """月内已 3 笔 → 第 4 笔买入被交易频率闸门拒绝（修复前恒通过）。"""
    from lihu_quantify.monitor.scheduler import build_account_snapshot
    from lihu_quantify.risk.checklist import ChecklistGate
    from lihu_quantify.types import Signal

    b = _snapshot_broker(tmp_path, trades=3)
    gate = ChecklistGate()
    sig = Signal(kind="buy", ts_code="600000.SH", suggested_price=10.0,
                 stop_loss=9.0, take_profit=[11.0], trade_date=date.today())
    item = gate._check_frequency(sig, build_account_snapshot(b))
    assert item.approved is False
    assert "上限" in item.reason or "已达上限" in item.value


def test_halt_gate_rejects_during_halt_period(tmp_path):
    """账户级停手未到期 → 买入被拒。"""
    from lihu_quantify.monitor.scheduler import build_account_snapshot
    from lihu_quantify.risk.checklist import ChecklistGate
    from lihu_quantify.types import Signal

    b = _snapshot_broker(tmp_path, trades=0)
    b.halted_until = date.today() + timedelta(days=20)
    gate = ChecklistGate()
    sig = Signal(kind="buy", ts_code="600000.SH", suggested_price=10.0,
                 stop_loss=9.0, take_profit=[11.0], trade_date=date.today())
    item = gate._check_frequency(sig, build_account_snapshot(b))
    assert item.approved is False and "停手" in item.value


def test_psychology_gate_unknown_branch():
    """psychology_alert=None → 未知分支：不拦截但标注（不伪造为通过）。"""
    from lihu_quantify.risk.checklist import ChecklistGate
    from lihu_quantify.types import AccountSnapshot

    item = ChecklistGate()._check_psychology(AccountSnapshot())
    assert item.approved is True and item.value == "未知"
    item2 = ChecklistGate()._check_psychology(
        AccountSnapshot(psychology_alert=True))
    assert item2.approved is False


# ============================================================
# P0-6：仓位闸门含存量市值（25% 口径）
# ============================================================

def _pos_snapshot(existing_pct: float, code="600000.SH"):
    from lihu_quantify.types import AccountSnapshot, Position

    total = 100_000.0
    mv = total * existing_pct
    return AccountSnapshot(
        total_asset=total,
        positions=[Position(ts_code=code, volume=1000,
                            cost=mv / 1000, current_price=mv / 1000)],
    )


def _pos_signal(code="600000.SH"):
    from lihu_quantify.types import Signal

    return Signal(kind="buy", ts_code=code, suggested_price=10.0,
                  stop_loss=9.0, take_profit=[11.0])


def test_position_gate_includes_existing_mv():
    """已持 20% 再买 10% → 拒绝（旧逻辑只看本笔会放行）。"""
    from lihu_quantify.risk.checklist import ChecklistGate, CheckContext

    gate = ChecklistGate()
    ctx = CheckContext(invest_amount=10_000.0)   # 10%
    item = gate._check_position(_pos_signal(), _pos_snapshot(0.20), ctx)
    assert item.approved is False
    assert "30.0%" in item.value                 # 存量20% + 本笔10%


def test_position_gate_first_buy_bounds():
    """首买（无存量）：25% 通过、26% 拒绝。"""
    from lihu_quantify.risk.checklist import CheckContext, ChecklistGate
    from lihu_quantify.types import AccountSnapshot

    gate = ChecklistGate()
    acc = AccountSnapshot(total_asset=100_000.0)
    ok = gate._check_position(_pos_signal(), acc,
                              CheckContext(invest_amount=25_000.0))
    assert ok.approved is True
    bad = gate._check_position(_pos_signal(), acc,
                               CheckContext(invest_amount=26_000.0))
    assert bad.approved is False
    # 存量 20% 时本笔 5% 恰好达线 → 通过
    edge = gate._check_position(_pos_signal(), _pos_snapshot(0.20),
                                CheckContext(invest_amount=5_000.0))
    assert edge.approved is True
