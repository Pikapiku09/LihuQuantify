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
    for key in ("strategy", "risk", "behavior", "research", "findings", "missing",
                "generated_at"):
        assert key in snap, f"契约缺字段：{key}"


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
