"""模拟盘权益历史（equity_history）单元测试（2026-11-17，P0）。

守的是「真值不得被重建值覆盖」这条铁律 —— 这条数据会成为工作台权益曲线与回撤告警的唯一来源，
一旦 live 被 reconstructed 覆盖，账面历史就会悄悄变成一份重建值，而没人知道。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))

from lihu_quantify.monitor.equity_history import (  # noqa: E402
    SOURCE_LIVE, SOURCE_RECONSTRUCTED, append_point, load_points, summarize,
)


def test_append_then_load(tmp_path):
    p = tmp_path / "e.jsonl"
    assert append_point(p, "2026-11-17", 100000.0, source=SOURCE_LIVE) is True
    pts = load_points(p)
    assert len(pts) == 1
    assert pts[0]["date"] == "2026-11-17"
    assert pts[0]["total_asset"] == 100000.0
    assert pts[0]["source"] == SOURCE_LIVE


def test_live_not_overwritten_by_reconstructed(tmp_path):
    """核心护栏：真实记账值不得被重建值覆盖。"""
    p = tmp_path / "e.jsonl"
    append_point(p, "2026-11-17", 123456.0, source=SOURCE_LIVE)
    assert append_point(p, "2026-11-17", 999.0, source=SOURCE_RECONSTRUCTED) is False
    assert load_points(p)[0]["total_asset"] == 123456.0


def test_live_overwrites_reconstructed(tmp_path):
    """反过来必须能覆盖：重建值被真值替换是正确方向。"""
    p = tmp_path / "e.jsonl"
    append_point(p, "2026-11-17", 999.0, source=SOURCE_RECONSTRUCTED)
    assert append_point(p, "2026-11-17", 123456.0, source=SOURCE_LIVE) is True
    pts = load_points(p)
    assert len(pts) == 1 and pts[0]["source"] == SOURCE_LIVE


def test_date_formats_normalized(tmp_path):
    p = tmp_path / "e.jsonl"
    append_point(p, "20261117", 1.0)
    append_point(p, "2026-11-18", 2.0)
    assert [x["date"] for x in load_points(p)] == ["2026-11-17", "2026-11-18"]


def test_bad_input_ignored(tmp_path):
    p = tmp_path / "e.jsonl"
    assert append_point(p, None, 100.0) is False
    assert append_point(p, "2026-11-17", "abc") is False
    assert load_points(p) == []


def test_corrupt_lines_skipped(tmp_path):
    p = tmp_path / "e.jsonl"
    p.write_text('{"date":"2026-11-17","total_asset":100}\n不是 JSON\n{"date":"x"}\n',
                 encoding="utf-8")
    pts = load_points(p)
    assert len(pts) == 1 and pts[0]["total_asset"] == 100.0


def test_summarize_drawdown():
    pts = [
        {"date": "2026-01-01", "total_asset": 100.0, "source": SOURCE_LIVE},
        {"date": "2026-01-02", "total_asset": 120.0, "source": SOURCE_LIVE},
        {"date": "2026-01-03", "total_asset": 90.0, "source": SOURCE_LIVE},
        {"date": "2026-01-04", "total_asset": 108.0, "source": SOURCE_LIVE},
    ]
    s = summarize(pts)
    assert s["peak_equity"] == 120.0
    assert s["max_drawdown"] == pytest.approx(-0.25)
    assert s["max_drawdown_peak_date"] == "2026-01-02"
    assert s["max_drawdown_trough_date"] == "2026-01-03"
    assert s["current_drawdown"] == pytest.approx(-0.10)
    assert s["n_points"] == 4 and s["live_days"] == 4


def test_summarize_empty_is_null_not_zero():
    """没有数据必须返回 None，不能返回 0 —— 0 会显示成「没有回撤」。"""
    s = summarize([])
    assert s["n_points"] == 0
    assert s["current_drawdown"] is None
    assert s["max_drawdown"] is None


def test_repo_history_is_consistent():
    """仓库里的真实历史文件：日期唯一、升序、数值为正、来源标记合法。"""
    pts = load_points(ROOT / "data" / "equity_history.jsonl")
    if not pts:                        # 新 clone 可能还没回填，跳过而不是失败
        pytest.skip("data/equity_history.jsonl 尚未回填")
    dates = [p["date"] for p in pts]
    assert dates == sorted(dates)
    assert len(dates) == len(set(dates)), "同日不得有两个点"
    assert all(p["total_asset"] > 0 for p in pts)
    assert {p["source"] for p in pts} <= {SOURCE_LIVE, SOURCE_RECONSTRUCTED}
