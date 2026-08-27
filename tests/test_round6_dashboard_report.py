"""第六轮（看板与报告）测试：移动止盈 / 交易记录 pnl+reason / 看板合并 / rich 报告。

对应 docs/第六轮修复清单_看板与报告_for_Trae.md：
    修复1 .md 报告 rich 版（report.py，scheduler 传 rich）
    修复2 看板持仓表补名称/现价/市值/浮盈亏（server._merge_positions）
    修复3 首页真实监控指标（server._live_metrics）
    修复4 PaperBroker high_water_mark + trailing 止盈 + sell 记录 pnl/reason
"""

from __future__ import annotations

import importlib.util
import json
from datetime import date
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from lihu_quantify.config import RiskConfig
from lihu_quantify.execution.oms import OrderManagementSystem, StopOrder
from lihu_quantify.execution.paper_trade import PaperBroker
from lihu_quantify.monitor.report import ReportGenerator

ROOT = Path(__file__).resolve().parents[1]


# ============ 修复4：PaperBroker 交易记录 pnl/reason ============

def _broker(tmp_path) -> PaperBroker:
    b = PaperBroker(init_capital=1_000_000.0, persist=True,
                    state_file=str(tmp_path / "state.json"))
    b.on_new_day(date(2026, 8, 27))
    return b


def test_sell_records_pnl_and_reason(tmp_path):
    """卖出记录含 pnl（加权成本-卖出费用）与 reason。"""
    b = _broker(tmp_path)
    b.buy("600000.SH", 10.0, 1000)          # 佣金 max(10000*0.00025, 5)=5
    b.on_new_day(date(2026, 8, 28))          # 解冻 T+1
    b.sell("600000.SH", 10.5, 1000, reason="移动止盈")
    sell = [t for t in b.trades if t["side"] == "sell"][-1]
    # pnl = (10.5-10)*1000 - 5(佣金) - 5.25(印花税) = 489.75
    assert abs(sell["pnl"] - 489.75) < 1e-9
    assert sell["reason"] == "移动止盈"
    # 买入记录带 reason 默认空串（不缺失字段）
    buy = [t for t in b.trades if t["side"] == "buy"][-1]
    assert "reason" in buy


def test_buy_reason_recorded(tmp_path):
    """买入记录携带策略原因（OMS place_buy_with_stop 传入）。"""
    from lihu_quantify.types import Signal

    b = _broker(tmp_path)
    sig = Signal(kind="buy", ts_code="600000.SH", suggested_price=10.0,
                 stop_loss=9.5, suggested_position_pct=0.10,
                 reason="三层过滤通过")
    oms = OrderManagementSystem(b, registry_file=str(tmp_path / "stop.json"))
    buy, stop = oms.place_buy_with_stop(sig, 1000, 10.0)
    assert buy.success
    rec = [t for t in b.trades if t["side"] == "buy"][-1]
    assert rec["reason"] == "三层过滤通过"


# ============ 修复4：high_water_mark 生命周期 ============

def test_buy_initializes_high_water(tmp_path):
    """买入即以成交价起算高水位（同回测 portfolio.apply_fill）。"""
    b = _broker(tmp_path)
    b.buy("600000.SH", 10.0, 1000)
    assert b.high_water_mark["600000.SH"] == 10.0


def test_update_high_water_only_raises(tmp_path):
    """高水位只升不降。"""
    b = _broker(tmp_path)
    b.update_high_water("A.SH", 10.0)
    b.update_high_water("A.SH", 12.0)
    b.update_high_water("A.SH", 9.5)   # 回落不降
    assert b.high_water_mark["A.SH"] == 12.0
    b.update_high_water("A.SH", 0)     # 非法价忽略
    assert b.high_water_mark["A.SH"] == 12.0


def test_high_water_persisted_roundtrip(tmp_path):
    """高水位持久化：重启恢复。"""
    b = _broker(tmp_path)
    b.buy("600000.SH", 10.0, 1000)
    b.update_high_water("600000.SH", 11.2)
    b2 = PaperBroker(init_capital=1_000_000.0,
                     state_file=str(tmp_path / "state.json"))
    assert b2.high_water_mark.get("600000.SH") == 11.2


def test_full_sell_clears_high_water(tmp_path):
    """清仓清理高水位（同回测 portfolio）。"""
    b = _broker(tmp_path)
    b.buy("600000.SH", 10.0, 1000)
    b.update_high_water("600000.SH", 11.0)
    b.on_new_day(date(2026, 8, 28))
    b.sell("600000.SH", 10.5, 1000)
    assert "600000.SH" not in b.high_water_mark
    # 持久化后重启亦无残留
    b2 = PaperBroker(init_capital=1_000_000.0,
                     state_file=str(tmp_path / "state.json"))
    assert "600000.SH" not in b2.high_water_mark


# ============ 修复4：scheduler 移动止盈判定 ============

def _scanner_stub(broker, settings=None):
    """绕过 __init__ 的 DailyScanner stub。"""
    from lihu_quantify.monitor.scheduler import DailyScanner

    s = DailyScanner.__new__(DailyScanner)
    s.broker = broker
    s.alerter = MagicMock()
    s.settings = settings or MagicMock(risk=RiskConfig())
    s.mode = "paper"
    return s


def test_trailing_stop_registered(tmp_path, monkeypatch):
    """浮盈≥3%后从高水位回撤3% → 登记 trailing_stop 待执行（而非等 MA10 破位）。"""
    from lihu_quantify.monitor import scheduler as sched_mod

    broker = _broker(tmp_path)
    broker.buy("600000.SH", 10.0, 1000)          # 成本 10.0，hwm=10.0
    broker.update_high_water("600000.SH", 11.0)  # 高水位 11.0（浮盈 10%）
    broker.set_price("600000.SH", 10.6)          # 回撤至 10.6 ≤ 11×0.97=10.67

    oms = OrderManagementSystem(broker, registry_file=str(tmp_path / "stop.json"),
                                persist=False)
    oms.stop_registry["600000.SH"] = StopOrder(
        ts_code="600000.SH", volume=1000, stop_price=9.0,
    )

    monkeypatch.setattr(sched_mod, "_ROOT", tmp_path)
    s = _scanner_stub(broker)
    # MA10 在下方（不构成破位）：close 10.6 > MA10 10.0
    monkeypatch.setattr(s, "_fetch_ma10", lambda code, latest: 10.0)

    executed, new_pending = s._check_stops_with_alert(oms, date(2026, 8, 28))

    assert executed == []
    assert len(new_pending) == 1
    assert new_pending[0]["reason"] == "trailing_stop"
    assert new_pending[0]["stop_price"] == pytest.approx(10.67)
    # 落盘的待执行同样携带原因
    pending_file = tmp_path / "data" / "pending_stops.json"
    assert pending_file.exists()
    saved = json.loads(pending_file.read_text(encoding="utf-8"))
    assert saved[0]["reason"] == "trailing_stop"
    # 高水位更新（close 10.6 < 11.0 不降）
    assert broker.high_water_mark["600000.SH"] == 11.0


def test_trailing_not_triggered_without_profit(tmp_path, monkeypatch):
    """无浮盈（高水位≤成本）→ 不触发移动止盈。"""
    from lihu_quantify.monitor import scheduler as sched_mod

    broker = _broker(tmp_path)
    broker.buy("600000.SH", 10.0, 1000)   # hwm=cost=10 → hwm > cost 不成立
    broker.set_price("600000.SH", 9.9)

    oms = OrderManagementSystem(broker, registry_file=str(tmp_path / "stop.json"),
                                persist=False)
    oms.stop_registry["600000.SH"] = StopOrder(
        ts_code="600000.SH", volume=1000, stop_price=9.0,
    )
    monkeypatch.setattr(sched_mod, "_ROOT", tmp_path)
    s = _scanner_stub(broker)
    monkeypatch.setattr(s, "_fetch_ma10", lambda code, latest: 9.5)  # close>MA10 不破位

    _, new_pending = s._check_stops_with_alert(oms, date(2026, 8, 28))
    assert new_pending == []


def test_trailing_pullback_from_settings(tmp_path, monkeypatch):
    """回撤阈值取 settings.risk.trailing_profit_pullback。"""
    from lihu_quantify.monitor import scheduler as sched_mod

    broker = _broker(tmp_path)
    broker.buy("600000.SH", 10.0, 1000)
    broker.update_high_water("600000.SH", 11.0)
    # 阈值 5%：11×0.95=10.45；close 10.5 > 10.45 不触发；close 10.4 触发
    broker.set_price("600000.SH", 10.4)
    oms = OrderManagementSystem(broker, registry_file=str(tmp_path / "stop.json"),
                                persist=False)
    oms.stop_registry["600000.SH"] = StopOrder(
        ts_code="600000.SH", volume=1000, stop_price=9.0,
    )
    monkeypatch.setattr(sched_mod, "_ROOT", tmp_path)
    s = _scanner_stub(broker, settings=MagicMock(risk=RiskConfig(trailing_profit_pullback=0.05)))
    monkeypatch.setattr(s, "_fetch_ma10", lambda code, latest: 10.0)
    _, new_pending = s._check_stops_with_alert(oms, date(2026, 8, 28))
    assert len(new_pending) == 1
    assert new_pending[0]["stop_price"] == pytest.approx(10.45)


def test_pending_execution_passes_reason(tmp_path, monkeypatch):
    """待执行止损执行时 reason（中文标签）写入交易记录。"""
    from lihu_quantify.monitor import scheduler as sched_mod

    broker = _broker(tmp_path)
    broker.buy("600000.SH", 10.0, 1000)
    broker.on_new_day(date(2026, 8, 28))
    broker.set_price("600000.SH", 10.5)

    pending_file = tmp_path / "data" / "pending_stops.json"
    pending_file.parent.mkdir(parents=True, exist_ok=True)
    pending_file.write_text(json.dumps([
        {"ts_code": "600000.SH", "volume": 1000,
         "stop_price": 10.67, "reason": "trailing_stop"},
    ]), encoding="utf-8")

    oms = OrderManagementSystem(broker, registry_file=str(tmp_path / "stop.json"),
                                persist=False)
    monkeypatch.setattr(sched_mod, "_ROOT", tmp_path)
    s = _scanner_stub(broker)
    monkeypatch.setattr(s, "_fetch_open", lambda code, latest: 10.4)

    executed, _ = s._check_stops_with_alert(oms, date(2026, 8, 28))
    assert len(executed) == 1
    assert executed[0]["reason"] == "trailing_stop"
    sell = [t for t in broker.trades if t["side"] == "sell"][-1]
    assert sell["reason"] == "移动止盈"   # 中文标签（看板/日报展示）
    assert sell["pnl"] is not None


def test_sold_position_not_re_registered(tmp_path, monkeypatch):
    """止损执行清仓后，同代码不再被收盘判定重复登记（防误登记）。"""
    from lihu_quantify.monitor import scheduler as sched_mod

    broker = _broker(tmp_path)
    broker.buy("600000.SH", 10.0, 1000)
    broker.on_new_day(date(2026, 8, 28))
    broker.set_price("600000.SH", 9.0)   # 仍低于止损线

    pending_file = tmp_path / "data" / "pending_stops.json"
    pending_file.parent.mkdir(parents=True, exist_ok=True)
    pending_file.write_text(json.dumps([
        {"ts_code": "600000.SH", "volume": 1000,
         "stop_price": 9.2, "reason": "price_stop"},
    ]), encoding="utf-8")

    oms = OrderManagementSystem(broker, registry_file=str(tmp_path / "stop.json"),
                                persist=False)
    oms.stop_registry["600000.SH"] = StopOrder(
        ts_code="600000.SH", volume=1000, stop_price=9.2,
    )
    monkeypatch.setattr(sched_mod, "_ROOT", tmp_path)
    s = _scanner_stub(broker)
    monkeypatch.setattr(s, "_fetch_open", lambda code, latest: 9.1)
    monkeypatch.setattr(s, "_fetch_ma10", lambda code, latest: 9.5)

    executed, new_pending = s._check_stops_with_alert(oms, date(2026, 8, 28))
    assert len(executed) == 1            # 昨日待执行已成交（清仓）
    assert new_pending == []             # 已清仓 → 不重复登记


# ============ 修复1：rich 版 .md 报告 ============

def _rich_summary() -> dict:
    return {
        "trade_date": date(2026, 8, 28), "market_state": "震荡", "mode": "paper",
        "signals": 12, "entry_scale": 0.5,
        "total_asset": 100753.0, "cash": 60000.0, "init_capital": 100000.0,
        "prev_total_asset": 100500.0,
        "positions": [
            {"ts_code": "600584.SH", "name": "长电科技", "volume": 200,
             "cost": 77.7, "price": 78.2, "market_value": 15640.0,
             "stop_price": 71.5, "float_pnl": 100.0,
             "float_pnl_pct": 0.0064, "weight": 0.155},
        ],
        "executed": [
            {"ts_code": "600036.SH", "name": "招商银行", "volume": 600,
             "price": 38.5, "stop": 35.4},
        ],
        "sells_today": [
            {"ts_code": "600519.SH", "name": "贵州茅台", "price": 1450.0,
             "volume": 10, "pnl": -320.0, "reason": "移动止盈"},
        ],
        "rejected": [
            {"ts_code": "601127.SH", "name": "赛力斯", "price": 85.0,
             "reasons": "铁律1：止损价 89.00 不低于买入价 85.00，拒绝下单"},
        ],
        "pending_stops": [
            {"ts_code": "000333.SZ", "name": "美的集团", "volume": 300,
             "stop_price": 66.5, "reason": "移动止盈"},
        ],
        "halted_codes": {"600519.SH": "2026-09-26"},
        "alerts": [{"level": "warn", "title": "风控拦截: 601127.SH", "detail": "铁律1"}],
        "report": "",
    }


def test_rich_report_modules(tmp_path):
    """rich 报告：六模块 + 持仓明细 + 买卖盈亏 + 完整拒绝原因。"""
    gen = ReportGenerator(tmp_path)
    rich = _rich_summary()
    path = gen.daily_report(
        trade_date=rich["trade_date"], market_state=rich["market_state"],
        total_asset=rich["total_asset"], cash=rich["cash"],
        positions=[{"ts_code": "600584.SH", "volume": 200, "cost": 77.7,
                    "market_value": 15640.0}],
        signals=[], executed=rich["executed"],
        rejected=rich["rejected"],
        stop_orders=[{"ts_code": "600584.SH", "stop_price": 71.5,
                      "volume": 200, "triggered": False}],
        alerts=[], mode="paper", rich=rich,
    )
    content = path.read_text(encoding="utf-8")
    for section in ("一、账户总览", "二、当前持仓", "三、今日操作",
                    "四、盈亏分析", "五、市场与风险提示", "六、铁律自检"):
        assert section in content
    # 持仓明细（名称/现价/浮盈亏/占比/止损线）
    assert "长电科技" in content and "78.20" in content and "71.50" in content
    assert "+100" in content          # 浮动盈亏
    assert "15.50%" in content        # 占比（不带符号）
    # 买入/卖出/拒绝
    assert "招商银行" in content and "35.40" in content
    assert "移动止盈" in content and "-320" in content
    assert "铁律1：止损价 89.00 不低于买入价 85.00，拒绝下单" in content
    # 账户总览（今日盈亏 253、累计 753）
    assert "+253" in content and "+753" in content
    # 待执行止损 + 停手票
    assert "待执行止损" in content and "2026-09-26" in content
    assert "不构成任何投资建议" in content


def test_rich_report_empty_day(tmp_path):
    """空仓无操作日：空态文案 + 累计盈亏仍在。"""
    gen = ReportGenerator(tmp_path)
    rich = {
        "trade_date": date(2026, 8, 28), "market_state": "上涨",
        "signals": 0, "entry_scale": 1.0,
        "total_asset": 100000.0, "cash": 100000.0, "init_capital": 100000.0,
        "prev_total_asset": None,
        "positions": [], "executed": [], "sells_today": [], "rejected": [],
        "pending_stops": [], "halted_codes": {}, "alerts": [],
    }
    path = gen.daily_report(
        trade_date=rich["trade_date"], market_state="上涨",
        total_asset=100000.0, cash=100000.0,
        positions=[], signals=[], executed=[], rejected=[], stop_orders=[],
        alerts=[], mode="paper", rich=rich,
    )
    content = path.read_text(encoding="utf-8")
    assert "空仓" in content
    assert "今日无买入" in content and "今日无卖出" in content and "今日无被拒信号" in content
    assert "累计盈亏" in content


def test_legacy_report_unchanged(tmp_path):
    """rich=None 走旧薄版（回归保护：旧断言仍成立）。"""
    gen = ReportGenerator(tmp_path)
    path = gen.daily_report(
        trade_date=date(2026, 8, 26), market_state="震荡",
        total_asset=153619.0, cash=80000.0,
        positions=[{"ts_code": "600584.SH", "volume": 200, "cost": 77.7,
                    "market_value": 15548.0}],
        signals=[{"ts_code": "600036.SH", "price": 38.5, "reason": "三层过滤通过"}],
        executed=[{"ts_code": "600036.SH", "volume": 600, "price": 38.5, "stop": 35.4}],
        rejected=[{"ts_code": "600519.SH", "reasons": "仓位预算(占比 30% 超过 25%)"}],
        stop_orders=[{"ts_code": "600584.SH", "stop_price": 71.5, "triggered": False}],
        alerts=[{"level": "warn", "title": "t", "detail": "d"}],
        mode="paper",
    )
    content = path.read_text(encoding="utf-8")
    assert "持仓与止损监控" in content      # 旧版节名
    assert "五、铁律自检" in content        # 旧版序号


# ============ 修复2/3：web/server 纯函数 ============

def _load_web_server():
    spec = importlib.util.spec_from_file_location(
        "lihu_web_server", ROOT / "web" / "server.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def web_server():
    return _load_web_server()


def test_merge_positions_rich_fields(web_server):
    """持仓合并：volume/cost 以 paper_state 为准，rich 字段来自 last_scan。"""
    state = {
        "positions": {
            "600584.SH": {"volume": 200, "cost": 77.7},
            "000333.SZ": {"volume": 300, "cost": 68.0},
        },
    }
    registry = {"600584.SH": {"stop_price": 71.5, "triggered": False}}
    scan_summary = {
        "positions": [
            {"ts_code": "600584.SH", "name": "长电科技", "price": 78.2,
             "market_value": 15640.0, "float_pnl": 100.0, "float_pnl_pct": 0.0064,
             "weight": 0.155, "stop_price": 71.5},
            # 000333 无 rich 数据 → 回退空
        ],
    }
    merged = web_server._merge_positions(state, registry, scan_summary)
    by_code = {p["ts_code"]: p for p in merged}
    assert by_code["600584.SH"]["volume"] == 200
    assert by_code["600584.SH"]["name"] == "长电科技"
    assert by_code["600584.SH"]["price"] == 78.2
    assert by_code["600584.SH"]["float_pnl"] == 100.0
    assert by_code["600584.SH"]["stop_price"] == 71.5
    # 无 rich/registry 的票：字段回退（不抛异常）
    assert by_code["000333.SZ"]["name"] == ""
    assert by_code["000333.SZ"]["price"] is None
    assert by_code["000333.SZ"]["stop_price"] is None


def test_merge_positions_no_last_scan(web_server):
    """无 last_scan：回退旧字段（volume/cost/stop_price）。"""
    state = {"positions": {"600584.SH": {"volume": 200, "cost": 77.7}}}
    registry = {"600584.SH": {"stop_price": 71.5}}
    merged = web_server._merge_positions(state, registry, {})
    assert merged[0]["ts_code"] == "600584.SH"
    assert merged[0]["volume"] == 200
    assert merged[0]["price"] is None


def test_live_metrics(web_server):
    """live 指标：累计收益率/今日盈亏/浮动盈亏/今日已实现。"""
    state = {"init_capital": 100000.0}
    scan_summary = {
        "prev_total_asset": 100500.0,
        "positions": [
            {"float_pnl": 100.0}, {"float_pnl": -50.0},
        ],
        "sells_today": [{"pnl": 253.0}, {"pnl": -53.0}],
    }
    live = web_server._live_metrics(state, scan_summary, total_asset=100753.0)
    assert live["cumulative_return"] == pytest.approx(0.00753)
    assert live["day_pnl"] == pytest.approx(253.0)
    assert live["floating_pnl"] == pytest.approx(50.0)
    assert live["realized_today"] == pytest.approx(200.0)
    assert live["init_capital"] == 100000.0


def test_live_metrics_no_prev(web_server):
    """上次巡检缺失（首日/同日重跑）→ day_pnl=None。"""
    live = web_server._live_metrics({"init_capital": 100000.0}, {}, 100753.0)
    assert live["day_pnl"] is None
    assert live["cumulative_return"] == pytest.approx(0.00753)


def test_dashboard_endpoint(web_server, tmp_path, monkeypatch):
    """/api/dashboard 端点：live 块 + 合并持仓（隔离数据目录）。"""
    httpx = pytest.importorskip("httpx")
    from fastapi.testclient import TestClient

    data_dir = tmp_path / "data"
    data_dir.mkdir()
    (data_dir / "paper_state.json").write_text(json.dumps({
        "cash": 60000.0, "init_capital": 100000.0,
        "asset": {"total_asset": 100753.0, "cash": 60000.0, "market_value": 40753.0},
        "positions": {"600584.SH": {"volume": 200, "cost": 77.7, "available": 200}},
        "trades": [{"ts_code": "600584.SH", "side": "buy", "price": 77.7,
                    "volume": 200, "date": "2026-08-27", "reason": "三层过滤通过"}],
        "halt_map": {},
    }), encoding="utf-8")
    (data_dir / "last_scan.json").write_text(json.dumps({
        "trade_date": "2026-08-27", "finished_at": "2026-08-27 16:31:00",
        "summary": {
            "trade_date": "2026-08-27", "total_asset": 100753.0,
            "prev_total_asset": 100500.0,
            "positions": [{"ts_code": "600584.SH", "name": "长电科技",
                           "price": 78.2, "market_value": 15640.0,
                           "float_pnl": 100.0, "float_pnl_pct": 0.0064,
                           "weight": 0.155, "stop_price": 71.5}],
            "sells_today": [{"ts_code": "X.SH", "pnl": 253.0}],
        },
    }), encoding="utf-8")

    monkeypatch.setattr(web_server, "DATA_DIR", data_dir)
    monkeypatch.setattr(web_server, "_market_state",
                        lambda: {"state": "上涨", "latest": "2026-08-27", "ret20d": 3.5})
    monkeypatch.setattr(web_server, "_grid_training_reference", lambda: {})
    monkeypatch.setattr(web_server, "_read_backtest_summary",
                        lambda: {"available": False})

    client = TestClient(web_server.app)
    resp = client.get("/api/dashboard")
    assert resp.status_code == 200
    d = resp.json()
    # live 块（真实指标）
    assert d["live"]["cumulative_return"] == pytest.approx(0.00753)
    assert d["live"]["day_pnl"] == pytest.approx(253.0)
    assert d["live"]["realized_today"] == pytest.approx(253.0)
    # 合并持仓：名称/现价/浮盈亏来自 last_scan
    pos = d["account"]["positions"][0]
    assert pos["ts_code"] == "600584.SH"
    assert pos["name"] == "长电科技"
    assert pos["price"] == 78.2
    assert pos["float_pnl"] == 100.0
    # 交易记录含 reason（修复4 数据就绪）
    assert d["recent_trades"][0]["reason"] == "三层过滤通过"


# ============ 修复1 端到端：_scan_impl → 真实 ReportGenerator rich 报告 ============

def test_scan_impl_writes_rich_report(tmp_path, monkeypatch):
    """_scan_impl 全链路：真实 ReportGenerator 落盘 rich 版 .md
    （含持仓名称/现价/浮盈亏、买入原因、铁律自检）。"""
    import pandas as pd

    from lihu_quantify.config import RiskConfig, StrategyConfig
    from lihu_quantify.monitor import scheduler as sched_mod
    from lihu_quantify.monitor.scheduler import DailyScanner
    from lihu_quantify.types import Signal

    latest = date(2026, 8, 28)
    broker = PaperBroker(init_capital=1_000_000.0, persist=False,
                         state_file=str(tmp_path / "state.json"))
    broker.set_price("600000.SH", 10.0)

    s = DailyScanner.__new__(DailyScanner)
    s.broker = broker
    s.alerter = MagicMock()
    s.reporter = ReportGenerator(tmp_path / "reports")   # 真实报告生成器
    s.mode = "paper"
    s.settings = MagicMock()

    monkeypatch.setattr(sched_mod, "_ROOT", tmp_path)
    monkeypatch.setattr(sched_mod, "_append_filter_stats", lambda rec: None)

    def fake_query(api, params=None, use_cache=True):
        dates = pd.date_range(end="2026-08-28", periods=40, freq="D")
        return pd.DataFrame({
            "ts_code": "600000.SH",
            "trade_date": [d.strftime("%Y%m%d") for d in dates],
            "open": 10.0, "high": 10.5, "low": 9.5, "close": 10.0,
            "vol": 1000.0, "amount": 10000.0,
        })

    s.client = MagicMock()
    s.client.query.side_effect = fake_query
    monkeypatch.setattr(
        sched_mod, "OrderManagementSystem",
        lambda b: OrderManagementSystem(b, registry_file=str(tmp_path / "stop.json")),
    )
    sig = Signal(kind="buy", ts_code="600000.SH", suggested_price=10.0,
                 stop_loss=9.5, suggested_position_pct=0.10,
                 reason="三层过滤通过")
    strategy = MagicMock()
    strategy.latest_signal.return_value = sig
    monkeypatch.setattr(sched_mod, "CherryClaw", lambda **kw: strategy)
    monkeypatch.setattr(sched_mod, "add_all_standard",
                        lambda df: df.assign(ma10=10.0))
    monkeypatch.setattr(
        s, "_universe",
        lambda n: (["600000.SH"], {}, {"600000.SH": "浦发银行"}),
    )
    gate = MagicMock()
    gate.check.return_value = MagicMock(approved=True, rejected_items=lambda: [])
    monkeypatch.setattr(sched_mod, "ChecklistGate", lambda **kw: gate)

    summary = s._scan_impl(
        latest, "上涨", n=50, days=120,
        s=StrategyConfig(market_filter=False), r=RiskConfig(),
        prev_total_asset=None,
    )

    # 报告路径回填
    report_file = tmp_path / "reports" / "2026-08-28.md"
    assert report_file.exists()
    assert summary["report"] == str(report_file)
    content = report_file.read_text(encoding="utf-8")
    # 六模块齐全 + 持仓明细（名称/现价/止损线/浮盈亏）
    for section in ("一、账户总览", "二、当前持仓", "三、今日操作",
                    "四、盈亏分析", "五、市场与风险提示", "六、铁律自检"):
        assert section in content
    assert "浦发银行" in content and "10.00" in content and "9.50" in content
    # 买入原因存于交易记录（看板交易表展示），报告买入表按清单规格无此列
    # summary 与报告共用数据（修复1：无口径分叉）
    assert summary["positions"][0]["name"] == "浦发银行"
    assert summary["positions"][0]["stop_price"] == 9.5
