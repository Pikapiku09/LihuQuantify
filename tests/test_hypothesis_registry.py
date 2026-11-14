"""假设登记册（预注册闸）回归测试。

覆盖：schema 各条规则、passed 缺 evidence/decided_at 被拒、pairing 非白名单被拒、
窗口重叠被拒、append 非法条目**不落盘**、id 唯一性、汇总计数。
"""

from __future__ import annotations

import copy
import sys
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from research.registry import (  # noqa: E402
    HEADER,
    STATUSES,
    append_entry,
    load_registry,
    summarize,
    validate_entry,
    validate_registry,
)


def _entry() -> dict:
    """合法基准条目（每个用例只改一处，定位错误来源）。"""
    return {
        "id": "H-2026-11-14-999",
        "title": "测试假设：MA20 离场优于 MA10 破位",
        "rationale": "仅用于单测的占位理由",
        "pre_registered_at": "2026-11-14",
        "method": {
            "pairing": "fifo",
            "universe": "IR 系 435 轮",
            "is_window": ["2024-09-24", "2025-12-31"],
            "oos_window": ["2026-01-01", "2026-11-10"],
        },
        "pass_criteria": {
            "min_oos_improvement_pp": 2.0,
            "max_mdd_increase_pp": 5.0,
            "require_significance": False,
        },
        "status": "pending",
        "evidence": None,
        "decided_at": None,
    }


def _errors_of(**mutations) -> list[str]:
    entry = _entry()
    for path, value in mutations.items():
        target = entry
        keys = path.split(".")
        for key in keys[:-1]:
            target = target[key]
        target[keys[-1]] = value
    return validate_entry(entry)


def _tmp_registry(tmp_path: Path, entries: list[dict] | None = None) -> Path:
    """写一本临时登记册（带统一表头）。"""
    path = tmp_path / "hypothesis_registry.yaml"
    body = yaml.safe_dump(entries if entries is not None else [_entry()],
                          allow_unicode=True, sort_keys=False)
    path.write_text(HEADER + body, encoding="utf-8")
    return path


# ============================================================
# 1. 合法条目基线
# ============================================================

def test_valid_entry_has_no_errors():
    assert validate_entry(_entry()) == []


def test_valid_wave_entry_has_no_errors():
    entry = _entry()
    entry["method"]["pairing"] = "wave"
    assert validate_entry(entry) == []


# ============================================================
# 2. 必填字段
# ============================================================

@pytest.mark.parametrize("field", ["id", "title", "rationale", "pre_registered_at"])
def test_missing_required_fields(field):
    entry = _entry()
    entry.pop(field)
    errors = validate_entry(entry)
    assert errors, f"缺 {field} 必须报错"
    assert any(field in msg for msg in errors)


@pytest.mark.parametrize("bad", ["", "   ", None, 123])
def test_id_must_be_nonempty_string(bad):
    assert any("id" in msg for msg in _errors_of(id=bad))


def test_pre_registered_at_must_be_strict_date():
    assert _errors_of(pre_registered_at="2026-1-1")
    assert _errors_of(pre_registered_at="2026-11-14T00:00:00")
    assert not _errors_of(pre_registered_at="2026-11-14")


def test_unknown_entry_key_rejected():
    errors = _errors_of(typo_field="x")
    assert any("未知字段" in msg for msg in errors)


# ============================================================
# 3. method：pairing 白名单 + 窗口 schema
# ============================================================

@pytest.mark.parametrize("pairing", ["fifo", "wave"])
def test_pairing_whitelist_accepts(pairing):
    assert not _errors_of(**{"method.pairing": pairing})


@pytest.mark.parametrize("pairing", ["price", "FIFO", "", None])
def test_pairing_whitelist_rejects(pairing):
    errors = _errors_of(**{"method.pairing": pairing})
    assert any("pairing" in msg for msg in errors)


def test_method_universe_required():
    assert any("universe" in msg for msg in _errors_of(**{"method.universe": ""}))


@pytest.mark.parametrize("window_key,spec", [
    ("method.is_window", ["2024-09-24"]),
    ("method.is_window", "2024-09-24,2025-12-31"),
    ("method.oos_window", ["2026-01-01"]),
    ("method.oos_window", ["2026-13-01", "2026-11-10"]),
    ("method.is_window", ["2025-12-31", "2024-09-24"]),   # 自身顺序颠倒
])
def test_window_schema_rejects(window_key, spec):
    errors = _errors_of(**{window_key: spec})
    assert errors, f"{window_key}={spec!r} 必须报错"


def test_window_overlap_rejected():
    """is 的结束日 == oos 的开始日 → 重叠，必须报错。"""
    errors = _errors_of(**{"method.oos_window": ["2025-12-31", "2026-11-10"]})
    assert any("重叠" in msg for msg in errors)


def test_window_reversed_windows_rejected():
    errors = _errors_of(**{"method.oos_window": ["2024-01-01", "2024-12-31"]})
    assert any("重叠" in msg for msg in errors)


def test_unknown_method_key_rejected():
    assert any("未知字段" in msg for msg in
               _errors_of(**{"method.pairing_typo": "fifo"}))


# ============================================================
# 4. pass_criteria：先定标准
# ============================================================

def test_missing_pass_criteria_key_rejected():
    entry = _entry()
    entry["pass_criteria"].pop("min_oos_improvement_pp")
    errors = validate_entry(entry)
    assert any("min_oos_improvement_pp" in msg for msg in errors)


@pytest.mark.parametrize("key,bad", [
    ("min_oos_improvement_pp", "2.0"),
    ("max_mdd_increase_pp", None),
    ("min_oos_improvement_pp", True),
])
def test_pass_criteria_numeric_rejected(key, bad):
    assert any(key in msg for msg in _errors_of(**{f"pass_criteria.{key}": bad}))


def test_require_significance_must_be_bool():
    errors = _errors_of(**{"pass_criteria.require_significance": "yes"})
    assert any("require_significance" in msg for msg in errors)


# ============================================================
# 5. status 白名单 + passed 的"不许自证"
# ============================================================

@pytest.mark.parametrize("status", ["done", "pass", "", None])
def test_status_whitelist_rejects(status):
    assert any("status" in msg for msg in _errors_of(status=status))


@pytest.mark.parametrize("status", ["pending", "failed", "rejected"])
def test_non_passed_statuses_do_not_require_evidence(status):
    assert not _errors_of(status=status)


def test_passed_status_accepted_with_evidence_and_date():
    entry = _entry()
    entry.update({
        "status": "passed",
        "evidence": "OOS +4.1pp，p=0.03；复现脚本 scripts/xxx.py",
        "decided_at": "2026-11-20",
    })
    assert validate_entry(entry) == []


@pytest.mark.parametrize("evidence", [None, "", "   ", [], {}])
def test_passed_requires_nonempty_evidence(evidence):
    entry = _entry()
    entry.update({"status": "passed", "evidence": evidence,
                  "decided_at": "2026-11-20"})
    errors = validate_entry(entry)
    assert any("evidence" in msg for msg in errors)


@pytest.mark.parametrize("decided_at", [None, "", "2026-13-01", "20261120"])
def test_passed_requires_valid_decided_at(decided_at):
    entry = _entry()
    entry.update({"status": "passed", "evidence": "OOS 通过",
                  "decided_at": decided_at})
    errors = validate_entry(entry)
    assert any("decided_at" in msg for msg in errors)


# ============================================================
# 6. validate_registry：逐条 + id 唯一
# ============================================================

def test_registry_clean_has_no_errors():
    assert validate_registry([_entry()]) == []


def test_registry_duplicate_ids_rejected():
    first, second = _entry(), copy.deepcopy(_entry())
    errors = validate_registry([first, second])
    assert any("id 重复" in msg for msg in errors)


def test_registry_error_message_contains_index():
    first = _entry()
    second = copy.deepcopy(_entry())
    second["id"] = "H-2026-11-14-998"
    second["method"]["pairing"] = "price"
    errors = validate_registry([first, second])
    assert any("第 2 条" in msg and "pairing" in msg for msg in errors)


def test_registry_root_must_be_list():
    assert validate_registry({"id": "x"})
    assert validate_registry("not-a-list")


def test_registry_flags_non_dict_entry():
    assert validate_registry([_entry(), "garbage"])


# ============================================================
# 7. load / append / summarize
# ============================================================

def test_load_missing_file_returns_empty(tmp_path):
    assert load_registry(tmp_path / "missing.yaml") == []


def test_load_parses_file(tmp_path):
    path = _tmp_registry(tmp_path)
    entries = load_registry(path)
    assert len(entries) == 1 and entries[0]["id"] == _entry()["id"]


def test_load_corrupt_yaml_raises(tmp_path):
    path = tmp_path / "bad.yaml"
    path.write_text("- id: [unclosed\n", encoding="utf-8")
    with pytest.raises(ValueError):
        load_registry(path)


def test_load_non_list_root_raises(tmp_path):
    path = tmp_path / "dict.yaml"
    path.write_text("foo: bar\n", encoding="utf-8")
    with pytest.raises(ValueError):
        load_registry(path)


def test_append_invalid_entry_does_not_touch_file(tmp_path):
    path = _tmp_registry(tmp_path)
    before = path.read_bytes()
    bad = _entry()
    bad["method"]["pairing"] = "price"

    with pytest.raises(ValueError, match="pairing"):
        append_entry(bad, path)

    assert path.read_bytes() == before, "非法条目绝不允许落盘"


def test_append_valid_entry_persists(tmp_path):
    path = _tmp_registry(tmp_path)
    new = _entry()
    new["id"] = "H-2026-11-14-1000"
    new["title"] = "新假设：测试追加"

    append_entry(new, path)

    entries = load_registry(path)
    assert len(entries) == 2
    assert entries[-1]["id"] == "H-2026-11-14-1000"
    assert validate_registry(entries) == []


def test_append_duplicate_id_rejected_and_file_unchanged(tmp_path):
    path = _tmp_registry(tmp_path)
    before = path.read_bytes()
    with pytest.raises(ValueError, match="重复"):
        append_entry(_entry(), path)
    assert path.read_bytes() == before


def test_summarize_counts():
    entries = [
        _entry(),
        {**_entry(), "id": "H-2", "status": "passed", "evidence": "x",
         "decided_at": "2026-11-20"},
        {**_entry(), "id": "H-3", "status": "failed"},
        {**_entry(), "id": "H-4", "status": "rejected"},
        "garbage",                      # 非 dict 不计入
        {**_entry(), "id": "H-5", "status": "unknown"},   # 未知状态不计入各档
    ]
    stats = summarize(entries)
    assert stats == {"total": 5, "pending": 1, "passed": 1,
                     "failed": 1, "rejected": 1}
    assert set(STATUSES) == {"pending", "passed", "failed", "rejected"}
