"""行为体检缓存「过期自动重算」的单元测试（2026-11-16）。

守的是四条护栏（这条链路出问题会让每日巡检挂掉，所以必须锁死）：
1. 未配 fills → 不重算（保留「只读缓存」旧行为）
2. 缓存新鲜 → 不重算
3. 缓存过期 + fills 有效 → 调一次 CLI，且参数正确
4. 交割单缺失 / CLI 非零退出 / 子进程异常 → 返回 False 且**不抛异常**
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


def _touch(path: Path, age_days: float) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{}", encoding="utf-8")
    ts = time.time() - age_days * 86400
    os.utime(path, (ts, ts))
    return path


def test_no_fills_means_no_refresh(tmp_path, monkeypatch):
    cache = _touch(tmp_path / "r.json", age_days=30)
    called = []
    monkeypatch.setattr(sch.subprocess, "run", lambda *a, **k: called.append(a) or None)
    assert sch._maybe_refresh_behavior_audit({"fills": []}, cache) is False
    assert called == [], "未配 fills 不得触发重算"


def test_fresh_cache_not_refreshed(tmp_path, monkeypatch):
    fills = tmp_path / "f.csv"
    fills.write_text("x", encoding="utf-8")
    cache = _touch(tmp_path / "r.json", age_days=1)
    called = []
    monkeypatch.setattr(sch.subprocess, "run", lambda *a, **k: called.append(a) or None)
    assert sch._maybe_refresh_behavior_audit(
        {"fills": [str(fills)], "auto_refresh_days": 7}, cache) is False
    assert called == []


def test_stale_cache_triggers_refresh(tmp_path, monkeypatch):
    fills = tmp_path / "f.csv"
    fills.write_text("x", encoding="utf-8")
    cache = _touch(tmp_path / "r.json", age_days=30)

    seen = {}

    class _Proc:
        returncode = 0
        stderr = ""

    def _fake_run(cmd, **kwargs):
        seen["cmd"] = cmd
        seen["kwargs"] = kwargs
        return _Proc()

    monkeypatch.setattr(sch.subprocess, "run", _fake_run)
    assert sch._maybe_refresh_behavior_audit(
        {"fills": [str(fills)], "auto_refresh_days": 7}, cache) is True
    cmd = seen["cmd"]
    assert any("behavior_audit.py" in str(x) for x in cmd)
    assert "--json" in cmd and str(cache) in cmd
    assert str(fills) in cmd
    assert seen["kwargs"].get("timeout"), "子进程必须带超时，否则巡检可能被卡死"


def test_missing_fills_file_skips_refresh(tmp_path, monkeypatch):
    cache = _touch(tmp_path / "r.json", age_days=30)
    called = []
    monkeypatch.setattr(sch.subprocess, "run", lambda *a, **k: called.append(a) or None)
    assert sch._maybe_refresh_behavior_audit(
        {"fills": [str(tmp_path / "nope.csv")]}, cache) is False
    assert called == []


def test_subprocess_failure_is_swallowed(tmp_path, monkeypatch):
    """CLI 失败绝不能让巡检抛异常。"""
    fills = tmp_path / "f.csv"
    fills.write_text("x", encoding="utf-8")
    cache = _touch(tmp_path / "r.json", age_days=30)

    class _Proc:
        returncode = 2
        stderr = "boom"

    monkeypatch.setattr(sch.subprocess, "run", lambda *a, **k: _Proc())
    assert sch._maybe_refresh_behavior_audit({"fills": [str(fills)]}, cache) is False

    def _raise(*a, **k):
        raise OSError("spawn failed")

    monkeypatch.setattr(sch.subprocess, "run", _raise)
    assert sch._maybe_refresh_behavior_audit({"fills": [str(fills)]}, cache) is False


def test_missing_cache_file_is_treated_as_stale(tmp_path, monkeypatch):
    fills = tmp_path / "f.csv"
    fills.write_text("x", encoding="utf-8")
    cache = tmp_path / "not_yet.json"          # 不存在

    class _Proc:
        returncode = 0
        stderr = ""

    monkeypatch.setattr(sch.subprocess, "run", lambda *a, **k: _Proc())
    assert sch._maybe_refresh_behavior_audit({"fills": [str(fills)]}, cache) is True


def test_repo_settings_have_auto_refresh_wired():
    """仓库配置必须真的把 fills 接上，否则这条机制形同虚设。"""
    import yaml

    cfg = yaml.safe_load((ROOT / "config" / "settings.yaml").read_text(encoding="utf-8"))
    ba = cfg.get("behavior_audit") or {}
    assert ba.get("enabled") is True
    assert isinstance(ba.get("fills"), list) and ba["fills"], "settings.yaml 必须配置 fills"
    assert int(ba.get("auto_refresh_days", 0)) > 0
