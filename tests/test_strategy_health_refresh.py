"""策略健康快照「巡检自动刷新」的单元测试（2026-11-16，1.5 步）。

守四条护栏（这条链路挂在每日巡检里，坏了会拖垮整个巡检）：
1. 未启用 → 不导出（保留「纯手工」旧行为）
2. 快照新鲜 → 跳过，不重复跑子进程
3. 快照过期 / 不存在 → 跑一次导出
4. 脚本缺失 / 子进程异常 / 非零退出 → 返回 None 且**不抛异常**
"""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))

from lihu_quantify.monitor import scheduler as sch  # noqa: E402


def _age(path: Path, hours: float) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{}", encoding="utf-8")
    ts = time.time() - hours * 3600
    os.utime(path, (ts, ts))
    return path


@pytest.fixture()
def wired(tmp_path, monkeypatch):
    """把 settings 指向临时目录，避免动真实 outputs。"""
    out = tmp_path / "strategy_health.json"
    cfg = {"enabled": True, "out_json": str(out), "refresh_hours": 20}
    monkeypatch.setattr(sch, "_read_settings_yaml", lambda *a, **k: {"strategy_health": cfg})
    calls: list[list[str]] = []

    class _Proc:
        returncode = 0
        stderr = ""

    def _run(cmd, **kw):
        calls.append(cmd)
        return _Proc()

    monkeypatch.setattr(sch.subprocess, "run", _run)
    return out, calls


def test_disabled_does_nothing(tmp_path, monkeypatch):
    monkeypatch.setattr(sch, "_read_settings_yaml",
                        lambda *a, **k: {"strategy_health": {"enabled": False}})
    called = []
    monkeypatch.setattr(sch.subprocess, "run", lambda *a, **k: called.append(a))
    assert sch._write_strategy_health() is None
    assert called == []


def test_fresh_snapshot_skipped(wired):
    out, calls = wired
    _age(out, 1)
    assert sch._write_strategy_health() == str(out)
    assert calls == [], "新鲜的快照不得重复导���"


def test_stale_snapshot_refreshed(wired):
    out, calls = wired
    _age(out, 30)
    assert sch._write_strategy_health() == str(out)
    assert len(calls) == 1
    cmd = calls[0]
    assert any("export_strategy_health.py" in str(x) for x in cmd)
    assert "--out" in cmd and str(out) in cmd


def test_missing_snapshot_treated_as_stale(wired):
    out, calls = wired          # 文件不存在
    assert sch._write_strategy_health() == str(out)
    assert len(calls) == 1


def test_subprocess_failure_swallowed(tmp_path, monkeypatch):
    out = tmp_path / "s.json"
    _age(out, 30)
    monkeypatch.setattr(sch, "_read_settings_yaml", lambda *a, **k: {
        "strategy_health": {"enabled": True, "out_json": str(out), "refresh_hours": 20}})

    class _Proc:
        returncode = 2
        stderr = "boom"

    monkeypatch.setattr(sch.subprocess, "run", lambda *a, **k: _Proc())
    assert sch._write_strategy_health() is None

    def _raise(*a, **k):
        raise OSError("spawn failed")

    monkeypatch.setattr(sch.subprocess, "run", _raise)
    assert sch._write_strategy_health() is None


def test_repo_settings_enable_strategy_health():
    """仓库配置必须真的开着，否则 1.5 步等于没做。"""
    import yaml

    cfg = yaml.safe_load((ROOT / "config" / "settings.yaml").read_text(encoding="utf-8"))
    sh = cfg.get("strategy_health") or {}
    assert sh.get("enabled") is True
    assert str(sh.get("out_json") or "").endswith("strategy_health.json")
    assert int(sh.get("refresh_hours") or 0) > 0
