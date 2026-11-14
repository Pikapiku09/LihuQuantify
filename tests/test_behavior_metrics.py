"""行为审计指标单测（纯函数，无 IO / 无网络）。"""

from __future__ import annotations

from datetime import date, timedelta

import pytest

from lihu_quantify.backtest.metrics import _pair_rounds, pair_round_details
from lihu_quantify.behavior.ingest import (
    clean_cell,
    parse_date,
    to_float,
    to_ts_code,
    to_trade_records,
)
from lihu_quantify.behavior.metrics import (
    loss_add_ratio,
    peak_capture,
    pgr_plr,
    position_sizing,
    post_sell_upside,
    turnover,
)
from lihu_quantify.types import TradeRecord


# ============================================================
# ingest
# ============================================================

@pytest.mark.parametrize("raw,expected", [
    ('= "516510      "', "516510.SH"),   # Excel 公式残留 + 沪市 ETF
    ("600584", "600584.SH"),
    ("000001", "000001.SZ"),
    ("300750", "300750.SZ"),
    ("110059", "110059.SH"),             # 可转债
    ("830799", "830799.BJ"),             # 北交所
])
def test_to_ts_code(raw, expected):
    assert to_ts_code(raw) == expected


def test_clean_cell_and_to_float():
    assert clean_cell('= "000400      "') == "000400"
    assert clean_cell("  10.5  ") == "10.5"
    assert to_float("1,234.5") == 1234.5
    assert to_float("--") == 0.0
    assert to_float("") == 0.0
    assert to_float("abc", default=-1.0) == -1.0


def test_parse_date_formats():
    assert parse_date("2026-11-13") == date(2026, 11, 13)
    assert parse_date("20261113") == date(2026, 11, 13)
    assert parse_date("2026/11/13") == date(2026, 11, 13)
    assert parse_date("") is None


def test_fills_to_trade_records_keeps_fee():
    fills = [
        {"ts_code": "600000.SH", "side": "buy", "trade_date": date(2026, 1, 5),
         "price": 10.0, "volume": 1000, "amount": 10000.0, "fee": 5.0,
         "cash_balance": 100000.0},
    ]
    tr = to_trade_records(fills)
    assert len(tr) == 1 and tr[0].commission == 5.0


# ============================================================
# 配对纪律：两个入口必须共用同一算法
# ============================================================

def test_pair_rounds_and_details_agree():
    """`_pair_rounds` 与 `pair_round_details` 必须产出完全相同的 pnl 序列。

    这是"单一配对算法"纪律的回归哨兵——任何人在别处另写一套配对都会被抓到。
    """
    trades = [
        TradeRecord("A", date(2026, 1, 1), "buy", 10.0, 1000, commission=5.0),
        TradeRecord("A", date(2026, 1, 5), "sell", 11.0, 600, commission=3.0,
                    stamp_tax=3.3),
        TradeRecord("A", date(2026, 1, 8), "sell", 9.5, 400, commission=2.0,
                    stamp_tax=1.9),
        TradeRecord("B", date(2026, 1, 2), "buy", 20.0, 500, commission=5.0),
        TradeRecord("B", date(2026, 1, 6), "sell", 21.0, 500, commission=5.0,
                    stamp_tax=5.25),
    ]
    pnls = _pair_rounds(trades)
    details = pair_round_details(trades)
    assert len(pnls) == len(details) == 3
    for a, d in zip(pnls, details):
        assert a == pytest.approx(d["pnl"])


def test_pair_round_details_metadata():
    trades = [
        TradeRecord("A", date(2026, 1, 1), "buy", 10.0, 1000),
        TradeRecord("A", date(2026, 1, 11), "sell", 12.0, 1000),
    ]
    r = pair_round_details(trades)[0]
    assert r["hold_days"] == 10
    assert r["cost"] == pytest.approx(10000.0)
    assert r["ret"] == pytest.approx(0.2)
    assert r["buy_price"] == 10.0 and r["sell_price"] == 12.0


# ============================================================
# 浮亏加仓
# ============================================================

def _fill(code, side, d, price, volume, cash=100000.0):
    return {"ts_code": code, "side": side, "trade_date": d, "price": price,
            "volume": volume, "amount": price * volume, "fee": 5.0,
            "cash_balance": cash}


def test_loss_add_ratio_detects_add_on_dip():
    d = date(2026, 1, 1)
    fills = [
        _fill("A", "buy", d, 10.0, 1000),                        # 首笔，不算加仓
        _fill("A", "buy", d + timedelta(days=1), 12.0, 1000),    # 涨了，不算
        _fill("A", "buy", d + timedelta(days=2), 9.0, 1000),     # 低于均价 11 → 算
        _fill("B", "buy", d, 20.0, 500),                         # 首笔
        _fill("B", "buy", d + timedelta(days=1), 15.0, 500),     # 低于 20 → 算
    ]
    out = loss_add_ratio(fills)
    assert out["n_buys"] == 5
    assert out["n_adds"] == 2
    assert out["add_amount"] == pytest.approx(9000.0 + 7500.0)
    # 总买入额 = 10000+12000+9000+10000+7500 = 48500；返回值为 round(.,4)
    assert out["ratio"] == pytest.approx(round(16500.0 / 48500.0, 4))


def test_loss_add_ratio_ignores_sells_and_flats():
    d = date(2026, 1, 1)
    fills = [
        _fill("A", "buy", d, 10.0, 1000),
        _fill("A", "sell", d + timedelta(days=1), 11.0, 1000),
        _fill("A", "buy", d + timedelta(days=2), 8.0, 1000),     # 已清仓 → 新首笔
    ]
    out = loss_add_ratio(fills)
    assert out["n_adds"] == 0
    assert out["n_buys"] == 2


# ============================================================
# 处置效应
# ============================================================

def _round(pnl, hold, cost=10000.0):
    return {"pnl": pnl, "hold_days": hold, "cost": cost,
            "buy_price": 10.0, "sell_price": 10.0 * (1 + pnl / cost),
            "ts_code": "A", "buy_date": date(2026, 1, 1),
            "sell_date": date(2026, 1, 1) + timedelta(days=hold)}


def test_pgr_plr_insignificant_when_ci_crosses_zero():
    rounds = [_round(500, 10), _round(300, 12), _round(400, 9),
              _round(-500, 11), _round(-300, 10), _round(-400, 12)]
    out = pgr_plr(rounds, n_boot=400)
    assert out["n_win"] == 3 and out["n_loss"] == 3
    assert out["significant"] is False


def test_pgr_plr_significant_when_clear_separation():
    rounds = [_round(500, 30), _round(300, 28), _round(400, 32),
              _round(-500, 5), _round(-300, 6), _round(-400, 4)]
    out = pgr_plr(rounds, n_boot=400)
    assert out["hold_diff"] > 20
    assert out["significant"] is True
    assert out["diff_ci_low"] > 0


# ============================================================
# 峰值捕获率（涨幅口径）
# ============================================================

def test_peak_capture_is_gain_ratio_not_price_ratio():
    """买 10、最高 20、卖 15 → 吃满 50% 涨幅；价格比口径会给出 0.75（错误）。"""
    d0 = date(2026, 1, 1)
    rounds = [{
        "ts_code": "A", "buy_date": d0, "sell_date": d0 + timedelta(days=10),
        "buy_price": 10.0, "sell_price": 15.0, "pnl": 5000.0, "cost": 10000.0,
        "hold_days": 10, "volume": 1000, "ret": 0.5,
        "buy_reason": "", "sell_reason": "",
    }]
    out = peak_capture(rounds, lambda c, s, e: 20.0)
    assert out["n"] == 1
    assert out["median"] == pytest.approx(0.5)


def test_peak_capture_skips_no_rise_and_missing_quotes():
    d0 = date(2026, 1, 1)
    rounds = [{
        "ts_code": "A", "buy_date": d0, "sell_date": d0 + timedelta(days=5),
        "buy_price": 10.0, "sell_price": 10.5, "pnl": 500.0, "cost": 10000.0,
        "hold_days": 5, "volume": 1000, "ret": 0.05,
        "buy_reason": "", "sell_reason": "",
    }]
    # 最高价未高于买入价 → 无法定义捕获，跳过
    out = peak_capture(rounds, lambda c, s, e: 9.5)
    assert out["n"] == 0 and out["skipped_no_rise"] == 1
    # 行情缺失 → 跳过
    out2 = peak_capture(rounds, lambda c, s, e: None)
    assert out2["n"] == 0


# ============================================================
# 卖出后走势
# ============================================================

def test_post_sell_upside_only_counts_profitable():
    d0 = date(2026, 1, 1)
    rounds = [
        {"ts_code": "A", "sell_date": d0, "sell_price": 10.0, "pnl": 500.0,
         "buy_date": d0, "cost": 1000.0, "hold_days": 0, "volume": 100,
         "ret": 0.05, "buy_reason": "", "sell_reason": "", "buy_price": 9.5},
        {"ts_code": "B", "sell_date": d0, "sell_price": 10.0, "pnl": -500.0,
         "buy_date": d0, "cost": 1000.0, "hold_days": 0, "volume": 100,
         "ret": -0.05, "buy_reason": "", "sell_reason": "", "buy_price": 10.5},
    ]
    out = post_sell_upside(rounds, lambda c, s, e: 11.0, window=20)
    assert out["n"] == 1                       # 亏损单不参与"卖飞"统计
    assert out["continued_up_pct"] == 1.0
    assert out["mean_gain_pct"] == pytest.approx(0.1)


# ============================================================
# 换手与仓位
# ============================================================

def test_turnover_uses_explicit_capital_base():
    d = date(2026, 1, 1)
    fills = [_fill("A", "buy", d, 10.0, 1000),
             _fill("A", "buy", d + timedelta(days=365), 10.0, 1000)]
    out = turnover(fills, capital_base=100000.0)
    assert out["capital_base"] == 100000.0
    assert out["turnover_annual"] == pytest.approx(0.2, abs=1e-6)
    assert out["friction_cost_pct"] == pytest.approx(0.2 * 0.003, abs=1e-6)
    # 未传分母时标注为现金口径
    out2 = turnover(fills)
    assert "现金" in out2["basis"]


def test_position_sizing_loss_to_win_ratio():
    rounds = [_round(500, 5, cost=10000.0), _round(300, 5, cost=10000.0),
              _round(-500, 5, cost=14000.0), _round(-300, 5, cost=14000.0)]
    out = position_sizing(rounds)
    assert out["win_cost_mean"] == pytest.approx(10000.0)
    assert out["loss_cost_mean"] == pytest.approx(14000.0)
    assert out["loss_to_win_ratio"] == pytest.approx(1.4)
