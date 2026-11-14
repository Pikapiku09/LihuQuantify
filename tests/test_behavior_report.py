"""行为体检报告组装/缓存/日报渲染 单测。"""

from __future__ import annotations

import json
import os
import time
from datetime import date

import pytest

from lihu_quantify.behavior.report import (
    build_report,
    evaluate_thresholds,
    format_markdown,
    load_cached,
    write_json,
)
from lihu_quantify.monitor.daily_report import _render_behavior


# ============================================================
# 造数
# ============================================================

def _fills():
    d = date(2026, 1, 5)
    return [
        {"ts_code": "600000.SH", "name": "A", "side": "buy", "trade_date": d,
         "price": 10.0, "volume": 1000, "amount": 10000.0, "fee": 5.0,
         "cash_balance": 200000.0},
        {"ts_code": "600000.SH", "name": "A", "side": "buy", "trade_date": d,
         "price": 9.0, "volume": 1000, "amount": 9000.0, "fee": 5.0,
         "cash_balance": 190000.0},
        {"ts_code": "600000.SH", "name": "A", "side": "sell", "trade_date": d,
         "price": 9.5, "volume": 2000, "amount": 19000.0, "fee": 15.0,
         "cash_balance": 209000.0},
    ]


def _rounds():
    return [
        {"ts_code": "600000.SH", "buy_date": date(2026, 1, 5),
         "sell_date": date(2026, 1, 15), "buy_price": 10.0, "sell_price": 12.0,
         "volume": 1000, "cost": 10000.0, "pnl": 2000.0, "ret": 0.2,
         "hold_days": 10, "buy_reason": "", "sell_reason": ""},
        {"ts_code": "600000.SH", "buy_date": date(2026, 1, 5),
         "sell_date": date(2026, 1, 8), "buy_price": 10.0, "sell_price": 9.0,
         "volume": 1000, "cost": 10000.0, "pnl": -1000.0, "ret": -0.1,
         "hold_days": 3, "buy_reason": "", "sell_reason": ""},
    ]


def _lookup_max(v):
    return lambda code, s, e: v


# ============================================================
# build_report / thresholds
# ============================================================

def test_build_report_shape():
    rep = build_report(_fills(), _rounds(), _lookup_max(15.0), capital_base=200000.0)
    for key in ("fills", "rounds", "quote_coverage", "pgr_plr", "loss_add_ratio",
                "peak_capture", "post_sell_upside", "turnover", "position_sizing"):
        assert key in rep
    assert rep["rounds"]["n_rounds"] == 2
    assert rep["rounds"]["total_pnl"] == pytest.approx(1000.0)
    assert rep["turnover"]["capital_base"] == 200000.0
    assert "显式传入" in rep["turnover"]["basis"]


def test_evaluate_thresholds_flags_over_exposure():
    rep = build_report(_fills(), _rounds(), _lookup_max(15.0), capital_base=200000.0)
    items = {i["key"]: i for i in evaluate_thresholds(rep)}
    # 2 笔买入里 1 笔是浮亏加仓（9.0 < 10.0），占比 9000/19000 ≈ 47% > 10%
    assert items["loss_add_ratio.ratio"]["hit"] is True
    # 亏损单规模 10000 / 盈利单规模 10000 = 1.0 → 未超 1.10
    assert items["position_sizing.loss_to_win_ratio"]["hit"] is False


def test_format_markdown_has_all_sections():
    rep = build_report(_fills(), _rounds(), _lookup_max(15.0), capital_base=200000.0)
    md = format_markdown(rep)
    for title in ("行为体检报告", "告警判定", "浮亏加仓", "峰值捕获率",
                  "卖出后走势", "换手与摩擦成本", "仓位规模"):
        assert title in md
    # 不得出现会炸 GBK 控制台的数学减号
    assert "\u2212" not in md


# ============================================================
# 缓存读写
# ============================================================

def test_write_then_load_roundtrip(tmp_path):
    rep = build_report(_fills(), _rounds(), _lookup_max(15.0))
    p = write_json(rep, tmp_path / "sub" / "behavior.json")
    assert p.exists()
    got = load_cached(p)
    assert got is not None
    assert got["rounds"]["n_rounds"] == 2
    assert got["_stale"] is False
    assert got["_generated_at"]


def test_load_cached_missing_and_bad_json(tmp_path):
    assert load_cached(tmp_path / "nope.json") is None
    bad = tmp_path / "bad.json"
    bad.write_text("{not json", encoding="utf-8")
    assert load_cached(bad) is None
    arr = tmp_path / "arr.json"
    arr.write_text("[1,2,3]", encoding="utf-8")
    assert load_cached(arr) is None   # 结构不是 dict


def test_load_cached_marks_stale(tmp_path):
    p = tmp_path / "old.json"
    p.write_text(json.dumps({"rounds": {"n_rounds": 1}}), encoding="utf-8")
    old = time.time() - 30 * 86400
    os.utime(p, (old, old))
    got = load_cached(p, stale_days=7)
    assert got is not None and got["_stale"] is True


# ============================================================
# 日报渲染
# ============================================================

def test_render_behavior_absent_returns_empty():
    assert _render_behavior({}) == ""
    assert _render_behavior({"behavior_audit": None}) == ""


def test_render_behavior_renders_metrics():
    rep = build_report(_fills(), _rounds(), _lookup_max(15.0), capital_base=200000.0)
    rep["_cached_at"] = "2026-11-14 10:00"
    rep["_stale"] = False
    html = _render_behavior({"behavior_audit": rep})
    assert "行为体检" in html
    assert "浮亏加仓" in html
    assert "缓存时间" in html
    assert "告警" in html          # 浮亏加仓 47% 触发阈值
    assert "已过期" not in html


def test_render_behavior_flags_stale_cache():
    rep = build_report(_fills(), _rounds(), _lookup_max(15.0), capital_base=200000.0)
    rep["_cached_at"] = "2026-01-01 10:00"
    rep["_stale"] = True
    html = _render_behavior({"behavior_audit": rep})
    assert "已过期" in html


# ============================================================
# scheduler 侧：缓存读取开关
# ============================================================

def test_load_behavior_audit_disabled_by_default():
    """线上 settings.yaml 默认关闭 → 返回 None，日报整节省略（零行为变化）。"""
    from lihu_quantify.monitor.scheduler import _load_behavior_audit
    assert _load_behavior_audit() is None


def test_load_behavior_audit_reads_cache_when_enabled(tmp_path, monkeypatch):
    from lihu_quantify.monitor import scheduler as sched_mod

    cache = write_json(
        build_report(_fills(), _rounds(), _lookup_max(15.0)),
        tmp_path / "behavior.json",
    )
    monkeypatch.setattr(
        sched_mod, "_read_settings_yaml",
        lambda path=None: {"behavior_audit": {
            "enabled": True, "cache_json": str(cache), "stale_days": 7}},
    )
    got = sched_mod._load_behavior_audit()
    assert got is not None
    assert got["rounds"]["n_rounds"] == 2
    assert got["_stale"] is False


def test_load_behavior_audit_missing_cache_returns_none(tmp_path, monkeypatch):
    from lihu_quantify.monitor import scheduler as sched_mod

    monkeypatch.setattr(
        sched_mod, "_read_settings_yaml",
        lambda path=None: {"behavior_audit": {
            "enabled": True, "cache_json": str(tmp_path / "absent.json")}},
    )
    assert sched_mod._load_behavior_audit() is None
