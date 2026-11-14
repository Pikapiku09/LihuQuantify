"""第 4 层测试：回撤观察（只记录不阻断）+ 数据健康体检。

对应 2026-11-14 用户风险偏好决定：**回撤只观察、不阻断交易**
（与"胜率<45% 则阻断"刻意采用相反处理）。
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

import pytest

from lihu_quantify.monitor.data_health import (
    STATE_FAIL,
    STATE_OK,
    STATE_UNKNOWN,
    check_data_health,
)
from lihu_quantify.monitor.drawdown_watch import (
    POLICY,
    STATE_BREACH,
    STATE_OK as DD_OK,
    STATE_WARNING,
    STATE_WATCH,
    drawdown_from_curve,
    watch_drawdown,
)

ROOT = Path(__file__).resolve().parents[1]
DB = ROOT / "data" / "lihu_quant.duckdb"


# ===== 回撤观察 =====


def test_drawdown_from_curve_basic():
    """峰值 100 → 80 是 −20%；再回到 90，当前回撤 −10%。"""
    info = drawdown_from_curve([100, 90, 80, 85, 90])
    assert info["peak"] == 100
    assert info["max"] == pytest.approx(0.20, abs=1e-6)      # 100 → 80
    assert info["current"] == pytest.approx(0.10, abs=1e-6)  # 100 → 90
    assert info["n"] == 5


def test_drawdown_from_curve_empty_and_nan():
    assert drawdown_from_curve([])["current"] is None
    info = drawdown_from_curve([float("nan"), None])
    assert info["current"] is None and info["n"] == 0


@pytest.mark.parametrize(
    "asset,hwm,expected",
    [
        (100000, 100000, DD_OK),              # 0% 回撤
        (94000, 100000, DD_OK),               # 6%  → 24% of 25% → ok（watch 门槛 50%）
        (86000, 100000, STATE_WATCH),         # 14% → 56% of 25% → watch（warning 门槛 80%）
        (80000, 100000, STATE_WARNING),       # 20% → 80% of 25% → warning(边界)
        (70000, 100000, STATE_BREACH),        # 30% → 120% of 25% → breach
    ],
)
def test_drawdown_grading(asset, hwm, expected, tmp_path):
    rep = watch_drawdown(
        [{"name": "main", "asset": asset, "high_water_mark": hwm}],
        output_path=tmp_path / "dd.json",
        now=datetime(2026, 11, 14, 8, 0, 0),
    )
    # 6% / 25% = 24% → ok；这里 allow ok 或 watch，只要不误报 warning 以上
    if asset == 94000:
        assert rep.state in (DD_OK, STATE_WATCH)
    else:
        assert rep.state == expected


def test_drawdown_breach_at_exactly_limit(tmp_path):
    """25% 上限：恰好触及 25% 应判 breach（>= 语义）。"""
    rep = watch_drawdown(
        [{"name": "main", "asset": 75000, "high_water_mark": 100000}],
        limit=0.25, output_path=tmp_path / "dd.json",
    )
    assert rep.state == STATE_BREACH
    assert rep.worst == pytest.approx(0.25, abs=1e-9)


def test_drawdown_never_blocks_trading():
    """**核心契约**：回撤模块不得声明任何阻断/平仓能力。"""
    assert POLICY["blocks_trading"] is False
    assert POLICY["auto_close_position"] is False
    assert "不约束" in POLICY["mdd_limit_scope"] or "仅约束" in POLICY["mdd_limit_scope"]


def test_drawdown_falls_back_to_curve(tmp_path):
    """high_water_mark 缺失时用 equity_curve 推导，不得崩。"""
    rep = watch_drawdown(
        [{"name": "main", "equity_curve": [100, 95, 80, 88]}],
        output_path=tmp_path / "dd.json",
    )
    # worst 取"当前回撤"（用于判断距上限多远）：100 → 88 = 12%
    assert rep.worst == pytest.approx(0.12, abs=1e-6)
    # 历史最大回撤另列：100 → 80 = 20%
    assert rep.ledgers[0]["curve_max_drawdown"] == pytest.approx(0.20, abs=1e-6)
    assert rep.ledgers[0]["source"] == "curve"


def test_drawdown_reports_worst_ledger(tmp_path):
    rep = watch_drawdown(
        [
            {"name": "main", "asset": 97000, "high_water_mark": 100000},
            {"name": "s44", "asset": 70000, "high_water_mark": 100000},
        ],
        output_path=tmp_path / "dd.json",
    )
    assert rep.worst_ledger == "s44"
    assert rep.state == STATE_BREACH


def test_drawdown_brief_line_silent_when_ok(tmp_path):
    rep = watch_drawdown(
        [{"name": "main", "asset": 99000, "high_water_mark": 100000}],
        output_path=tmp_path / "dd.json",
    )
    assert rep.state == DD_OK
    assert rep.brief_line() == ""


def test_drawdown_brief_line_mentions_not_blocking(tmp_path):
    rep = watch_drawdown(
        [{"name": "main", "asset": 70000, "high_water_mark": 100000}],
        output_path=tmp_path / "dd.json",
    )
    line = rep.brief_line()
    assert "不阻断" in line and "机会" in line


def test_drawdown_empty_ledgers(tmp_path):
    rep = watch_drawdown([], output_path=tmp_path / "dd.json")
    assert rep.worst is None and rep.state == STATE_OK


# ===== 数据体检（真实库集成） =====

requires_db = pytest.mark.skipif(not DB.exists(), reason="本地 duckdb 不存在")


@requires_db
def test_data_health_real_db(tmp_path):
    from lihu_quantify.data.duckdb_store import DuckDBStore

    store = DuckDBStore(DB, read_only=True)
    try:
        rep = check_data_health(store, output_path=tmp_path / "dh.json",
                                now=datetime(2026, 11, 14, 8, 0, 0))
    finally:
        store.close()
    names = {c["name"] for c in rep.checks}
    assert {"空表", "行情滞后", "伪交易日", "日历覆盖"} <= names
    assert rep.index_latest is not None
    # 空表应已通过（P0-0 回填完成）
    empty = next(c for c in rep.checks if c["name"] == "空表")
    assert empty["state"] == STATE_OK, empty["detail"]
    # 伪交易日 2026-09-12 仍在库中（只加护栏、未删原始数据）→ 体检必须报 FAIL
    bad = next(c for c in rep.checks if c["name"] == "伪交易日")
    assert bad["state"] == STATE_FAIL
    assert "2026-09-12" in bad["detail"]


@requires_db
def test_data_health_render_is_ascii_safe(tmp_path):
    """render() 输出必须能编码成 GBK（Windows 控制台），否则体检自身会崩。"""
    from lihu_quantify.data.duckdb_store import DuckDBStore

    store = DuckDBStore(DB, read_only=True)
    try:
        rep = check_data_health(store, output_path=tmp_path / "dh.json")
    finally:
        store.close()
    text = rep.render()
    text.encode("gbk")      # 不抛 UnicodeEncodeError 即通过


def test_data_health_graceful_without_calendar():
    """trade_cal 为空时不得抛异常，伪交易日检查降级为 unknown。"""

    class _EmptyStore:
        def query(self, sql, params=None):
            import pandas as pd

            s = " ".join(sql.split()).lower()
            if "count(*)" in s:
                return pd.DataFrame({"n": [0], "mx": [None]})
            return pd.DataFrame()

    rep = check_data_health(_EmptyStore(), output_path=None)
    assert rep.state in (STATE_FAIL, STATE_UNKNOWN)
    names = {c["name"]: c["state"] for c in rep.checks}
    assert names["伪交易日"] == STATE_UNKNOWN
    assert names["空表"] == STATE_FAIL
