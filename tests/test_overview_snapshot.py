"""模拟盘总览（overview_snapshot）单元测试（2026-11-17，P1）。

这层最容易出的错不是崩溃，而是**混源**：把实盘的数字当模拟盘显示。
所以除常规字段外，重点守三条：
1. 账户数字必须与 paper_state 同源（不掺实盘交割单重建的任何数）
2. 持仓优先用 last_scan 的富字段，缺失才退回 paper_state（不得凭空造名称/现价）
3. report 路径只给文件名，不下发容器内绝对路径
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))

from lihu_quantify.monitor.overview_snapshot import build_overview_block  # noqa: E402

SCAN = {
    "trade_date": "2026-11-10",
    "finished_at": "2026-11-10 20:49:27",
    "summary": {
        "trade_date": "2026-11-10",
        "market_state": "下跌",
        "entry_scale": 0.0,
        "signals": 5,
        "executed": [],
        "rejected": [],
        "sells_today": [],
        "pending_stops": [],
        "halted_codes": {},
        "stale_dropped": 78,
        "positions": [{
            "ts_code": "000429.SZ", "name": "粤高速A", "volume": 500, "cost": 13.26,
            "price": 13.74, "market_value": 6870.0, "stop_price": 12.696,
        }],
        "total_asset": 88747.61,
        "cash": 81877.61,
        "init_capital": 100000.0,
        "prev_total_asset": 88600.0,
        "review": {"closed_rounds": 98, "target": 100},
        "market_gate": {"state": "下跌", "mode": "block", "gate_open": False},
        "brief": "今日巡检完成",
        "ai_summary": None,
        "report": "/app/outputs/reports/2026-11-10.md",
        "alerts": ["某告警"],
    },
}
STATE = {
    "cash": 81877.61,
    "init_capital": 100000.0,
    "asset": {"cash": 81877.61, "total_asset": 88747.61, "market_value": 6870.0},
    "positions": {"000429.SZ": {"volume": 500, "cost": 13.26}},
}


def _write(root: Path, scan: dict | None, state: dict | None) -> Path:
    d = root / "data"
    d.mkdir(parents=True, exist_ok=True)
    if scan is not None:
        (d / "last_scan.json").write_text(json.dumps(scan, ensure_ascii=False), encoding="utf-8")
    if state is not None:
        (d / "paper_state.json").write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
    return root


def test_full_block(tmp_path):
    b = build_overview_block(_write(tmp_path, SCAN, STATE), [])
    assert b["trade_date"] == "2026-11-10"
    assert b["market_state"] == "下跌"
    assert b["counts"]["signals"] == 5 and b["counts"]["stale_dropped"] == 78
    assert b["account"]["total_asset"] == 88747.61
    # 模块把 total_return 四舍五入到 4 位小数（-0.1125），别按原始精度断言
    assert b["account"]["total_return"] == pytest.approx(-0.1125, abs=1e-4)
    assert b["positions"][0]["name"] == "粤高速A"
    assert b["positions"][0]["pnl_ratio"] == pytest.approx(0.036199, abs=1e-5)


def test_today_pnl_prefers_prev_total_asset(tmp_path):
    b = build_overview_block(_write(tmp_path, SCAN, STATE), [])
    tp = b["account"]["today_pnl"]
    assert tp["basis"] == "prev_total_asset"
    assert tp["amount"] == pytest.approx(147.61, abs=0.01)


def test_today_pnl_falls_back_to_equity(tmp_path):
    scan = json.loads(json.dumps(SCAN))
    scan["summary"]["prev_total_asset"] = None
    eq = [{"date": "2026-11-07", "total_asset": 88000.0}, {"date": "2026-11-10", "total_asset": 88747.61}]
    b = build_overview_block(_write(tmp_path, scan, STATE), eq)
    tp = b["account"]["today_pnl"]
    assert tp["basis"] == "equity_history"
    assert tp["amount"] == pytest.approx(747.61, abs=0.01)


def test_today_pnl_none_without_basis(tmp_path):
    scan = json.loads(json.dumps(SCAN))
    scan["summary"]["prev_total_asset"] = None
    b = build_overview_block(_write(tmp_path, scan, STATE), [])
    assert b["account"]["today_pnl"] is None


def test_positions_fall_back_to_state(tmp_path):
    scan = json.loads(json.dumps(SCAN))
    scan["summary"]["positions"] = []
    b = build_overview_block(_write(tmp_path, scan, STATE), [])
    p = b["positions"][0]
    assert p["code"] == "000429.SZ"
    # 兜底时没有名称/现价，必须是 None 而不是编造
    assert p["name"] is None and p["price"] is None and p["pnl_ratio"] is None


def test_report_path_is_basename_only(tmp_path):
    b = build_overview_block(_write(tmp_path, SCAN, STATE), [])
    assert b["report_name"] == "2026-11-10.md"
    assert "/app/" not in json.dumps(b, ensure_ascii=False)


def test_no_sources_returns_none(tmp_path):
    (tmp_path / "data").mkdir()
    assert build_overview_block(tmp_path, []) is None


def test_state_only_still_works(tmp_path):
    """只有 paper_state（没跑过巡检）时也要能出块，只是没有巡检计数。"""
    b = build_overview_block(_write(tmp_path, None, STATE), [])
    assert b["account"]["total_asset"] == 88747.61
    assert b["counts"]["signals"] is None
    assert b["trade_date"] is None


def test_repo_block_matches_paper_state():
    """仓库真实数据：总资产必须与 paper_state 同源（混入实盘重建数就会在这里炸）。"""
    from lihu_quantify.monitor.equity_history import EQUITY_FILE, load_points

    b = build_overview_block(ROOT, load_points(ROOT / EQUITY_FILE))
    if b is None:
        pytest.skip("仓库里没有 last_scan / paper_state")
    state = json.loads((ROOT / "data" / "paper_state.json").read_text(encoding="utf-8"))
    assert b["account"]["total_asset"] == pytest.approx(state["asset"]["total_asset"], abs=0.01)
    assert b["account"]["init_capital"] == pytest.approx(state["init_capital"], abs=0.01)
