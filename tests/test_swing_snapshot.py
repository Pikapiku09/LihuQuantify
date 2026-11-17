"""波段池快照解析（swing_snapshot）单元测试（2026-11-17，P3）。

这层解析的输出会直接出现在工作台页面上，所以守三件事：
1. **按表头名字取列**，不按下标——那张表历史上加过列，按下标会错位且静默出错
2. 解析失败必须降级（raw_excerpt + parse_ok=False），不许返回空表假装没事
3. 「策略已证伪」要能被独立判定，不能只依赖某一句文案
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))

from lihu_quantify.monitor.swing_snapshot import (  # noqa: E402
    build_swing_block, load_backtest, load_pool, load_robustness, parse_pool_markdown,
)

SAMPLE = """# 波段池持仓

> ⚠️ 2026-10-20 扩样本验证：策略证伪，继续暂停实盘、纸面跟踪。
> **当前状态：S1 纸面前向验证**

## 一、池内状态（v3.0 口径重算 · **2026-11-13 收盘**）

| 代码 | 名称 | 所属池 | 现价 | 1 手成本 | 建仓日期/成本 | 目标价 | 止损价 | 状态 | 下次复核 |
|---|---|---|---|---|---|---|---|---|---|
| 000768.SZ | 中航西飞 | —（已退出） | 23.36 | 2,336 元 | 未建仓 | — | 已触发 | **已平仓** | 已归档 |
| 601899.SH | 紫金矿业 | **R 反转池**（挂观察） | 26.38 | 2,638 元 | 未建仓 | 30.42 | 24.79 | 挂观察 | 2026-11-20 |

## 二、别的小节
| 代码 | 不该被读到 |
|---|---|
| 999999.SZ | x |
"""


def test_parse_takes_columns_by_name_not_index():
    d = parse_pool_markdown(SAMPLE)
    assert d["parse_ok"] is True
    hs = d["holdings"]
    assert [h["code"] for h in hs] == ["000768.SZ", "601899.SH"]
    # 若按下标取，目标价会错位成「2,336 元」
    assert hs[1]["target"] == "30.42"
    assert hs[1]["stop"] == "24.79"
    assert hs[1]["next_check"] == "2026-11-20"


def test_columns_reordered_still_correct():
    """把列顺序打乱，按名字取必须仍然正确。"""
    text = """
## 池内状态
| 名称 | 代码 | 止损价 | 状态 |
|---|---|---|---|
| 紫金矿业 | 601899.SH | 24.79 | 挂观察 |
"""
    d = parse_pool_markdown(text)
    assert d["holdings"][0] == {
        "name": "紫金矿业", "code": "601899.SH", "stop": "24.79", "state": "挂观察",
    }


def test_statements_captured():
    d = parse_pool_markdown(SAMPLE)
    blob = " ".join(d["statements"])
    assert "证伪" in blob
    assert any("当前状态" in s for s in d["statements"])


def test_only_pool_section_table_parsed():
    """第二节里的表格不得混进来。"""
    d = parse_pool_markdown(SAMPLE)
    assert all(h["code"] != "999999.SZ" for h in d["holdings"])


def test_no_table_degrades_not_crash():
    d = parse_pool_markdown("# 空文件\n\n没有表\n")
    assert d["parse_ok"] is False
    assert d["holdings"] == []
    assert d["raw_excerpt"]


def test_markdown_emphasis_stripped():
    d = parse_pool_markdown(SAMPLE)
    assert d["holdings"][0]["state"] == "已平仓"          # ** 被去掉
    assert "**" not in (d["holdings"][1]["pool"] or "")


def test_load_backtest_missing(tmp_path):
    assert load_backtest(tmp_path / "nope.json") is None


def test_load_backtest_keeps_disclaimers(tmp_path):
    p = tmp_path / "bt.json"
    p.write_text(json.dumps({"generated_at": "x", "metrics": {"total_return": -0.5},
                             "disclaimers": ["训练段参考"]}), encoding="utf-8")
    d = load_backtest(p)
    assert d["metrics"]["total_return"] == -0.5
    assert d["disclaimers"] == ["训练段参考"]


def test_robustness_returns_all_windows(tmp_path):
    """两个窗口都必须出现，并标出哪个与头条回测同窗口。"""
    (tmp_path / "swing_backtest.json").write_text(json.dumps(
        {"params": {"window": ["2021-01-04", "2026-10-16"]}}), encoding="utf-8")
    for name, win, base in (("swing_montecarlo_2021.json", ["2021-01-04", "2026-10-16"], -0.52),
                            ("swing_montecarlo.json", ["2023-10-02", "2026-10-16"], -0.488)):
        (tmp_path / name).write_text(json.dumps({
            "window": win,
            "baseline": {"metrics": {"total_return": base}},
            "controls": {"C1": {"mean": 0.02, "baseline_percentile": 0.02,
                                "q": {"p5": -0.1, "p95": 0.1}}},
        }), encoding="utf-8")
    bt = load_backtest(tmp_path / "swing_backtest.json")
    rb = load_robustness(tmp_path, bt)
    assert len(rb["windows"]) == 2
    assert rb["windows"][0]["matched_headline"] is True, "同窗口的必须排在最前"
    assert rb["windows"][0]["window"] == ["2021-01-04", "2026-10-16"]


def test_robustness_dedupes_same_window(tmp_path):
    same = {"window": ["2021-01-04", "2026-10-16"], "baseline": {"metrics": {}}, "controls": {}}
    for name in ("swing_montecarlo_2021.json", "swing_montecarlo.json"):
        (tmp_path / name).write_text(json.dumps(same), encoding="utf-8")
    rb = load_robustness(tmp_path, None)
    assert len(rb["windows"]) == 1


def test_falsified_detected_from_backtest_alone(tmp_path, monkeypatch):
    """只给回测（无 md）也必须判定为证伪 —— 两条独立证据任一路径成立。

    注意：load_pool 默认经 swing_pool._pick_pool_file 解析（取 mtime 最新，可能是仓库/NAS 件），
    所以这里必须把 load_pool 打桩成 None，否则会读到仓库真实 md，测不到「只有回测」的分支。
    """
    import lihu_quantify.monitor.swing_snapshot as snap
    monkeypatch.setattr(snap, "load_pool", lambda path=None: None)
    out = tmp_path / "outputs"
    out.mkdir()
    (out / "swing_backtest.json").write_text(json.dumps(
        {"metrics": {"total_return": -0.52}, "params": {"window": ["a", "b"]}}), encoding="utf-8")
    b = build_swing_block(tmp_path)
    assert b["pool"] is None
    assert b["strategy_status"] == "falsified"


def test_empty_root_does_not_crash(tmp_path, monkeypatch):
    import lihu_quantify.monitor.swing_snapshot as snap
    monkeypatch.setattr(snap, "load_pool", lambda path=None: None)
    b = build_swing_block(tmp_path)
    assert b["pool"] is None and b["backtest"] is None and b["robustness"] is None
    assert b["strategy_status"] == "unknown"


def test_repo_block_is_consistent():
    """仓库真实数据：解析必须成功、状态必须已证伪（这条会挡住「页面把证伪策略显示成正常」）。"""
    b = build_swing_block(ROOT)
    if b["pool"] is None:
        pytest.skip("仓库里没有波段池 md")
    assert b["pool"]["parse_ok"] is True
    assert b["strategy_status"] == "falsified"
    assert b["backtest"]["metrics"]["total_return"] < 0
