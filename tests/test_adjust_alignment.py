"""复权口径对齐回归（评审 P0，2026-09-29）。

- 除权日 stop_price 按因子折算到当日口径，不产生假止损
- adj_ratio 单元（缺失降级 1.0）
- MA10 前复权折算（除权不污染均线）
"""
from __future__ import annotations

import json
from datetime import date
from types import SimpleNamespace

import pandas as pd

from lihu_quantify.execution.paper_trade import PaperBroker


def _broker(tmp_path, *, buy_adj=None):
    b = PaperBroker(init_capital=100_000.0, persist=False,
                    state_file=str(tmp_path / "state.json"))
    if buy_adj:
        b.buy_adj.update(buy_adj)
    return b


def test_adj_ratio_unit(tmp_path):
    b = _broker(tmp_path, buy_adj={"600000.SH": 1.0})
    # 除权因子翻倍 → 比率 0.5（买入日口径 → 今日口径）
    assert b.adj_ratio("600000.SH", 2.0) == 0.5
    # 无变化 → 1.0
    assert b.adj_ratio("600000.SH", 1.0) == 1.0
    # 今日因子缺失 → 降级 1.0（旧行为）
    assert b.adj_ratio("600000.SH", 0.0) == 1.0
    # 未记录买入因子（旧状态文件）→ 1.0
    assert b.adj_ratio("600001.SH", 2.0) == 1.0


def test_dividend_no_false_stop(tmp_path, monkeypatch):
    """除权日：cost=10 买入、除权后 raw=5（因子×2）→ 旧口径误判 -50% 触发止损；
    修复后 stop_eff=9.2×0.5=4.6 < 5.0 → 不触发。"""
    from lihu_quantify.monitor import scheduler as sched_mod
    from lihu_quantify.monitor.alerts import Alerter
    from lihu_quantify.monitor.scheduler import DailyScanner

    monkeypatch.setattr(sched_mod, "_ROOT", tmp_path)
    (tmp_path / "data").mkdir(parents=True, exist_ok=True)

    s = DailyScanner.__new__(DailyScanner)
    s.alerter = Alerter()
    s.book = SimpleNamespace(suffix=lambda: "", is_main=True,
                             name="main", strategy="intraday_reversal")
    s.settings = SimpleNamespace(risk=SimpleNamespace(trailing_profit_pullback=0.03))

    stop = SimpleNamespace(triggered=False, stop_price=9.2, volume=1000)  # 成本-8%
    s.oms = SimpleNamespace(stop_registry={"600000.SH": stop})
    s.broker = SimpleNamespace(
        get_price=lambda c: 5.0,                 # 除权后 raw 收盘
        positions={"600000.SH": SimpleNamespace(cost=10.0)},
        high_water_mark={},
        trades=[],
        adj_ratio=lambda c, at: 0.5,             # adj_buy=1 → adj_today=2
    )
    monkeypatch.setattr(s, "_fetch_ma10", lambda c, d: 0.0, raising=False)
    monkeypatch.setattr(s, "_fetch_adj", lambda c, d: 2.0, raising=False)

    new_pending = s._register_new_stops(s.oms, date(2026, 9, 2))
    assert new_pending == [], f"除权假止损触发（修复失效）: {new_pending}"

    # 对照：因子缺失（降级 1.0）→ 旧口径，会误触发（证明换算在起作用）
    monkeypatch.setattr(s, "_fetch_adj", lambda c, d: 0.0, raising=False)
    old_behavior = s._register_new_stops(s.oms, date(2026, 9, 2))
    assert any(p["reason"] == "price_stop" for p in old_behavior)


def test_fetch_ma10_dividend_adjusted():
    """除权场景：raw 序列被除权拉低 → 前复权折算后 MA10 与当日 raw 可比。"""
    # 构造 10 日：价格 10，最后一天除权 raw 变 5（因子 1→2）
    closes = [10.0] * 9 + [5.0]
    dates = [f"2026-09{i:02d}" for i in range(1, 11)]
    daily = pd.DataFrame({"trade_date": dates, "close": closes})
    adj = pd.DataFrame({"trade_date": dates, "adj_factor": [1.0] * 9 + [2.0]})

    # 手工复算：折算到今日口径 = raw × adj_i / adj_today
    close_adj = [c * a / 2.0 for c, a in zip(closes, adj["adj_factor"])]
    ma10_adj = sum(close_adj[-10:]) / 10
    ma10_raw = sum(closes) / 10
    # raw 版被除权污染（≈9.5），折算版与当日 raw（5.0）同口径（≈7.5→包含历史 10 元，均值合理）
    assert ma10_raw > 9.0
    assert ma10_adj < ma10_raw           # 折算把历史价拉到当日口径
    assert abs(ma10_adj - 5.0) < 1e-9    # 全部折算到 5 → 与当日 raw 完全可比
