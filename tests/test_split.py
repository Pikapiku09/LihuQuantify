"""IS/OOS 切分工具回归测试（研究纪律第 2 道闸）。

覆盖：空集、边界日（闭区间）、重叠报错、顺序颠倒、date/datetime/字符串三种日期类型。
"""

from __future__ import annotations

import sys
from datetime import date, datetime
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from lihu_quantify.backtest.split import (  # noqa: E402
    assert_no_overlap,
    compare_oos,
    parse_window,
    split_by_date,
)

IS_WINDOW = ["2024-09-24", "2025-12-31"]
OOS_WINDOW = ["2026-01-01", "2026-11-10"]


# ============================================================
# 1. parse_window
# ============================================================

def test_parse_window_returns_dates():
    assert parse_window(IS_WINDOW) == (date(2024, 9, 24), date(2025, 12, 31))
    # tuple 同样接受（配置里既可能是 list 也可能是 tuple）
    assert parse_window(("2024-01-01", "2024-01-31")) == (
        date(2024, 1, 1), date(2024, 1, 31))
    # 单日窗口合法（start == end）
    assert parse_window(["2024-01-01", "2024-01-01"]) == (
        date(2024, 1, 1), date(2024, 1, 1))


@pytest.mark.parametrize("spec", [
    ["2024-09-24"],                 # 只有一端
    ["2024-09-24", "2025-12-31", "2026-01-01"],   # 多于 2 个
    "2024-09-24",                   # 不是序列
    None,
])
def test_parse_window_rejects_bad_shape(spec):
    with pytest.raises(ValueError):
        parse_window(spec)


@pytest.mark.parametrize("spec", [
    ["2024-1-1", "2025-12-31"],     # 非严格 YYYY-MM-DD
    ["20240101", "2025-12-31"],     # 紧凑写法
    ["2024-09-24 00:00:00", "2025-12-31"],  # 带时间
    [20240101, "2025-12-31"],       # 非字符串
])
def test_parse_window_rejects_bad_dates(spec):
    with pytest.raises(ValueError):
        parse_window(spec)


def test_parse_window_rejects_reversed_order():
    with pytest.raises(ValueError, match="顺序颠倒"):
        parse_window(["2025-12-31", "2024-09-24"])


# ============================================================
# 2. assert_no_overlap
# ============================================================

def test_assert_no_overlap_ok_and_returns_parsed():
    assert assert_no_overlap(IS_WINDOW, OOS_WINDOW) == (
        (date(2024, 9, 24), date(2025, 12, 31)),
        (date(2026, 1, 1), date(2026, 11, 10)),
    )


def test_assert_no_overlap_rejects_boundary_touch():
    """is_end == oos_start 也算重叠（禁止边界日同时用于定参与验证）。"""
    with pytest.raises(ValueError, match="重叠|早于"):
        assert_no_overlap(["2024-01-01", "2024-12-31"],
                          ["2024-12-31", "2025-12-31"])


def test_assert_no_overlap_rejects_real_overlap():
    with pytest.raises(ValueError, match="重叠|早于"):
        assert_no_overlap(["2024-01-01", "2025-06-30"],
                          ["2025-01-01", "2025-12-31"])


def test_assert_no_overlap_rejects_swapped_windows():
    """is/oos 整体颠倒（oos 在 is 之前）→ 顺序错误必须报错。"""
    with pytest.raises(ValueError, match="重叠|早于"):
        assert_no_overlap(OOS_WINDOW, IS_WINDOW)


# ============================================================
# 3. split_by_date
# ============================================================

def test_split_empty_rows():
    assert split_by_date([], "trade_date", IS_WINDOW, OOS_WINDOW) == ([], [])


def test_split_boundary_days_inclusive():
    rows = [
        {"trade_date": "2024-09-23"},   # is 开始前一天 → 丢弃
        {"trade_date": "2024-09-24"},   # IS 首日（含）
        {"trade_date": "2025-12-31"},   # IS 末日（含）
        {"trade_date": "2026-01-01"},   # OOS 首日（含）
        {"trade_date": "2026-11-10"},   # OOS 末日（含）
        {"trade_date": "2026-11-11"},   # OOS 结束之后 → 丢弃
    ]
    is_rows, oos_rows = split_by_date(rows, "trade_date", IS_WINDOW, OOS_WINDOW)
    assert [r["trade_date"] for r in is_rows] == ["2024-09-24", "2025-12-31"]
    assert [r["trade_date"] for r in oos_rows] == ["2026-01-01", "2026-11-10"]


def test_split_date_types_date_datetime_str():
    rows = [
        {"d": date(2025, 6, 30), "tag": "date-is"},
        {"d": datetime(2025, 7, 1, 15, 0), "tag": "datetime-is"},
        {"d": "2026-03-02", "tag": "str-oos"},
        {"d": "2026-03-02T10:30:00", "tag": "iso-datetime-oos"},
    ]
    is_rows, oos_rows = split_by_date(rows, "d", IS_WINDOW, OOS_WINDOW)
    assert [r["tag"] for r in is_rows] == ["date-is", "datetime-is"]
    assert [r["tag"] for r in oos_rows] == ["str-oos", "iso-datetime-oos"]


def test_split_drops_unusable_rows_silently():
    rows = [
        "not-a-dict",
        {"other": "x"},                      # 缺日期字段
        {"trade_date": None},                # None
        {"trade_date": "not-a-date"},        # 非法字符串
        {"trade_date": ""},                  # 空串
        {"trade_date": "2025-06-30", "tag": "ok"},
    ]
    is_rows, oos_rows = split_by_date(rows, "trade_date", IS_WINDOW, OOS_WINDOW)
    assert [r["tag"] for r in is_rows] == ["ok"]
    assert oos_rows == []


def test_split_preserves_input_order():
    rows = [
        {"d": "2025-06-30", "i": 1},
        {"d": "2024-10-01", "i": 2},
        {"d": "2021-01-01", "i": 3},   # 窗口外
        {"d": "2026-02-01", "i": 4},
    ]
    is_rows, oos_rows = split_by_date(rows, "d", IS_WINDOW, OOS_WINDOW)
    assert [r["i"] for r in is_rows] == [1, 2]
    assert [r["i"] for r in oos_rows] == [4]


def test_split_rejects_overlapping_windows():
    with pytest.raises(ValueError):
        split_by_date([], "d", ["2024-01-01", "2025-06-30"],
                      ["2025-01-01", "2025-12-31"])


# ============================================================
# 4. compare_oos
# ============================================================

def test_compare_oos_units_are_percent_fractions():
    """入参是比例：2.41% 收益写成 0.0241；delta 以百分点返回。"""
    result = compare_oos(0.0072, 0.0241, 2.0)
    assert result["is"] == pytest.approx(0.0072)
    assert result["oos"] == pytest.approx(0.0241)
    assert result["delta_pp"] == pytest.approx(1.69, abs=1e-6)
    assert result["passed"] is False, "样本外仅改善 1.69pp < 预注册 2.0pp"


def test_compare_oos_passed_when_threshold_met():
    result = compare_oos(0.01, 0.04, 2.0)
    assert result["delta_pp"] == pytest.approx(3.0)
    assert result["passed"] is True


def test_compare_oos_exact_threshold_is_passed():
    """delta 恰好等于门槛 → 通过（容忍浮点误差）。"""
    result = compare_oos(0.02, 0.04, 2.0)
    assert result["delta_pp"] == pytest.approx(2.0)
    assert result["passed"] is True


def test_compare_oos_negative_delta_fails():
    result = compare_oos(0.05, 0.01, 0.0)
    assert result["delta_pp"] == pytest.approx(-4.0)
    assert result["passed"] is False


@pytest.mark.parametrize("bad", [True, None, "0.1", float("nan")])
def test_compare_oos_rejects_invalid_metrics(bad):
    with pytest.raises(ValueError):
        compare_oos(bad, 0.02, 1.0)
