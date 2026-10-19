"""波段池回测引擎回归测试（2026-10-19 审计修复）。

覆盖：
1. 期末清算不再重复乘复权比例（单笔收益不被平方）
2. 硬止损/趋势止损收益不被平方
3. 停牌日 pending 指令顺延（不静默丢弃）
4. 集中度指标输出
"""
from __future__ import annotations

import datetime as dt
import importlib.util
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location("swing_bt", ROOT / "scripts" / "run_swing_backtest.py")
swing = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(swing)

# 回测窗口从 2023-10-02 起，合成数据必须落在窗口内
START = dt.date(2024, 1, 2)


def _trading_days(start: dt.date, n: int) -> list[dt.date]:
    out, d = [], start
    while len(out) < n:
        if d.weekday() < 5:
            out.append(d)
        d += dt.timedelta(days=1)
    return out


def _rows(prices, start: dt.date = START) -> list[dict]:
    days = _trading_days(start, len(prices))
    return [{"d": d.strftime("%Y%m%d"), "close": round(float(p), 4), "adj": 1.0,
             "pe": 10.0, "pb": 1.0} for d, p in zip(days, prices)]


def _gold(rows, code: str = "000001.SZ") -> dict:
    months = sorted({r["d"][:6] for r in rows})
    return {m: {code: {"name": "测试股", "brokers": 1}} for m in months}


def _series(rows_by_code: dict) -> dict:
    return {c: {"name": "测试股", "rows": rows} for c, rows in rows_by_code.items()}


def _run(prices, code: str = "000001.SZ", **kwargs):
    rows = _rows(prices)
    return swing.simulate(_gold(rows, code), _series({code: rows}), swing.SLOPE_MIN, **kwargs)


def _expected_ret(t: dict) -> float:
    """由成交价反推单笔净收益（双边成本 0.1%）。"""
    return (t["exit_close"] / t["entry_close"]) * (1 - swing.COST_SIDE) ** 2 - 1


def test_no_double_count_on_end_liquidation():
    """期末清算：单笔收益必须等于成交价之比（而非其平方）。"""
    prices = [10.0] * 180 + [10.0 + 10.0 * i / 130 for i in range(1, 131)]
    res = _run(prices, trail=0.0, time_stop=999)
    assert res["trades"], "应产生至少一笔交易"
    t = res["trades"][0]
    assert t["reason"] == "期末清仓"
    expected = _expected_ret(t)
    assert t["ret"] == pytest.approx(expected, abs=2e-3), (
        f"收益={t['ret']} 期望={expected:.3f}（平方后会≈{(1 + expected) ** 2 - 1:.2f}）")
    assert expected > 0.5, "该场景应录得明显正收益"
    assert t["exit_close"] == pytest.approx(round(prices[-1], 2), rel=1e-3)


def test_hard_stop_not_squared():
    """硬止损/趋势止损：单笔亏损应≈成交价之比，而非平方后的更深亏损。"""
    prices = [10.0] * 180 + [10.0 + 0.2 * i for i in range(1, 11)]
    prices += [10.2, 9.7, 9.3, 8.9, 8.4, 8.3, 8.2]   # 一周内急跌触发止损/破位
    res = _run(prices, trail=0.0, time_stop=999)
    assert res["trades"], "应产生至少一笔交易"
    t = res["trades"][0]
    assert t["reason"] in ("硬止损-15%", "破30周线"), t["reason"]
    expected = _expected_ret(t)
    assert t["ret"] == pytest.approx(expected, abs=2e-3), (
        f"收益={t['ret']} 期望={expected:.3f}（平方后会≈{(1 + expected) ** 2 - 1:.2f}）")
    assert -0.24 < t["ret"] < -0.08


def test_pending_carried_through_suspension():
    """出场指令遇到停牌日应顺延到下一个可成交日，而不是丢弃。"""
    prices = [10.0] * 180 + [10.0 + 0.02 * i for i in range(1, 61)]
    rows_main = _rows(prices)
    code = "000001.SZ"
    base = swing.simulate(_gold(rows_main, code), _series({code: rows_main}), swing.SLOPE_MIN,
                          trail=0.0, time_stop=5)
    assert base["trades"], "应产生交易"
    entry = base["trades"][0]["entry_date"]
    dates = sorted({r["d"] for r in rows_main})
    idx = dates.index(entry)
    # 时间止损在 days>=5（entry+4）生成，次日（entry+5）应执行 → 令该日停牌
    suspend_date = dates[idx + 5]
    rows_sus = [r for r in rows_main if r["d"] != suspend_date]
    rows_cal = _rows([10.0] * len(prices))
    res = swing.simulate(_gold(rows_main, code), _series({code: rows_sus, "000002.SZ": rows_cal}),
                         swing.SLOPE_MIN, trail=0.0, time_stop=5)
    trades = [t for t in res["trades"] if t["code"] == code]
    assert trades, "应产生交易"
    expected = dates[idx + 6]
    assert trades[0]["exit_date"] == expected, (
        f"停牌顺延失败：exit={trades[0]['exit_date']} expected={expected}")


def test_concentration_metrics_present():
    prices = [10.0] * 180 + [10.0 + 10.0 * i / 130 for i in range(1, 131)]
    res = _run(prices, trail=0.0, time_stop=999)
    c = res.get("concentration")
    assert c is not None and "ex_top1" in c and "top1" in c
    assert c["ex_top1"] is None   # 仅一笔交易，剔除 TOP1 后无剩余
