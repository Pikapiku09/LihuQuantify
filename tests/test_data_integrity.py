"""数据完整性护栏测试（P0-0，2026-11-14）。

覆盖真实缺陷：``index_daily`` 曾混入 ``2026-09-12``（**星期六**）一条孤立 bar，
``close=4072.07``、``pct_chg=+4.73%``，而前后交易日为 3888.11 / 3885.33。

该行会让 ``pct_change(20)`` / ``rolling(20).mean()`` 这类**按行数**滚动的闸门指标失真
（实测把 2026-10-19 的 ret_20d 由 −4.04% 扭曲成 −8.38%），因此必须在喂给
``classify_market_state`` 之前剔除。

本测试只验证护栏本身的行为契约，不依赖真实数据库文件。
"""

from __future__ import annotations

from datetime import date

import pandas as pd
import pytest

from lihu_quantify.data.integrity import (
    audit_index_daily,
    filter_non_trading_days,
    find_non_trading_days,
    format_audit,
    load_trading_days,
    validate_and_filter,
)

# 交易日历：2026-09-10(四) / 09-11(五) / 09-14(一) / 09-15(二)；09-12 周六不开市
TRADING_DAYS = {
    date(2026, 9, 10),
    date(2026, 9, 11),
    date(2026, 9, 14),
    date(2026, 9, 15),
}


def _frame() -> pd.DataFrame:
    """构造含伪交易日的指数序列（真实缺陷的迷你复刻）。"""
    return pd.DataFrame(
        {
            "trade_date": [
                date(2026, 9, 10),
                date(2026, 9, 11),
                date(2026, 9, 12),   # ← 伪交易日（周六）
                date(2026, 9, 14),
                date(2026, 9, 15),
            ],
            "close": [3850.0, 3888.1106, 4072.07, 3885.3328, 3864.2794],
        }
    )


# ===== filter_non_trading_days（纯函数） =====


def test_filter_drops_saturday_bar():
    """核心用例：剔除周六那条伪 bar，其余保留且顺序不变。"""
    out, dropped = filter_non_trading_days(_frame(), TRADING_DAYS)
    assert dropped == [date(2026, 9, 12)]
    assert len(out) == 4
    assert date(2026, 9, 12) not in set(out["trade_date"])
    assert list(out["trade_date"]) == [
        date(2026, 9, 10), date(2026, 9, 11), date(2026, 9, 14), date(2026, 9, 15),
    ]


def test_filter_keeps_frame_when_all_trading_days():
    """无伪交易日时原样返回（不得无谓拷贝/改序）。"""
    df = _frame()
    df = df[df["trade_date"] != date(2026, 9, 12)]
    out, dropped = filter_non_trading_days(df, TRADING_DAYS)
    assert dropped == []
    assert len(out) == 4


def test_filter_noop_when_calendar_empty():
    """交易日历为空 → 原样返回。宁可漏报，绝不误删生产数据。"""
    df = _frame()
    out, dropped = filter_non_trading_days(df, set())
    assert dropped == []
    assert len(out) == 5      # 伪 bar 仍在，但调用方会在无日历时不具备判定能力


def test_filter_handles_empty_and_missing_column():
    out, dropped = filter_non_trading_days(pd.DataFrame(), TRADING_DAYS)
    assert out.empty and dropped == []
    df = pd.DataFrame({"close": [1.0, 2.0]})
    out, dropped = filter_non_trading_days(df, TRADING_DAYS)
    assert dropped == [] and len(out) == 2


def test_filter_keeps_dates_outside_calendar_coverage():
    """**关键回归**：日历只覆盖 2026-09，传入 1990~2026 全历史时，
    覆盖范围外的行必须原样保留——否则 scheduler 传入 8761 行全历史会被
    误删成只剩 485 行（trade_cal 只回填了 2025~2026）。
    """
    dates = [date(1990, 12, 19), date(2008, 9, 12), date(2025, 6, 3)]
    dates += [date(2026, 9, 10), date(2026, 9, 12), date(2026, 9, 14)]
    df = pd.DataFrame({"trade_date": dates, "close": [100.0] * len(dates)})
    out, dropped = filter_non_trading_days(df, TRADING_DAYS)
    assert dropped == [date(2026, 9, 12)]        # 只剔覆盖范围内的伪日
    assert len(out) == len(dates) - 1
    # 覆盖范围外的三条必须都在
    kept = set(out["trade_date"])
    assert date(1990, 12, 19) in kept
    assert date(2008, 9, 12) in kept
    assert date(2025, 6, 3) in kept


def test_filter_accepts_timestamp_dtype():
    """DuckDB 返回 Timestamp 时也要能正确判定（线上真实返回类型）。"""
    df = _frame()
    df["trade_date"] = pd.to_datetime(df["trade_date"])
    out, dropped = filter_non_trading_days(df, TRADING_DAYS)
    assert dropped == [date(2026, 9, 12)]
    assert len(out) == 4


# ===== 依赖 store 的接口（用假 store 隔离数据库） =====


class _FakeStore:
    """最小假 store：只实现 query()，按 SQL 片段返回预置表。

    注意区分两种取法（真实的 integrity.py 两种都在用）：
    - ``SELECT DISTINCT <col> AS d``  → find_non_trading_days，列名为 ``d``
    - ``SELECT DISTINCT trade_date``  → audit_index_daily，列名为 ``trade_date``
    """

    def __init__(self, tables: dict[str, pd.DataFrame]):
        self._tables = tables

    def query(self, sql: str, params=None) -> pd.DataFrame:
        s = " ".join(sql.split()).lower()
        if "from trade_cal" in s and "is_open" in s:
            return self._tables.get("cal_open", pd.DataFrame())
        if "min(cal_date)" in s:
            return self._tables.get(
                "cal_bounds", pd.DataFrame({"mn": [None], "mx": [None]}))
        if "as d from index_daily" in s:
            return self._tables.get("idx_as_d", pd.DataFrame(columns=["d"]))
        if "distinct trade_date from index_daily" in s:
            return self._tables.get(
                "idx_plain", pd.DataFrame(columns=["trade_date"]))
        if "abs(pct_chg) > 7" in s:
            return self._tables.get(
                "extreme", pd.DataFrame(columns=["trade_date", "close", "pct_chg"]))
        return self._tables.get("default", pd.DataFrame())


def _cal_store() -> _FakeStore:
    cal = pd.DataFrame({"cal_date": sorted(TRADING_DAYS)})
    bounds = pd.DataFrame({
        "mn": [min(TRADING_DAYS)],
        "mx": [max(TRADING_DAYS)],
    })
    days = sorted(TRADING_DAYS | {date(2026, 9, 12)})
    return _FakeStore({
        "cal_open": cal,
        "cal_bounds": bounds,
        "idx_as_d": pd.DataFrame({"d": days}),
        "idx_plain": pd.DataFrame({"trade_date": days}),
    })


def test_load_trading_days_reads_open_only():
    days = load_trading_days(_cal_store())
    assert days == TRADING_DAYS


def test_find_non_trading_days_detects_saturday():
    bad = find_non_trading_days(_cal_store(), table="index_daily")
    assert bad == [date(2026, 9, 12)]


def test_validate_and_filter_uses_store_calendar():
    out, dropped = validate_and_filter(_cal_store(), _frame())
    assert dropped == [date(2026, 9, 12)]
    assert len(out) == 4


def test_audit_reports_structure():
    """audit_index_daily 的返回结构必须稳定（看板/报告会消费）。"""
    report = audit_index_daily(_cal_store())
    assert report["trade_cal_days"] == 4
    assert report["non_trading_days"] == ["2026-09-12"]
    assert isinstance(report["extreme_moves"], list)
    text = format_audit(report)
    assert "伪交易日" in text
    assert "2026-09-12" in text


def test_audit_without_calendar_is_graceful():
    """trade_cal 为空时不得抛异常，要给出可读提示。"""
    report = audit_index_daily(_FakeStore({}))
    assert report["trade_cal_days"] == 0
    assert "note" in report
    assert "trade_cal" in format_audit(report)


# ===== 回归锚点：剔除前后 ret_20d 的差异（真实数字） =====


def test_removal_changes_ret20_window_basis():
    """证明剔除会改变 ret_20d 的基准——这是护栏存在的理由。

    真实案例：2026-10-19 的 ret_20d 含伪日为 −8.378%、剔除后为 −4.043%。
    此处用 21 行构造同样的位移效应：伪 bar 位于窗口起点时影响最大。
    """
    # 21 个"交易日" + 1 个伪 bar 插在窗口起点位置
    base = [4000.0] * 20 + [3700.0]          # 最后一天相对首日下跌
    dates = [date(2026, 9, 1 + i) for i in range(20)] + [date(2026, 9, 21)]
    clean = pd.DataFrame({"trade_date": dates, "close": base})

    dirty = pd.concat(
        [pd.DataFrame({"trade_date": [date(2026, 9, 30)], "close": [4072.07]}), clean],
        ignore_index=True,
    ).sort_values("trade_date").reset_index(drop=True)

    r_dirty = (dirty["close"].pct_change(20) * 100).iloc[-1]
    r_clean = (clean["close"].pct_change(20) * 100).iloc[-1]
    # 伪 bar 插入后窗口整体位移，基准价不同 → 两者必然不等
    assert pd.notna(r_dirty) and pd.notna(r_clean)
    assert r_dirty != pytest.approx(r_clean)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-q"]))
