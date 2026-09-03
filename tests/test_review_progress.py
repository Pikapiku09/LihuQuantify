"""100 笔评审进度可视化（看板 + 日报 + 邮件）回归测试。

口径：买入-卖出 FIFO 配对为一轮（复用 backtest.metrics._pair_rounds，
与月度复盘/回测基准三方一致）。禁止自创第二套配对算法。
"""

from __future__ import annotations

import importlib.util
import json
from datetime import date
from pathlib import Path

import pytest

from lihu_quantify.monitor.review_progress import (
    CHECKPOINT_50,
    REVIEW_TARGET,
    review_stats,
)

ROOT = Path(__file__).resolve().parents[1]


# ============================================================
# 1. 空流水不抛错
# ============================================================

def test_empty_trades():
    r = review_stats([])
    assert r["closed_rounds"] == 0
    assert r["win_rate"] is None
    assert r["pl_ratio"] is None
    assert r["stage"] == "accumulating"
    assert r["remaining"] == REVIEW_TARGET
    assert r["realized_pnl"] == 0


# ============================================================
# 2. FIFO 配对与 _pair_rounds 直跑完全一致
# ============================================================

def _full_pairs(n: int) -> list[dict]:
    """n 组一买一卖（价格交错胜/负），date 用 ISO str。"""
    trades = []
    for i in range(n):
        d = date(2026, 8, 1 + i)
        trades.append({"ts_code": f"6000{i:02d}.SH", "side": "buy",
                       "price": 10.0, "volume": 100, "date": d.isoformat(),
                       "commission": 5.0, "stamp_tax": 0.0})
        trades.append({"ts_code": f"6000{i:02d}.SH", "side": "sell",
                       "price": 10.5 if i % 2 == 0 else 9.5, "volume": 100,
                       "date": date(2026, 8, 2 + i).isoformat(),
                       "commission": 5.0, "stamp_tax": 5.0})
    return trades


def test_fifo_pairing_matches_pair_rounds():
    from lihu_quantify.backtest.metrics import _pair_rounds
    from lihu_quantify.types import TradeRecord

    trades = _full_pairs(6)
    r = review_stats(trades)
    # closed_rounds == 买卖对数
    assert r["closed_rounds"] == 6
    # 与 _pair_rounds 直接跑结果一致（realized_pnl == 各轮 pnl 之和）
    recs = [TradeRecord(ts_code=t["ts_code"], trade_date=date.fromisoformat(t["date"]),
                        side=t["side"], price=t["price"], volume=t["volume"],
                        commission=t.get("commission", 0),
                        stamp_tax=t.get("stamp_tax", 0)) for t in trades]
    rounds = _pair_rounds(recs)
    assert r["realized_pnl"] == pytest.approx(sum(rounds))
    assert r["win_rate"] == pytest.approx(3 / 6)
    # 胜负各半：盈亏比 = 平均赢 / |平均亏|
    wins = [x for x in rounds if x > 0]
    losses = [x for x in rounds if x < 0]
    expect_pl = (sum(wins) / len(wins)) / abs(sum(losses) / len(losses))
    assert r["pl_ratio"] == pytest.approx(expect_pl)


# ============================================================
# 3. 未平仓买入不计轮次
# ============================================================

def test_unclosed_buys_not_counted():
    # 只有 buy 无 sell
    r = review_stats([
        {"ts_code": "600584.SH", "side": "buy", "price": 80.0,
         "volume": 100, "date": "2026-08-26"},
    ])
    assert r["closed_rounds"] == 0
    assert r["win_rate"] is None

    # 两笔 buy + 一笔 sell（不同票）→ FIFO 只配对同票，1 轮
    r2 = review_stats([
        {"ts_code": "600584.SH", "side": "buy", "price": 80.0,
         "volume": 100, "date": "2026-08-26"},
        {"ts_code": "000037.SZ", "side": "buy", "price": 9.0,
         "volume": 1000, "date": "2026-08-26"},
        {"ts_code": "600584.SH", "side": "sell", "price": 82.0,
         "volume": 100, "date": "2026-08-27"},
    ])
    assert r2["closed_rounds"] == 1

    # 同票两笔 buy + 一笔 sell 只覆盖第一笔量 → 1 轮（FIFO 语义）
    r3 = review_stats([
        {"ts_code": "600584.SH", "side": "buy", "price": 80.0,
         "volume": 100, "date": "2026-08-26"},
        {"ts_code": "600584.SH", "side": "buy", "price": 81.0,
         "volume": 100, "date": "2026-08-27"},
        {"ts_code": "600584.SH", "side": "sell", "price": 82.0,
         "volume": 100, "date": "2026-08-28"},
    ])
    assert r3["closed_rounds"] == 1


# ============================================================
# 4. date 对象与 ISO str 混用结果一致
# ============================================================

def test_date_str_and_date_mixed():
    str_ver = _full_pairs(3)
    obj_ver = []
    for t in str_ver:
        t2 = dict(t)
        t2["date"] = date.fromisoformat(t["date"])
        obj_ver.append(t2)
    assert review_stats(str_ver) == review_stats(obj_ver)

    # 混用（一半 str 一半 date）
    mixed = [dict(t, date=(date.fromisoformat(t["date"]) if i % 2 else t["date"]))
             for i, t in enumerate(str_ver)]
    assert review_stats(mixed)["closed_rounds"] == 3
    assert review_stats(mixed)["realized_pnl"] == pytest.approx(
        review_stats(str_ver)["realized_pnl"])


# ============================================================
# 5. 阶段边界
# ============================================================

def test_stage_boundaries():
    from lihu_quantify.monitor.review_progress import _stage
    assert _stage(0) == "accumulating"
    assert _stage(49) == "accumulating"
    assert _stage(50) == "ready_checkpoint50"
    assert _stage(99) == "ready_checkpoint50"
    assert _stage(100) == "ready_review"
    assert CHECKPOINT_50 == 50
    assert REVIEW_TARGET == 100


# ============================================================
# 6. /api/dashboard review 端点
# ============================================================

def _load_web_server():
    spec = importlib.util.spec_from_file_location(
        "lihu_web_server_review", ROOT / "web" / "server.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def web_server():
    return _load_web_server()


def test_dashboard_review_endpoint(web_server, tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    data_dir = tmp_path / "data"
    data_dir.mkdir()
    # 2 买 1 卖（同票）→ 配对 1 轮
    (data_dir / "paper_state.json").write_text(json.dumps({
        "cash": 60000.0, "init_capital": 100000.0,
        "asset": {"total_asset": 100753.0, "cash": 60000.0, "market_value": 40753.0},
        "positions": {"600584.SH": {"volume": 100, "cost": 77.7}},
        "trades": [
            {"ts_code": "600584.SH", "side": "buy", "price": 77.7,
             "volume": 100, "date": "2026-08-26"},
            {"ts_code": "600584.SH", "side": "buy", "price": 78.0,
             "volume": 100, "date": "2026-08-27"},
            {"ts_code": "600584.SH", "side": "sell", "price": 80.0,
             "volume": 100, "date": "2026-08-28", "pnl": 150.0},
        ],
        "halt_map": {},
    }), encoding="utf-8")
    monkeypatch.setattr(web_server, "DATA_DIR", data_dir)
    monkeypatch.setattr(web_server, "_market_state",
                        lambda: {"state": "上涨", "latest": "2026-08-28", "ret20d": 3.5})
    monkeypatch.setattr(web_server, "_grid_training_reference", lambda: {})
    monkeypatch.setattr(web_server, "_read_backtest_summary",
                        lambda: {"available": False})

    client = TestClient(web_server.app)
    resp = client.get("/api/dashboard")
    assert resp.status_code == 200
    rv = resp.json()["review"]
    assert rv["closed_rounds"] == 1
    assert rv["win_rate"] == pytest.approx(1.0)
    assert rv["stage"] == "accumulating"


def test_dashboard_review_empty_state(web_server, tmp_path, monkeypatch):
    """paper_state 缺失/空 → closed_rounds=0 不炸。"""
    from fastapi.testclient import TestClient

    data_dir = tmp_path / "data"
    data_dir.mkdir()
    (data_dir / "paper_state.json").write_text("{}", encoding="utf-8")
    monkeypatch.setattr(web_server, "DATA_DIR", data_dir)
    monkeypatch.setattr(web_server, "_market_state",
                        lambda: {"state": "未知", "latest": None, "ret20d": None})
    monkeypatch.setattr(web_server, "_grid_training_reference", lambda: {})
    monkeypatch.setattr(web_server, "_read_backtest_summary",
                        lambda: {"available": False})

    client = TestClient(web_server.app)
    resp = client.get("/api/dashboard")
    assert resp.status_code == 200
    rv = resp.json()["review"]
    assert rv["closed_rounds"] == 0
    assert rv["win_rate"] is None
    assert rv["stage"] == "accumulating"
