"""策略健康快照（导出契约）的单元测试（2026-11-16）。

为什么值得单独测：这份 JSON 是 LihuQuantify 与 dsh-invest 之间**唯一的耦合面**。
它一坏，投研工作台那一屏就白；它字段一改，消费端会静默空白（Lihu 原有状态 JSON
就是裸读、失败返回 {} 的毛病）。所以把契约本身钉死：

1. `schema_version` 必须是 int，消费端靠它判断"认不认识这份数据"
2. `account_type` 必须是 "paper"——Lihu 是模拟盘，与 dsh-invest 的实盘账本严禁混账
3. 分块容错：任一数据源缺失只能让该块为 null + 进 missing，**不许整份快照失败**
4. 数值必须与 config/registry 的真实值一致，不许写死
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

sys.path.insert(0, str(ROOT / "scripts"))
from export_strategy_health import SCHEMA_VERSION, build_snapshot  # noqa: E402


@pytest.fixture(scope="module")
def snap() -> dict:
    return build_snapshot()


def test_schema_version_is_int(snap):
    assert isinstance(snap["schema_version"], int)
    assert snap["schema_version"] == SCHEMA_VERSION


def test_account_type_is_paper(snap):
    """硬约束：Lihu 是模拟盘。若这里被改成 real，实盘与模拟盘就会在工作台里串账。"""
    assert snap["account_type"] == "paper"
    assert snap["source"] == "LihuQuantify"


def test_required_blocks_present(snap):
    # v2 起必须有 equity（权益序列 + 回撤），页面靠它替掉原来那个 null 回撤
    for key in ("strategy", "equity", "risk", "behavior", "research", "findings",
                "missing", "generated_at"):
        assert key in snap, f"契约缺字段：{key}"
    assert snap["schema_version"] >= 2, "加了 equity 块就必须升 schema 版本，不许在旧版本偷偷加字段"


def test_equity_block_shape(snap):
    """权益块要么是完整结构，要么是 None + 进 missing —— 不许半截。"""
    eq = snap["equity"]
    if eq is None:
        assert "equity_history" in snap["missing"]
        return
    for key in ("n_points", "points", "current_drawdown", "max_drawdown",
                "live_days", "reconstructed_days"):
        assert key in eq, f"equity 缺字段：{key}"
    assert isinstance(eq["points"], list) and eq["points"]
    for pt in eq["points"]:
        assert set(pt) == {"d", "v"} and isinstance(pt["v"], (int, float))
    # 重建段与实时段必须能分辨（否则没人知道曲线哪段是补的）
    assert eq["live_days"] + eq["reconstructed_days"] <= eq["n_points"]
    if eq["reconstructed_days"]:
        assert any("重建" in f["text"] for f in snap["findings"]), \
            "存在重建段时 findings 必须说明，否则会被当成实时记账"


def test_risk_uses_real_drawdown(snap):
    """回撤必须来自权益历史，不许从 paper_state.high_water_mark 硬算。"""
    eq, risk = snap["equity"], snap["risk"]
    if eq is None:
        assert risk["current_drawdown"] is None
        assert "drawdown_note" in risk
    else:
        assert risk["current_drawdown"] == eq["current_drawdown"]


def test_risk_block_matches_config(snap):
    cfg = yaml.safe_load((ROOT / "config" / "settings.yaml").read_text(encoding="utf-8"))
    risk = cfg["risk"]
    assert snap["risk"]["max_single_position"] == float(risk["max_single_position"])
    assert snap["risk"]["min_concurrent_positions"] == int(risk["min_concurrent_positions"])
    assert snap["risk"]["status"] in ("ok", "warn", "breach", "unknown")


def test_research_block_matches_registry(snap):
    from research.registry import load_registry, summarize

    stats = summarize(load_registry())
    r = snap["research"]
    assert r is not None, "登记册存在时 research 块不能为 null"
    assert r["total"] == stats["total"]
    assert r["passed"] == stats["passed"]
    assert r["failed"] == stats["failed"]
    assert len(r["decisions"]) == stats["total"]


def test_findings_shape(snap):
    assert isinstance(snap["findings"], list)
    for f in snap["findings"]:
        assert set(f) >= {"level", "text"}
        assert f["level"] in ("info", "warn", "error")


def test_missing_is_list(snap):
    assert isinstance(snap["missing"], list)


def test_blocks_degrade_not_crash(monkeypatch):
    """数据源全坏时必须仍能产出快照，且缺失项进 missing —— 不许抛异常。"""
    import export_strategy_health as mod

    monkeypatch.setattr(mod, "_read_json", lambda p: None)
    monkeypatch.setattr(mod, "_block_research", lambda: (None, ["hypothesis_registry"]))
    s = mod.build_snapshot()
    assert s["schema_version"] == SCHEMA_VERSION
    assert "behavior_report" in s["missing"]
    assert "paper_state" in s["missing"]
    assert s["behavior"] is None
    assert s["findings"] is not None

def test_swing_block_shape(snap):
    """波段池块（v3）：结构完整，且证伪状态必须被显式抬出来。"""
    sw = snap["swing"]
    if sw is None:
        assert "swing_pool" in snap["missing"]
        return
    for key in ("pool", "backtest", "robustness", "strategy_status"):
        assert key in sw, f"swing 缺字段：{key}"
    if sw["pool"]:
        assert "holdings" in sw["pool"] and "parse_ok" in sw["pool"]
    if sw["backtest"]:
        m = sw["backtest"]["metrics"]
        for key in ("total_return", "max_drawdown", "win_rate"):
            assert key in m
        assert sw["backtest"]["disclaimers"], "回测的免责声明必须透传，不许吞掉"
    if sw["robustness"]:
        assert sw["robustness"]["windows"]
        assert any(w["matched_headline"] for w in sw["robustness"]["windows"])


def test_falsified_strategy_raises_error_finding(snap):
    """策略已证伪时，findings 里必须有 error 级条目。

    这条守的是「页面不能把已证伪的策略显示成正常策略」——恰好是这份数据最容易犯的错。
    """
    sw = snap.get("swing") or {}
    if sw.get("strategy_status") != "falsified":
        pytest.skip("当前波段池未标记证伪")
    assert any(f["level"] == "error" and "证伪" in f["text"] for f in snap["findings"]), \
        "证伪状态没有出现在 error 级 findings 里"

def test_backtest_block_shape(snap):
    """策略回测块（v5）：回测与网格必须分开，且幸存者偏差可读。"""
    bt = snap.get("backtest")
    if bt is None:
        assert "backtest_result" in snap["missing"]
        return
    for key in ("main", "grid", "survivorship_bias"):
        assert key in bt, f"backtest 缺字段：{key}"
    if bt["main"]:
        m = bt["main"]
        for key in ("metrics", "pool", "curve", "curve_points_total"):
            assert key in m
        for key in ("total_return", "max_drawdown", "sharpe", "win_rate"):
            assert key in m["metrics"]
        assert bt["survivorship_bias"], "回测口径必须交代幸存者偏差"
