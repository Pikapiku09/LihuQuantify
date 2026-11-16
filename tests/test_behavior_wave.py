"""波口径配对单测。"""

from __future__ import annotations

from datetime import date

import pytest

from lihu_quantify.behavior.wave import (
    Wave,
    build_waves,
    by_code,
    wave_summary,
)


def _f(code, side, d, price, vol, fee=0.0, name="测试"):
    return {"ts_code": code, "name": name, "side": side, "trade_date": d,
            "price": price, "volume": vol, "amount": price * vol, "fee": fee,
            "cash_balance": 100000.0}


D0 = date(2026, 1, 5)


# ============================================================
# 波的切分
# ============================================================

def test_simple_roundtrip_makes_one_wave():
    fills = [_f("A", "buy", D0, 10.0, 1000),
             _f("A", "sell", date(2026, 2, 5), 12.0, 1000)]
    waves = build_waves(fills)
    assert len(waves) == 1
    w = waves[0]
    assert w.pnl == pytest.approx(2000.0)
    assert w.hold_days == 31
    assert w.open is False


def test_scaling_in_and_out_stays_one_wave():
    """分批建仓 + 分批减仓，只要仓位没归零，就是同一波。"""
    fills = [
        _f("A", "buy", D0, 10.0, 1000),
        _f("A", "buy", date(2026, 1, 10), 9.0, 1000),      # 加仓（浮亏）
        _f("A", "sell", date(2026, 1, 20), 11.0, 500),     # 部分减仓
        _f("A", "buy", date(2026, 1, 25), 10.5, 500),      # 再加
        _f("A", "sell", date(2026, 2, 10), 12.0, 2000),    # 清仓
    ]
    waves = build_waves(fills)
    assert len(waves) == 1
    w = waves[0]
    assert w.n_buys == 3 and w.n_sells == 2
    assert w.max_volume == 2000
    assert w.buy_amount == pytest.approx(10_000 + 9_000 + 5_250)
    assert w.sell_amount == pytest.approx(5_500 + 24_000)


def test_reopen_after_full_exit_makes_new_wave():
    fills = [
        _f("A", "buy", D0, 10.0, 1000),
        _f("A", "sell", date(2026, 1, 15), 11.0, 1000),     # 清仓 → 第一波结束
        _f("A", "buy", date(2026, 2, 1), 12.0, 500),       # 重新建仓 → 第二波
        _f("A", "sell", date(2026, 2, 10), 13.0, 500),
    ]
    waves = build_waves(fills)
    assert len(waves) == 2
    assert waves[0].hold_days == 10          # 01-05 → 01-15
    assert waves[1].hold_days == 9           # 02-01 → 02-10


def test_multi_stock_waves_independent():
    fills = [
        _f("A", "buy", D0, 10.0, 1000),
        _f("B", "buy", D0, 20.0, 500),
        _f("A", "sell", date(2026, 1, 20), 11.0, 1000),
        _f("B", "sell", date(2026, 2, 20), 18.0, 500),
    ]
    waves = build_waves(fills)
    assert len(waves) == 2
    by = {w.ts_code: w for w in waves}
    assert by["A"].hold_days == 15           # 01-05 → 01-20
    assert by["B"].hold_days == 46           # 01-05 → 02-20


def test_open_wave_flagged_not_counted_in_closed_summary():
    fills = [_f("A", "buy", D0, 10.0, 1000)]   # 只买不卖
    waves = build_waves(fills)
    assert len(waves) == 1 and waves[0].open is True
    assert waves[0].hold_days is None
    # 未平仓波不进入已清仓摘要（否则 -10000 会被当成亏损）
    s = wave_summary(waves)
    assert s["n_waves"] == 0 and s["n_open"] == 1


def test_open_wave_included_when_requested():
    fills = [_f("A", "buy", D0, 10.0, 1000)]
    s = wave_summary(build_waves(fills), closed_only=False)
    assert s["n_waves"] == 1 and s["n_open"] == 1


def test_fee_deducted_from_pnl():
    fills = [_f("A", "buy", D0, 10.0, 1000, fee=5.0),
             _f("A", "sell", date(2026, 1, 20), 11.0, 1000, fee=8.0)]
    w = build_waves(fills)[0]
    assert w.pnl == pytest.approx(1000.0 - 13.0)


# ============================================================
# 边界与容错
# ============================================================

def test_sell_without_position_ignored():
    fills = [_f("A", "sell", D0, 10.0, 1000)]
    assert build_waves(fills) == []


def test_zero_volume_skipped():
    fills = [_f("A", "buy", D0, 10.0, 0), _f("A", "sell", D0, 11.0, 1000)]
    assert build_waves(fills) == []


def test_empty_fills():
    assert build_waves([]) == []
    assert wave_summary([])["n_waves"] == 0


def test_oversell_clamps_to_zero():
    """卖出量超过持仓（数据异常）→ 仓位归零收波，不出现负仓位。"""
    fills = [_f("A", "buy", D0, 10.0, 500),
             _f("A", "sell", date(2026, 1, 10), 11.0, 1000)]
    waves = build_waves(fills)
    assert len(waves) == 1 and waves[0].open is False


# ============================================================
# 汇总
# ============================================================

def test_wave_summary_stats():
    fills = [
        _f("A", "buy", D0, 10.0, 1000),
        _f("A", "sell", date(2026, 1, 25), 12.0, 1000),      # +2000，20 天
        _f("B", "buy", D0, 10.0, 1000),
        _f("B", "sell", date(2026, 1, 15), 9.0, 1000),       # -1000，10 天
    ]
    s = wave_summary(build_waves(fills))
    assert s["n_waves"] == 2
    assert s["total_pnl"] == pytest.approx(1000.0)
    assert s["win_rate"] == pytest.approx(0.5)
    assert s["hold_max"] == 20
    assert s["win_pnl_mean"] == pytest.approx(2000.0)
    assert s["loss_pnl_mean"] == pytest.approx(-1000.0)


def test_by_code_aggregates_and_sorts():
    fills = [
        _f("A", "buy", D0, 10.0, 1000),
        _f("A", "sell", date(2026, 1, 25), 12.0, 1000),      # A: +2000
        _f("B", "buy", D0, 10.0, 1000),
        _f("B", "sell", date(2026, 1, 15), 7.0, 1000),       # B: -3000
    ]
    out = by_code(build_waves(fills))
    assert out[0]["ts_code"] == "B"          # 按绝对盈亏降序
    assert out[0]["pnl"] == pytest.approx(-3000.0)
    assert out[1]["ts_code"] == "A"
    assert out[1]["ret"] == pytest.approx(0.2)


def test_fifo_vs_wave_different_hold_days():
    """核心动机回归：分批进出时，波口径的持有期显著长于 FIFO 单轮。

    构造：一次建仓、中途多次小额减仓、隔很久才清仓。
    FIFO 会把持仓切碎，波口径保持完整。
    """
    from lihu_quantify.backtest.metrics import pair_round_details
    from lihu_quantify.behavior.ingest import to_trade_records

    fills = [
        _f("A", "buy", D0, 10.0, 1000),
        _f("A", "sell", date(2026, 3, 1), 11.0, 100),      # 中途小额减仓
        _f("A", "sell", date(2026, 5, 1), 12.0, 100),
        _f("A", "sell", date(2026, 7, 1), 13.0, 800),      # 才清仓
    ]
    waves = build_waves(fills)
    rounds = pair_round_details(to_trade_records(fills))
    assert waves[0].hold_days == (date(2026, 7, 1) - D0).days
    # FIFO 的每一轮都从建仓日算，但被拆成 3 段；第一段最早结束
    assert len(rounds) == 3
    assert max(r["hold_days"] for r in rounds) == waves[0].hold_days
    assert min(r["hold_days"] for r in rounds) < waves[0].hold_days
