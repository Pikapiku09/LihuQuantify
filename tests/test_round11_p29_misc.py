"""第十一轮 P2-9 杂项清理回归测试。

覆盖：
- P2-9-1 stamp_tax 代码默认与 settings.yaml 对齐（0.0005）
- P2-9-2 chasing_high_threshold 双定义收敛（Strategy 引用 Risk）
- P2-9-3 铁律常量收敛（checklist/position_limit/report 读 settings.risk）
- P2-9-4 classify_market_state 迁入包内模块
- P2-9-6 run_live 复用 DailyScanner.collect_signals
- P2-9-7 告警返回实通道真实结果 + 连续失败写自检文件
- P2-9-9 OMS：同票止损累计 / rebuild cost≤0 跳过 / volume 不符重算
- P2-9-10 xtquant 取价守卫（连续失败计数）
"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path
from unittest.mock import MagicMock

import pandas as pd
import pytest

from lihu_quantify.config import get_settings
from lihu_quantify.market import classify_market_state


# ============================================================
# P2-9-1 / P2-9-2 / P2-9-3 配置与常量收敛
# ============================================================

def test_p29_stamp_tax_aligned_with_yaml():
    st = get_settings()
    assert st.backtest.stamp_tax == 0.0005, "2023.8 后印花税应是万 5"


def test_p29_chasing_threshold_single_source():
    st = get_settings()
    # Strategy 不再独立持有（属性引用 risk 段），两处必然一致
    assert st.strategy.chasing_high_threshold == st.risk.chasing_high_threshold


def test_p29_limits_constants_read_settings(tmp_path, monkeypatch):
    from lihu_quantify.risk import limits as limits_mod
    from lihu_quantify.risk.limits import MAX_SECTOR_POSITION, MAX_SINGLE_POSITION

    st = get_settings()
    assert MAX_SINGLE_POSITION == st.risk.max_single_position == 0.25
    assert MAX_SECTOR_POSITION == st.risk.max_sector_position == 0.40
    # checklist / position_limit 引用同一常量来源
    from lihu_quantify.risk.checklist import ChecklistGate
    from lihu_quantify.risk.position_limit import PositionLimiter

    assert ChecklistGate.MAX_SINGLE_PCT == MAX_SINGLE_POSITION
    assert ChecklistGate.MAX_SECTOR_PCT == MAX_SECTOR_POSITION
    assert PositionLimiter.MAX_SINGLE == MAX_SINGLE_POSITION
    assert PositionLimiter.MAX_SECTOR == MAX_SECTOR_POSITION


def test_p29_import_limits_no_circular():
    import lihu_quantify.risk.limits as m
    assert m.MAX_SINGLE_POSITION > 0


# ============================================================
# P2-9-4 classify_market_state 迁入包内
# ============================================================

def test_p29_market_classify_basic():
    df = pd.DataFrame({
        "trade_date": [date(2026, 8, i) for i in range(1, 25)],
        "close": [100 + i for i in range(24)],  # 持续上涨 → 20 日涨幅为正
    })
    states = classify_market_state(df, window=20)
    # 前 20 根 ret_20d 为 NaN → 未知；最后一根 20 日涨幅 ≥3 → 上涨
    assert states[date(2026, 8, 20)] == "未知"
    assert states[date(2026, 8, 24)] == "上涨"


# ============================================================
# P2-9-6 run_live 复用 DailyScanner.collect_signals
# ============================================================

def test_p29_run_live_delegates_to_dailyscanner(monkeypatch):
    import run_live
    from lihu_quantify.monitor import scheduler as sched_mod

    fake_signals = [(object(), object())]
    captured = {}

    def _fake_collect(self, n=50, days=120):
        captured["n"] = n
        captured["days"] = days
        return {"latest": date(2026, 8, 26), "market_state": "上涨", "codes": [], "signals": fake_signals}

    monkeypatch.setattr(sched_mod.DailyScanner, "collect_signals", _fake_collect)
    signals, market_states, _ = run_live.scan_universe(n=7, days=120)
    assert signals == fake_signals
    assert market_states == {date(2026, 8, 26): "上涨"}
    assert captured["n"] == 7


# ============================================================
# P2-9-7 告警真实性 + 连续失败自检
# ============================================================

def test_p29_alert_self_check_file_after_consec_fails(tmp_path, monkeypatch):
    from lihu_quantify.monitor.alerts import Alerter

    al = Alerter(serverchan_key="fake", enabled=True)
    # 用必失败的 Server酱通道（无邮件），3 次连续失败 → 写自检文件
    al._alert_status_path_override = tmp_path / "alert_status.json"
    monkeypatch.setattr(al, "_push_serverchan", lambda t, d: False)

    for _ in range(2):
        al.send("x", "y", level="error")
    assert not (tmp_path / "alert_status.json").exists(), "连续失败 <3 不应落盘"

    al.send("x", "y", level="error")
    assert (tmp_path / "alert_status.json").exists()
    data = json.loads((tmp_path / "alert_status.json").read_text(encoding="utf-8"))
    assert data["ok"] is False
    assert data["consec_fail"] >= 3

    # 成功后清零并记录 ok
    monkeypatch.setattr(al, "_push_serverchan", lambda t, d: True)
    al.send("x", "y", level="error")
    data = json.loads((tmp_path / "alert_status.json").read_text(encoding="utf-8"))
    assert data["ok"] is True
    assert data["consec_fail"] == 0


def test_p29_alert_returns_real_channel_result(monkeypatch):
    from lihu_quantify.monitor.alerts import Alerter

    al = Alerter(serverchan_key="fake", enabled=True)
    al._alert_status_path_override = Path("x") / "y"
    monkeypatch.setattr(al, "_push_serverchan", lambda t, d: True)
    assert al.send("ok", level="info") is True

    al2 = Alerter(serverchan_key="fake", enabled=True)
    al2._alert_status_path_override = Path("x") / "y"
    monkeypatch.setattr(al2, "_push_serverchan", lambda t, d: False)
    assert al2.send("fail", level="info") is False, "实通道全失败应返回 False"


# ============================================================
# P2-9-9 OMS stop_registry 加固
# ============================================================

class _FakeBroker:
    def __init__(self, positions=None, cost_by_code=None):
        self.positions = positions or []
        self.cost_by_code = cost_by_code or {}
        self.trades = []
        self.bought = []

    def query_positions(self):
        from lihu_quantify.execution.base import PositionInfo
        out = []
        for p in self.positions:
            if p["ts_code"] in self.cost_by_code:
                out.append(PositionInfo(
                    ts_code=p["ts_code"], volume=p["volume"],
                    frozen=p.get("frozen", 0), cost=self.cost_by_code[p["ts_code"]],
                ))
        return out

    def buy(self, ts_code, price, volume, reason=""):
        self.bought.append(volume)
        from lihu_quantify.execution.base import OrderResult
        return OrderResult(success=True, order_id=f"o{len(self.bought)}",
                           filled_volume=volume, filled_price=price)

    def sell(self, ts_code, price, volume, reason=""):
        from lihu_quantify.execution.base import OrderResult
        return OrderResult(success=True, order_id="s1",
                           filled_volume=volume, filled_price=price)

    def get_price(self, ts_code):
        return 100.0


def test_p29_oms_rebuild_cost_zero_skips(tmp_path):
    from lihu_quantify.execution.oms import OrderManagementSystem

    broker = _FakeBroker(positions=[{"ts_code": "600000.SH", "volume": 100}],
                         cost_by_code={"600000.SH": 0.0})  # cost=0
    oms = OrderManagementSystem(broker, persist=False, registry_file=str(tmp_path / "r.json"))
    n = oms.rebuild_stops_from_positions()
    assert n == 0
    assert "600000.SH" not in oms.stop_registry


def test_p29_oms_rebuild_volume_mismatch_recomputes(tmp_path):
    from lihu_quantify.execution.oms import OrderManagementSystem, StopOrder

    broker = _FakeBroker(positions=[{"ts_code": "600000.SH", "volume": 200}],
                         cost_by_code={"600000.SH": 10.0})
    oms = OrderManagementSystem(broker, persist=False, registry_file=str(tmp_path / "r.json"))
    oms.stop_registry["600000.SH"] = StopOrder(ts_code="600000.SH", volume=100, stop_price=9.0)
    n = oms.rebuild_stops_from_positions()
    assert n == 1
    assert oms.stop_registry["600000.SH"].volume == 200
    assert oms.stop_registry["600000.SH"].stop_price == round(10 * 0.92, 2)


def test_p29_oms_stop_aggregation_for_same_ticker(tmp_path):
    from lihu_quantify.execution.oms import OrderManagementSystem
    from lihu_quantify.types import Signal

    broker = _FakeBroker()
    oms = OrderManagementSystem(broker, persist=False, registry_file=str(tmp_path / "r.json"))
    s1 = Signal(kind="buy", ts_code="600519.SH", suggested_price=100.0,
                stop_loss=90.0, take_profit=[110.0], suggested_position_pct=0.25,
                strategy_name="CherryClaw", reason="x")
    oms.place_buy_with_stop(s1, 100, 100.0)
    oms.place_buy_with_stop(s1, 200, 100.0)
    stop = oms.stop_registry["600519.SH"]
    assert stop.volume == 300, "同票二次买入应累计监控数量"


# ============================================================
# P2-9-10 xtquant 取价守卫
# ============================================================

def test_p29_xtquant_price_guard(monkeypatch):
    import sys
    import types
    from lihu_quantify.execution.xtquant_client import MiniQMTClient

    fake_module = types.ModuleType("xtquant")
    fake_xtdata = MagicMock()
    fake_xtdata.get_market_data_ex.return_value = {"600519.SH": pd.DataFrame()}
    fake_module.xtdata = fake_xtdata
    monkeypatch.setitem(sys.modules, "xtquant", fake_module)

    client = MiniQMTClient(qmt_path="x", account_id="1")
    client.get_price("600519.SH")
    client.get_price("600519.SH")
    assert client._price_fail_streak == 2
    assert client._price_fail_threshold == 3
    client.get_price("600519.SH")
    assert client._price_fail_streak == 3
    # 恢复行情后计数清零
    fake_xtdata.get_market_data_ex.return_value = {
        "600519.SH": pd.DataFrame({"close": [100.0]})}
    assert client.get_price("600519.SH") == 100.0
    assert client._price_fail_streak == 0