"""策略回测快照（backtest_snapshot）单元测试（2026-11-17，P2）。

这层最容易骗人的地方不是崩溃，是**把两组不可比的数字并排放**：
实盘策略回测是 −4.06%，网格寻优最好是 +559.68%（训练段 + 幸存者偏差）。
所以守三条：
1. 幸存者偏差说明必须透传到一级字段，不能埋在 pool 里
2. 网格各行原样呈现，不做「取最好」之类的美化
3. 曲线降采样保留首末与等距，不能改变形状与端点
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))

from lihu_quantify.monitor.backtest_snapshot import (  # noqa: E402
    build_backtest_block, load_grid,
)

BT = {
    "generated_at": "2026-11-16",
    "config_hash": "abc123",
    "init_capital": 100000.0,
    "market_filter_mode": "block",
    "pool_info": {"type": "head-N", "strategy": "IntradayReversal",
                  "survivorship_bias": "乐观口径"},
    "metrics": {"total_return": -0.0406, "sharpe": -0.803},
    "rules_included": {"t_plus_1": True},
    "equity_curve": [{"date": f"2026-01-{i:02d}", "equity": 100000.0 + i} for i in range(1, 31)],
}


def _write(root: Path, bt: dict | None, grids: dict[str, dict] | None = None) -> Path:
    out = root / "outputs"
    out.mkdir(parents=True, exist_ok=True)
    if bt is not None:
        (out / "backtest_result.json").write_text(json.dumps(bt, ensure_ascii=False), encoding="utf-8")
    for name, g in (grids or {}).items():
        (out / name).write_text(json.dumps(g, ensure_ascii=False), encoding="utf-8")
    return root


def test_main_block_fields(tmp_path):
    b = build_backtest_block(_write(tmp_path, BT))
    m = b["main"]
    assert m["generated_at"] == "2026-11-16"
    assert m["metrics"]["total_return"] == -0.0406
    assert m["pool"]["strategy"] == "IntradayReversal"
    assert m["curve_points_total"] == 30


def test_survivorship_bias_hoisted(tmp_path):
    """幸存者偏差必须在一级字段，页面才能显著展示而不必去 pool 里挖。"""
    b = build_backtest_block(_write(tmp_path, BT))
    assert b["survivorship_bias"] == "乐观口径"


def test_curve_downsampled_keeps_endpoints(tmp_path):
    b = build_backtest_block(_write(tmp_path, BT), curve_limit=10)
    c = b["main"]["curve"]
    assert len(c) == 10
    assert c[0] == {"d": "2026-01-01", "v": 100001.0}
    assert c[-1] == {"d": "2026-01-30", "v": 100030.0}


def test_curve_short_not_padded(tmp_path):
    small = dict(BT, equity_curve=BT["equity_curve"][:5])
    b = build_backtest_block(_write(tmp_path, small), curve_limit=250)
    assert len(b["main"]["curve"]) == 5


def test_curve_bad_points_skipped(tmp_path):
    bad = dict(BT, equity_curve=[{"date": "2026-01-01"},
                                 {"date": "2026-01-02", "equity": None},
                                 {"date": "2026-01-03", "equity": 5}])
    b = build_backtest_block(_write(tmp_path, bad))
    assert len(b["main"]["curve"]) == 1


def test_grid_rows_preserved_verbatim(tmp_path):
    """网格各行不得美化：最好的那行与最差的那行都要在。"""
    grids = {
        "grid_v2_a_summary.json": {"pool_label": "a", "best": {"total_return": 5.5968},
                                   "orig_total_return": 1.3645, "orig_robust": True,
                                   "survivorship_bias": "乐观口径"},
        "grid_v2_b_summary.json": {"pool_label": "b", "best": {"total_return": 0.4628},
                                   "orig_total_return": 0.3316, "orig_robust": False,
                                   "survivorship_bias": "乐观口径"},
    }
    b = build_backtest_block(_write(tmp_path, BT, grids))
    assert [g["pool_label"] for g in b["grid"]] == ["a", "b"]
    assert b["grid"][0]["best"]["total_return"] == 5.5968
    assert b["grid"][1]["orig_robust"] is False


def test_grid_ignores_other_files(tmp_path):
    root = _write(tmp_path, BT)
    (root / "outputs" / "grid_random.json").write_text("{}", encoding="utf-8")
    assert load_grid(root / "outputs") == []


def test_grid_bad_json_skipped(tmp_path):
    root = _write(tmp_path, BT)
    (root / "outputs" / "grid_v2_x_summary.json").write_text("{oops", encoding="utf-8")
    assert load_grid(root / "outputs") == []


def test_missing_backtest_gives_none_main(tmp_path):
    (tmp_path / "outputs").mkdir()
    b = build_backtest_block(tmp_path)
    assert b["main"] is None
    assert b["grid"] == []
    assert b["survivorship_bias"] is None


def test_repo_block_is_consistent():
    """仓库真实数据：回测与网格必须分开呈现，且网格存在时偏差说明不能为空。"""
    b = build_backtest_block(ROOT)
    if b["main"] is None:
        pytest.skip("仓库里没有 backtest_result.json")
    assert b["main"]["metrics"]["total_return"] is not None
    assert b["survivorship_bias"], "幸存者偏差说明缺失 —— 页面就没法如实告知"
    if b["grid"]:
        assert all(g["survivorship_bias"] or g["pool_label"] for g in b["grid"])
