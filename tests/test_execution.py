"""执行层测试：OMS 铁律 + 模拟盘 + xtquant 客户端（mock）。

修复B后：PaperBroker/OMS 默认持久化到 data/。
测试统一用 tmp_path 隔离，避免污染真实状态文件。
"""

from __future__ import annotations

import json
from datetime import date, timedelta
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from lihu_quantify.execution.base import OrderResult
from lihu_quantify.execution.oms import OrderManagementSystem, StopOrder
from lihu_quantify.execution.paper_trade import PaperBroker
from lihu_quantify.execution.xtquant_client import MiniQMTClient
from lihu_quantify.types import Signal


_TEST_STATE_DIR = Path(__file__).parent / ".test_state"
_COUNTER = [0]


def _unique_path(prefix: str) -> str:
    """每次生成唯一状态文件路径（避免测试间共享残留）。"""
    _TEST_STATE_DIR.mkdir(exist_ok=True)
    _COUNTER[0] += 1
    import os

    return str(_TEST_STATE_DIR / f"{prefix}_{os.getpid()}_{_COUNTER[0]}.json")


def _paper(tmp_path=None, **kwargs) -> PaperBroker:
    """构建隔离持久化的 PaperBroker（每次唯一文件）。"""
    if "state_file" not in kwargs:
        kwargs["state_file"] = _unique_path("paper")
    return PaperBroker(**kwargs)


def _oms(pb, tmp_path=None, **kwargs) -> OrderManagementSystem:
    """构建隔离持久化的 OMS（每次唯一文件）。"""
    if "registry_file" not in kwargs:
        kwargs["registry_file"] = _unique_path("registry")
    return OrderManagementSystem(pb, **kwargs)


def _buy_signal(ts_code="600519.SH", stop_loss=92.0) -> Signal:
    return Signal(
        kind="buy", ts_code=ts_code, suggested_price=100.0,
        stop_loss=stop_loss, take_profit=[105.0],
        suggested_position_pct=0.10, trade_date=date(2026, 8, 26),
    )


# ============ PaperBroker ============

def test_paper_buy_sell_roundtrip():
    pb = _paper(init_capital=100000)
    pb.connect()
    pb.set_price("600519.SH", 100.0)
    r = pb.buy("600519.SH", 100.0, 500)
    assert r.success
    assert r.filled_price == 100.0
    # 资金 = 100000 - 50000 - 25（佣金 50000*0.00025=12.5 → max(12.5,5)=12.5）
    assert pb.cash == pytest.approx(100000 - 50000 - 12.5)
    # T+1：今日买入不可卖
    assert pb.sell("600519.SH", 100.0, 500).success is False
    # 次日可卖
    pb.on_new_day(date(2026, 8, 27))
    r2 = pb.sell("600519.SH", 110.0, 500)
    assert r2.success
    # 卖出回笼 55000 - 佣金 max(13.75,5)=13.75 - 印花税 27.5
    assert pb.cash == pytest.approx(100000 - 50012.5 + 55000 - 13.75 - 27.5)


def test_paper_insufficient_cash():
    pb = _paper(init_capital=10000)
    pb.set_price("600519.SH", 100.0)
    r = pb.buy("600519.SH", 100.0, 2000)   # 需 20 万
    assert not r.success
    assert "资金不足" in r.msg


def test_paper_query_asset_and_positions():
    pb = _paper(init_capital=100000)
    pb.set_price("600519.SH", 100.0)
    pb.buy("600519.SH", 100.0, 500)
    pb.on_new_day(date(2026, 8, 27))
    positions = pb.query_positions()
    assert len(positions) == 1
    assert positions[0].ts_code == "600519.SH"
    assert positions[0].volume == 500
    assert positions[0].cost == pytest.approx(100.0)
    asset = pb.query_asset()
    assert asset["market_value"] == pytest.approx(50000)
    assert asset["total_asset"] == pytest.approx(pb.cash + 50000)


def test_paper_weighted_cost():
    pb = _paper(init_capital=1000000)
    pb.buy("600519.SH", 100.0, 100)
    pb.buy("600519.SH", 110.0, 100)
    assert pb.positions["600519.SH"].cost == pytest.approx(105.0)


# ============ OMS 铁律 ============

def test_oms_rejects_buy_without_stop():
    """铁律1：无止损价 → 拒绝买入。"""
    pb = _paper(init_capital=100000)
    oms = _oms(pb)
    sig = _buy_signal(stop_loss=None)
    r, stop = oms.place_buy_with_stop(sig, 100, 100.0)
    assert not r.success
    assert "铁律1" in r.msg
    assert stop is None


def test_oms_rejects_stop_above_price():
    """铁律1：止损价 >= 买入价 → 拒绝。"""
    pb = _paper(init_capital=100000)
    oms = _oms(pb)
    sig = _buy_signal(stop_loss=105.0)   # 高于买入价 100
    r, stop = oms.place_buy_with_stop(sig, 100, 100.0)
    assert not r.success
    assert "铁律1" in r.msg


def test_oms_buy_with_stop_registers():
    """买入+止损同时登记。"""
    pb = _paper(init_capital=100000)
    pb.set_price("600519.SH", 100.0)
    oms = _oms(pb)
    sig = _buy_signal(stop_loss=92.0)
    r, stop = oms.place_buy_with_stop(sig, 100, 100.0)
    assert r.success
    assert stop is not None
    assert stop.stop_price == 92.0
    assert stop.volume == 100
    assert "600519.SH" in oms.stop_registry


def test_oms_rejects_averaging_down():
    """铁律2：持仓亏损时拒绝向下补仓。"""
    pb = _paper(init_capital=100000)
    pb.set_price("600519.SH", 100.0)
    oms = _oms(pb)
    # 先买入
    sig = _buy_signal(stop_loss=92.0)
    r1, _ = oms.place_buy_with_stop(sig, 100, 100.0)
    assert r1.success
    pb.on_new_day(date(2026, 8, 27))
    # 价格跌到 90（持仓成本 100，亏损）
    pb.set_price("600519.SH", 90.0)
    sig2 = _buy_signal(stop_loss=83.0)
    r2, _ = oms.place_buy_with_stop(sig2, 100, 90.0)
    assert not r2.success
    assert "铁律2" in r2.msg
    assert "向下补仓" in r2.msg

def test_oms_stop_triggers_on_price_drop():
    """止损监控：价格跌破止损线 → 自动卖出。"""
    pb = _paper(init_capital=100000)
    pb.set_price("600519.SH", 100.0)
    oms = _oms(pb)
    sig = _buy_signal(stop_loss=92.0)
    oms.place_buy_with_stop(sig, 100, 100.0)
    pb.on_new_day(date(2026, 8, 27))
    # 价格跌破 92
    pb.set_price("600519.SH", 91.0)
    results = oms.check_stops_once()
    assert len(results) == 1
    assert results[0].success
    assert oms.stop_registry["600519.SH"].triggered
    # 持仓应清空
    assert pb.query_positions() == []


def test_oms_no_trigger_when_price_above_stop():
    pb = _paper(init_capital=100000)
    pb.set_price("600519.SH", 100.0)
    oms = _oms(pb)
    sig = _buy_signal(stop_loss=92.0)
    oms.place_buy_with_stop(sig, 100, 100.0)
    pb.set_price("600519.SH", 99.0)   # 未破止损
    assert oms.check_stops_once() == []
    assert not oms.stop_registry["600519.SH"].triggered


def test_oms_rebuild_from_positions():
    """崩溃恢复：从持仓重建止损登记（默认成本-8%）。"""
    pb = _paper(init_capital=100000)
    pb.set_price("600519.SH", 100.0)
    pb.buy("600519.SH", 100.0, 200)
    oms = _oms(pb)
    assert oms.stop_registry == {}
    n = oms.rebuild_stops_from_positions()
    assert n == 1
    stop = oms.stop_registry["600519.SH"]
    assert stop.stop_price == pytest.approx(100.0 * 0.92)   # -8%
    assert stop.volume == 200


def test_oms_buy_failed_no_stop_registered():
    """买入失败（资金不足）→ 不登记止损。"""
    pb = _paper(init_capital=1000)   # 资金不足
    oms = _oms(pb)
    sig = _buy_signal(stop_loss=92.0)
    r, stop = oms.place_buy_with_stop(sig, 100, 100)   # 需 1 万
    assert not r.success
    assert stop is None
    assert oms.stop_registry == {}


# ============ 修复B：持久化恢复 ============

def test_paper_state_persistence_roundtrip(tmp_path):
    """修复B验收：买入→序列化→新建 broker 加载→持仓/现金完全一致。"""
    state_file = tmp_path / "paper_state.json"
    # 第一段：买入
    pb1 = _paper(state_file=str(state_file), init_capital=100000)
    pb1.set_price("600519.SH", 100.0)
    pb1.buy("600519.SH", 100.0, 500)
    pb1.on_new_day(date(2026, 8, 26))
    assert state_file.exists(), "状态文件应已写入"

    # 第二段：新实例加载
    pb2 = PaperBroker(state_file=str(state_file))
    assert pb2.cash == pytest.approx(pb1.cash)
    assert "600519.SH" in pb2.positions
    p2 = pb2.positions["600519.SH"]
    assert p2.volume == 500
    assert p2.available == 500   # on_new_day 已解冻
    assert p2.cost == pytest.approx(100.0)
    assert len(pb2.trades) == 1
    assert pb2.trade_day == date(2026, 8, 26)


def test_oms_registry_persistence_roundtrip(tmp_path):
    """修复B验收：止损登记持久化→新 OMS 加载→保留原始止损价。"""
    reg_file = tmp_path / "stop_registry.json"
    pb = _paper(state_file=str(tmp_path / "p.json"))
    pb.set_price("600519.SH", 100.0)
    oms1 = _oms(pb, registry_file=str(reg_file))
    sig = _buy_signal(stop_loss=95.5)   # 非默认 -8%（92）
    oms1.place_buy_with_stop(sig, 100, 100.0)
    assert reg_file.exists()

    # 新 OMS 加载（不 rebuild）
    oms2 = OrderManagementSystem(pb, registry_file=str(reg_file))
    stop = oms2.stop_registry.get("600519.SH")
    assert stop is not None, "止损登记应从文件恢复"
    assert stop.stop_price == 95.5, "原始止损价应保留（非 -8% 默认）"
    assert stop.volume == 100


def test_paper_corrupt_state_file_ignored(tmp_path):
    """损坏的状态文件应被忽略（全新账户），不抛异常。"""
    bad = tmp_path / "bad.json"
    bad.write_text("{invalid json", encoding="utf-8")
    pb = PaperBroker(state_file=str(bad), init_capital=100000)
    assert pb.cash == 100000
    assert pb.positions == {}


# ============ 修复F：连亏 3 笔停手 ============

def test_paper_halt_after_three_consecutive_losses():
    """修复F验收：同票连亏 3 笔 → 停手，is_halted 为真。"""
    pb = _paper(init_capital=1000000)
    code = "600584.SH"
    # 三轮亏损：买 100 卖 95（亏）×3
    for i in range(3):
        pb.set_price(code, 100.0)
        pb.buy(code, 100.0, 100)
        pb.on_new_day(date(2026, 8, 10 + i * 7))
        pb.set_price(code, 95.0)
        pb.sell(code, 95.0, 100)
    assert pb.is_halted(code, today=date(2026, 9, 1)), "连亏 3 笔应停手 30 天"
    # 30 天后解除
    assert not pb.is_halted(code, today=date(2026, 10, 1)), "停手期满应解除"


def test_paper_no_halt_when_win_intervenes():
    """盈利打断连亏 → 不停手。"""
    pb = _paper(init_capital=1000000)
    code = "600584.SH"
    # 亏、亏、盈、亏 → 连亏最多 2
    for i, (bp, sp) in enumerate([(100, 95), (100, 95), (100, 105), (100, 95)]):
        pb.set_price(code, bp)
        pb.buy(code, bp, 100)
        pb.on_new_day(date(2026, 8, 3 + i * 7))
        pb.set_price(code, sp)
        pb.sell(code, sp, 100)
    assert not pb.is_halted(code, today=date(2026, 9, 1)), "盈利打断连亏不应停手"


def test_paper_halt_persisted(tmp_path):
    """修复B+F：停手状态持久化，重启后仍生效。"""
    state_file = tmp_path / "paper_state.json"
    pb1 = _paper(state_file=str(state_file), init_capital=1000000)
    code = "600584.SH"
    for i in range(3):
        pb1.set_price(code, 100.0)
        pb1.buy(code, 100.0, 100)
        pb1.on_new_day(date(2026, 8, 10 + i * 7))
        pb1.set_price(code, 95.0)
        pb1.sell(code, 95.0, 100)
    assert pb1.is_halted(code)
    # 重启
    pb2 = PaperBroker(state_file=str(state_file))
    assert pb2.is_halted(code), "停手状态应跨重启保留"


def test_paper_fee_adjusted_loss_counts():
    """修复F(第三轮)验收：卖出价=买入价×0.9995（价格持平、费用吃亏）→ 判定为亏损。

    三轮这样的边界交易后应触发停手（旧口径会漏判）。
    """
    pb = _paper(init_capital=1_000_000)
    code = "600584.SH"
    sell_price = 100.0 * 0.9995   # 99.95，价差 0.05/股=5 元
    # 100 股：价差收益 5 元；卖出费用 = max(9995*0.00025, 5) + 9995*0.0005 ≈ 7.5
    # 买入佣金 = max(10000*0.00025, 5) = 5 → 真实 pnl ≈ 5 - 7.5 - 5 = -7.5 < 0 → 亏损
    for i in range(3):
        pb.set_price(code, 100.0)
        pb.buy(code, 100.0, 100)
        pb.on_new_day(date(2026, 8, 10 + i * 7))
        pb.set_price(code, sell_price)
        pb.sell(code, sell_price, 100)
    assert pb.is_halted(code, today=date(2026, 9, 1)), \
        "边界微亏（费用吃亏）应计入连亏并触发停手（修复F）"


# ============ MiniQMTClient（mock xtquant） ============

def _mock_xtquant_modules():
    """构造 mock 的 xtquant 包（真实模块层级结构）。"""
    import types

    # xtquant.xtconstant：常量
    xtconstant_mod = types.ModuleType("xtquant.xtconstant")
    xtconstant_mod.STOCK_BUY = 23
    xtconstant_mod.STOCK_SELL = 24
    xtconstant_mod.FIX_PRICE = 11

    # xtquant.xttrader：交易接口
    xttrader_mod = types.ModuleType("xtquant.xttrader")
    trader = MagicMock()
    trader.start.return_value = 0
    trader.connect.return_value = 0
    trader.subscribe.return_value = 0
    trader.order_stock.return_value = 12345   # order_id >= 0 成功
    xttrader_mod.XtQuantTrader = MagicMock(return_value=trader)
    xttrader_mod.StockAccount = MagicMock()

    # xtquant 包（属性指向子模块）
    xtquant_pkg = types.ModuleType("xtquant")
    xtquant_pkg.xttrader = xttrader_mod
    xtquant_pkg.xtconstant = xtconstant_mod
    return xtquant_pkg, xttrader_mod


def test_xtquant_client_import_error_message():
    """无 xtquant 环境时应给出清晰提示而非崩溃。"""
    client = MiniQMTClient(qmt_path="C:/fake_qmt", account_id="123456")
    with patch.dict("sys.modules", {"xtquant": None}):
        with pytest.raises(ImportError) as e:
            client._import_xtquant()
        assert "MiniQMT" in str(e.value)


def test_xtquant_client_buy_sell_with_mock():
    """mock xtquant：连接/买卖流程走通。"""
    client = MiniQMTClient(qmt_path="C:/fake_qmt", account_id="123456")
    xtquant_pkg, xttrader_mod = _mock_xtquant_modules()

    import sys
    fake_modules = {
        "xtquant": xtquant_pkg,
        "xtquant.xttrader": xttrader_mod,
        "xtquant.xtconstant": xtquant_pkg.xtconstant,
    }
    with patch.dict(sys.modules, fake_modules):
        assert client.connect() is True
        r = client.buy("600519.SH", 100.0, 100)
        assert r.success
        assert r.order_id == "12345"
        # 验证调用参数
        trader = xttrader_mod.XtQuantTrader.return_value
        trader.order_stock.assert_called()
        args = trader.order_stock.call_args[0]
        assert args[1] == "600519.SH"
        assert args[3] == 100   # volume

        r2 = client.sell("600519.SH", 101.0, 100)
        assert r2.success
        client.close()


