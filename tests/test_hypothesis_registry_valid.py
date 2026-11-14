"""仓库内假设登记册的"常驻校验"（预注册闸接入 pytest）。

目的：不引入新的 CI 配置——只要跑全量 ``pytest``，就会连带校验
``research/hypothesis_registry.yaml`` 的 schema 与 id 唯一性；登记册写坏时
测试直接失败（而不是等到上线才发现）。

同时验证 ``scripts/check_hypotheses.py`` 的 CLI 行为：
合法登记册退出码 0 + 中文摘要；非法登记册退出码 1。
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from research.registry import (  # noqa: E402
    DEFAULT_PATH,
    load_registry,
    validate_registry,
)

SCRIPT = ROOT / "scripts" / "check_hypotheses.py"


def _run_script(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(SCRIPT), *args],
        cwd=str(ROOT), capture_output=True, text=True,
        encoding="utf-8", errors="replace", timeout=120,
    )


# ============================================================
# 1. 仓库登记册必须合法
# ============================================================

def test_repo_registry_exists_and_is_valid():
    entries = load_registry()
    assert DEFAULT_PATH.exists(), f"登记册缺失：{DEFAULT_PATH}"
    assert entries, "登记册至少应预填 2 条真实假设"
    assert validate_registry(entries) == [], (
        "仓库内登记册存在非法条目：\n" + "\n".join(validate_registry(entries)))


def test_repo_registry_has_two_prefilled_pending_hypotheses():
    by_id = {entry["id"]: entry for entry in load_registry()}

    h1 = by_id["H-2026-11-14-001"]
    assert "MACD" in h1["title"]
    assert h1["status"] == "pending"
    assert h1["method"]["pairing"] == "fifo"
    assert h1["method"]["is_window"] == ["2024-09-24", "2025-12-31"]
    assert h1["method"]["oos_window"] == ["2026-01-01", "2026-11-10"]

    h2 = by_id["H-2026-11-14-002"]
    assert "分批" in h2["title"] or "止盈" in h2["title"]
    assert h2["status"] == "pending"
    assert h2["method"]["pairing"] in ("fifo", "wave")
    assert h2["method"]["is_window"][1] < h2["method"]["oos_window"][0]


def test_pending_hypotheses_have_no_evidence_yet():
    """预注册纪律：pending 阶段不得预先写 evidence/decided_at（否则=自证）。"""
    for entry in load_registry():
        if entry["status"] == "pending":
            assert entry.get("evidence") is None
            assert entry.get("decided_at") is None


# ============================================================
# 2. CLI 接入（脚本可正常退出并输出中文摘要）
# ============================================================

def test_script_runs_without_error_and_prints_summary():
    proc = _run_script()
    assert proc.returncode == 0, f"stdout={proc.stdout}\nstderr={proc.stderr}"
    assert "校验通过" in proc.stdout
    assert "pending" in proc.stdout
    assert "共 2 条" in proc.stdout or "共 2 条" in proc.stdout.replace(" ", "")


def test_script_exits_1_on_invalid_registry(tmp_path: Path):
    bad_entry = {
        "id": "H-BAD-000",
        "title": "非法示例",
        "rationale": "status=passed 却没有 evidence",
        "pre_registered_at": "2026-11-14",
        "method": {
            "pairing": "fifo",
            "universe": "样例",
            "is_window": ["2024-09-24", "2025-12-31"],
            "oos_window": ["2026-01-01", "2026-11-10"],
        },
        "pass_criteria": {
            "min_oos_improvement_pp": 2.0,
            "max_mdd_increase_pp": 5.0,
            "require_significance": False,
        },
        "status": "passed",
        "evidence": None,
        "decided_at": None,
    }
    path = tmp_path / "bad_registry.yaml"
    path.write_text(yaml.safe_dump([bad_entry], allow_unicode=True, sort_keys=False),
                    encoding="utf-8")

    proc = _run_script("--path", str(path))
    assert proc.returncode == 1, f"stdout={proc.stdout}\nstderr={proc.stderr}"
    assert "校验未通过" in proc.stdout
    assert "evidence" in proc.stdout


def test_script_exits_1_on_corrupt_registry(tmp_path: Path):
    path = tmp_path / "corrupt.yaml"
    path.write_text("- id: [unclosed\n", encoding="utf-8")
    proc = _run_script("--path", str(path))
    assert proc.returncode == 1
    assert "失败" in proc.stdout
