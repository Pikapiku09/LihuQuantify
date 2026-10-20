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


def _prep_series(rows_by_code: dict) -> dict:
    return _series(rows_by_code)


def test_build_weeks_calendar_val_window_ignores_stale_obs():
    """估值分位必须使用“近 3 年日历窗”：更早年份的极低 PE 不得进入分位统计。"""
    start = dt.date(2018, 1, 2)
    days = _trading_days(start, 1500)
    # 2018 年：PE=1（极低）；2019~2022 初：PE 缺失（真实个股常见，正是旧实现的坑）；
    # 2022 年后：PE=100（近期估值）。近 3 年窗口内只有 PE=100，旧实现会把 2018 年 PE=1 算进来。
    rows = []
    for i, d in enumerate(days):
        pe = 1.0 if i < 240 else (None if i < 950 else 100.0)
        rows.append({"d": d.strftime("%Y%m%d"), "close": 10.0 + i * 0.01, "adj": 1.0,
                     "pe": pe, "pb": pe})   # PB 与 PE 同步变化，避免 min(pe_p, pb_p) 被常数列压成 0
    weeks_cal = swing.build_weeks(rows, 1095)
    weeks_legacy = swing.build_weeks(rows, 0)
    assert weeks_cal and weeks_legacy
    wc = [w for w in weeks_cal if w["date"] >= "20230101"]
    wl = [w for w in weeks_legacy if w["date"] >= "20230101"]
    assert wc and wl, f"数据不足：最后周 {weeks_cal[-1]['date']}"
    # 日历窗：窗口内 PE 全为 100（当前值也为 100）→ 分位≈0，不被 2018 年极低 PE 污染
    assert wc[-1]["val_pct"] == pytest.approx(0.0, abs=0.02), f"日历窗分位={wc[-1]['val_pct']}"
    # 旧实现（最近 756 个非空观测）会跨越缺失区回溯到 2018 年 PE=1 → 分位明显偏高
    assert wl[-1]["val_pct"] is not None and wl[-1]["val_pct"] > 0.2, f"旧实现分位={wl[-1]['val_pct']}"


def test_prep_reuse_same_result():
    """预处理复用（多变体/蒙特卡洛共享周线）结果必须与逐步构建完全一致。"""
    prices = [10.0] * 180 + [10.0 + 0.05 * i for i in range(1, 120)]
    rows = _rows(prices)
    gold = _gold(rows)
    s1 = _prep_series({"000001.SZ": rows})
    direct = swing.simulate(gold, s1, swing.SLOPE_MIN, trail=0.0, time_stop=60)
    s2 = _prep_series({"000001.SZ": rows})
    prep = swing.build_prep(s2)
    reused = swing.simulate(gold, s2, swing.SLOPE_MIN, trail=0.0, time_stop=60, prep=prep)
    assert direct["metrics"] == reused["metrics"]
    assert [t["ret"] for t in direct["trades"]] == [t["ret"] for t in reused["trades"]]


def test_month_cap_limits_entries():
    """month_cap=1：每月最多一次新开仓（对齐实盘口径）。"""
    prices = [10.0] * 180 + [10.0 + 0.05 * i for i in range(1, 400)]
    rows = _rows(prices)
    codes = ("000001.SZ", "000002.SZ", "000003.SZ")
    series = _series({c: rows for c in codes})
    months_all = sorted({r["d"][:6] for r in rows})
    gold = {m: {c: {"name": "测试股", "brokers": 1} for c in codes} for m in months_all}
    capped = swing.simulate(gold, series, swing.SLOPE_MIN, trail=0.0, time_stop=999, month_cap=1)
    entries = [t["entry_date"][:6] for t in capped["trades"]]
    assert len(entries) >= 2, f"样本不足: {capped['trades']}"
    assert len(entries) == len(set(entries)), f"同月重复开仓: {entries}"
    free = swing.simulate(gold, _series({c: rows for c in codes}), swing.SLOPE_MIN,
                          trail=0.0, time_stop=999)
    assert len(free["trades"]) >= len(capped["trades"])


def test_force_exit_after_long_suspension():
    """长期停牌/退市保护：超过 max_miss 个交易日无数据应按最后价格强制清算，而不是按成本价挂到期末。"""
    prices = [10.0] * 180 + [10.0 + 0.02 * i for i in range(1, 120)]
    code = "000001.SZ"
    rows_main = _rows(prices)
    base = swing.simulate(_gold(rows_main, code), _series({code: rows_main}), swing.SLOPE_MIN,
                          trail=0.0, time_stop=999)
    assert base["trades"], "应产生交易"
    entry = base["trades"][0]["entry_date"]
    dates = sorted({r["d"] for r in rows_main})
    idx = dates.index(entry)
    # 入场后第 3 个交易日起连续停牌 45 个交易日
    gap = set(dates[idx + 3:idx + 48])
    rows_sus = [r for r in rows_main if r["d"] not in gap]
    rows_cal = _rows([10.0] * len(prices))   # 提供完整主日历（模拟其它股票的交易日）
    res = swing.simulate(_gold(rows_main, code), _series({code: rows_sus, "000002.SZ": rows_cal}),
                         swing.SLOPE_MIN, trail=0.0, time_stop=999, max_miss=40)
    forced = [t for t in res["trades"] if t["code"] == code and t["reason"].startswith("停牌/退市超")]
    assert forced, f"未触发强制清算: {[t['reason'] for t in res['trades'] if t['code'] == code]}"
    t = forced[0]
    expected = t["exit_close"] / t["entry_close"] * (1 - swing.COST_SIDE) - 1
    assert t["ret"] == pytest.approx(expected, abs=2e-3), f"收益={t['ret']} 期望={expected:.3f}"
    assert t["exit_date"] >= dates[idx + 3] and t["exit_date"] <= dates[idx + 48]
