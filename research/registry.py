"""研究假设登记册（预注册闸）：加载 / 严格校验 / 追加。

背景（docs/研究纪律_20261114.md）
--------------------------------
2026-11-14 的分析里翻过两次同类车：

1. **前视偏差**：`lag=0` 让当日收盘算出的市场状态驱动当日收益，虚增 **52.49pp**
   （同一轮内 +61.87% → T+1 诚实口径 +9.38%）；
2. **事后挑规则**：从一堆离场规则里挑出「MACD 死叉 +27,072 元」，是在知道结果之后
   挑的，样本外未必成立。

根因不是能力，而是**没有制度化约束**。本模块是其中"预注册"闸的实现：任何假设在
验证之前先登记（写清 rationale / 样本切分 / 通过标准），验证后只能按证据推进状态；
`status: passed` 强制要求 `evidence` 与 `decided_at` 非空——**不许自证**。

设计纪律
--------
- 纯函数优先：文件 IO 只出现在 `load_registry` / `append_entry`；
- 无网络、无行情数据、无模型调用；
- `validate_entry` / `validate_registry` 返回中文错误列表（不抛异常）；
  `append_entry` 校验失败才 raise ValueError，且**非法条目绝不落盘**；
- 登记册 schema 为**封闭集合**：未知字段视为错误（防拼写错误被静默忽略）。
"""

from __future__ import annotations

import datetime as _dt
from pathlib import Path
from typing import Any, Iterable, Optional

import yaml

# 登记册默认路径（research/registry.py → research/hypothesis_registry.yaml）
DEFAULT_PATH = Path(__file__).resolve().parent / "hypothesis_registry.yaml"

# 状态白名单 / 配对口径白名单（口径声明：fifo=逐笔先进先出；wave=持仓周期聚合）
# 2026-11-16 新增 inconclusive：**设计无检验力**（样本量/触发次数不足，既不能确认也不能否证）。
# 为什么需要它：H-2026-11-16-008 曾被判 failed，但证据自己写着「只触发 3 次、无检验力」——
# 在无检验力上判否定，与「检验了但不达标」是两回事，混在 failed 里会污染 yes/no 叙事。
STATUSES: tuple[str, ...] = ("pending", "passed", "failed", "rejected", "inconclusive")
PAIRINGS: tuple[str, ...] = ("fifo", "wave")

# pass_criteria 必填键（缺任何一项都无法判定"通过"）
# 2026-11-16 新增 decision_rule：**判定规则必须事前用自然语言写清**。
# 起因：H-006 预注册只写了 min_oos_improvement_pp>=2.0，实际却用「1/3 种子方向反转」
# 和「换实盘策略后效应消失」两条**未登记**的理由判了 failed —— 事后挪动标准。
# 光有数值门槛不够，多重检验校正、稳健性要求、换样本要求都得在事前落成文字。
PASS_CRITERIA_KEYS: tuple[str, ...] = (
    "min_oos_improvement_pp",
    "max_mdd_increase_pp",
    "require_significance",
    "decision_rule",
)

# 封闭 schema：允许的字段集合（含可选 notes 供人工注释）
ENTRY_KEYS = frozenset({
    "id", "title", "rationale", "pre_registered_at", "method",
    "pass_criteria", "status", "evidence", "decided_at", "notes",
})
METHOD_KEYS = frozenset({"pairing", "universe", "is_window", "oos_window", "notes"})
PASS_KEYS = frozenset(set(PASS_CRITERIA_KEYS) | {"notes"})

# append_entry 重写文件时保留的统一表头（YAML 注释无法被 safe_load 保留，
# 故由代码常量化；手工编辑请优先用 append_entry，避免丢注释与格式）。
HEADER = (
    "# 研究假设登记册（预注册闸）——机器维护，请勿手改格式；追加请用 "
    "research.registry.append_entry\n"
    "# 规则见 docs/研究纪律_20261114.md：未登记的参数变更不得上线。\n"
    "# status: pending | passed | failed | rejected | inconclusive；"
    "非 pending 必须同时有 evidence 与 decided_at（不许自证）。\n"
    "# pass_criteria.decision_rule 必须事前用文字写清判定规则（含多重检验/稳健性要求），"
    "禁止事后追加理由。\n"
)


# ===== 基础解析工具（纯函数） =====


def _is_nonempty_str(v: Any) -> bool:
    """非空字符串（纯空白不算）。"""
    return isinstance(v, str) and bool(v.strip())


def parse_ymd(v: Any) -> Optional[_dt.date]:
    """严格解析 ``YYYY-MM-DD``（拒绝 ``2024-1-1`` / ``20240101`` / 带时间）；失败 → None。"""
    if not isinstance(v, str):
        return None
    text = v.strip()
    if len(text) != 10:
        return None
    try:
        d = _dt.date.fromisoformat(text)
    except ValueError:
        return None
    # 严格性：roundtrip 必须逐字符一致（fromisoformat 允许 "20240101" 等写法）
    return d if d.isoformat() == text else None


def _window_errors(label: str, spec: Any) -> tuple[list[str], Optional[tuple[_dt.date, _dt.date]]]:
    """校验时间窗口：2 个严格 YYYY-MM-DD，且开始 ≤ 结束。返回 (错误, 解析结果)。"""
    if not isinstance(spec, (list, tuple)) or len(spec) != 2:
        return [f"{label} 必须是包含 2 个 'YYYY-MM-DD' 的列表（当前 {spec!r}）"], None
    start, end = parse_ymd(spec[0]), parse_ymd(spec[1])
    errors: list[str] = []
    if start is None:
        errors.append(f"{label}[0] 不是合法的 'YYYY-MM-DD'：{spec[0]!r}")
    if end is None:
        errors.append(f"{label}[1] 不是合法的 'YYYY-MM-DD'：{spec[1]!r}")
    if errors:
        return errors, None
    assert start is not None and end is not None
    if start > end:
        errors.append(f"{label} 顺序颠倒：开始日 {start} 晚于结束日 {end}")
    return errors, (start, end)


def _unknown_keys_errors(label: str, value: dict, allowed: frozenset) -> list[str]:
    """封闭 schema：未知字段报错（防拼写错误被静默忽略）。"""
    unknown = sorted(set(value) - set(allowed))
    if unknown:
        return [f"{label} 含未知字段 {unknown}（允许：{sorted(allowed)}）"]
    return []


# ===== 校验（纯函数，不抛异常） =====


def validate_entry(entry: Any) -> list[str]:
    """校验单条假设，返回错误列表（空列表 = 合法）。

    规则（与 docs/研究纪律_20261114.md 一致）：
      - id / title / rationale / pre_registered_at 必填；
      - ``method.pairing`` ∈ {"fifo", "wave"}；``method.universe`` 非空；
      - ``is_window`` / ``oos_window`` 各为 2 个 ``YYYY-MM-DD``，且 is 结束 < oos 开始；
      - ``status`` ∈ {"pending", "passed", "failed", "rejected"}；
      - ``status == "passed"`` 时 evidence 非空且 decided_at 为合法日期（不许自证）。
    """
    if not isinstance(entry, dict):
        return [f"假设条目必须是 dict，当前为 {type(entry).__name__}"]

    errors: list[str] = []
    errors.extend(_unknown_keys_errors("条目", entry, ENTRY_KEYS))

    if not _is_nonempty_str(entry.get("id")):
        errors.append("id 必填且为非空字符串（如 H-2026-11-14-001）")
    if not _is_nonempty_str(entry.get("title")):
        errors.append("title 必填且为非空字符串")
    if not _is_nonempty_str(entry.get("rationale")):
        errors.append("rationale 必填且为非空字符串（为什么先验地相信它）")
    if parse_ymd(entry.get("pre_registered_at")) is None:
        errors.append(
            f"pre_registered_at 必须是 'YYYY-MM-DD'：{entry.get('pre_registered_at')!r}")

    # ---- method：口径声明 + 样本切分 ----
    method = entry.get("method")
    is_win = oos_win = None
    if not isinstance(method, dict):
        errors.append("method 必填且为 dict（pairing/universe/is_window/oos_window）")
    else:
        errors.extend(_unknown_keys_errors("method", method, METHOD_KEYS))
        pairing = method.get("pairing")
        if pairing not in PAIRINGS:
            errors.append(
                f"method.pairing 必须是 {list(PAIRINGS)} 之一（当前 {pairing!r}）"
                "——口径必须显式声明，不得留空")
        if not _is_nonempty_str(method.get("universe")):
            errors.append("method.universe 必填且为非空字符串（样本范围必须写清）")
        is_errors, is_win = _window_errors("method.is_window", method.get("is_window"))
        oos_errors, oos_win = _window_errors("method.oos_window", method.get("oos_window"))
        errors.extend(is_errors)
        errors.extend(oos_errors)
        if is_win is not None and oos_win is not None and is_win[1] >= oos_win[0]:
            errors.append(
                f"样本内窗口结束日 {is_win[1]} 必须早于样本外窗口开始日 {oos_win[0]}"
                "（is/oos 不得重叠，禁止全样本调参）")

    # ---- pass_criteria：先写死通过标准，再验证 ----
    crit = entry.get("pass_criteria")
    if not isinstance(crit, dict):
        errors.append("pass_criteria 必填且为 dict（先定通过标准，后看结果）")
    else:
        errors.extend(_unknown_keys_errors("pass_criteria", crit, PASS_KEYS))
        for key in PASS_CRITERIA_KEYS:
            if key not in crit:
                errors.append(f"pass_criteria.{key} 必填")
        for key in ("min_oos_improvement_pp", "max_mdd_increase_pp"):
            value = crit.get(key)
            if key in crit and (
                isinstance(value, bool) or not isinstance(value, (int, float))
            ):
                errors.append(f"pass_criteria.{key} 必须是数值（当前 {value!r}）")
        if "require_significance" in crit and not isinstance(
                crit.get("require_significance"), bool):
            errors.append("pass_criteria.require_significance 必须是布尔值")
        # 2026-11-16：判定规则必须事前落成文字。光有数值门槛挡不住「事后追加理由」。
        if not _is_nonempty_str(crit.get("decision_rule")):
            errors.append(
                "pass_criteria.decision_rule 必填且为非空字符串——"
                "必须事前写清判定规则（含多重检验校正、稳健性/换样本要求、"
                "无检验力时如何判），否则等于允许事后挪动标准")

    # ---- status：白名单 + 非 pending 的"不许自证"约束 ----
    status = entry.get("status")
    if status not in STATUSES:
        errors.append(f"status 必须是 {list(STATUSES)} 之一（当前 {status!r}）")
    elif status != "pending":
        evidence = entry.get("evidence")
        empty = (
            evidence is None
            or (isinstance(evidence, str) and not evidence.strip())
            or (isinstance(evidence, (list, tuple, dict)) and len(evidence) == 0)
        )
        if empty:
            errors.append(
                f"status={status} 时 evidence 必须非空（禁止自证：必须写清验证结果/出处）")
        if parse_ymd(entry.get("decided_at")) is None:
            errors.append(
                f"status={status} 时 decided_at 必须是 'YYYY-MM-DD'"
                f"（当前 {entry.get('decided_at')!r}）")
    return errors


def validate_registry(entries: Iterable[Any]) -> list[str]:
    """校验整本登记册（逐条 schema + id 唯一性），返回错误列表（空 = 合法）。

    注意：本函数只做校验，不修复任何数据；错误消息带条目序号与 id 便于定位。
    """
    if not isinstance(entries, (list, tuple)):
        return [f"登记册根节点必须是列表，当前为 {type(entries).__name__}"]

    errors: list[str] = []
    seen: dict[str, int] = {}
    for idx, entry in enumerate(entries):
        entry_errors = validate_entry(entry)
        label = f"第 {idx + 1} 条"
        if isinstance(entry, dict):
            label += f"（id={entry.get('id')!r}）"
        errors.extend(f"{label}：{msg}" for msg in entry_errors)
        if isinstance(entry, dict) and _is_nonempty_str(entry.get("id")):
            entry_id = str(entry["id"])
            if entry_id in seen:
                errors.append(
                    f"id 重复：{entry_id!r}（第 {seen[entry_id]} 条与第 {idx + 1} 条）")
            else:
                seen[entry_id] = idx + 1
    return errors


# ===== 文件 IO =====


def load_registry(path: Optional[Path | str] = None) -> list[dict]:
    """读登记册；文件缺失 → []（不抛）。YAML 损坏 / 根节点非列表 → ValueError。"""
    target = Path(path) if path is not None else DEFAULT_PATH
    if not target.exists():
        return []
    try:
        raw = yaml.safe_load(target.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as e:
        raise ValueError(f"登记册读取/解析失败：{target} | {e}") from e
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise ValueError(f"登记册根节点必须是列表：{target}（当前 {type(raw).__name__}）")
    return list(raw)


def _dump_registry(entries: list[dict], path: Path) -> None:
    """原子写登记册（统一表头 + YAML）。失败抛 OSError，由调用方决定是否吞。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    body = yaml.safe_dump(
        entries, allow_unicode=True, sort_keys=False,
        default_flow_style=False, width=100,
    )
    tmp = path.with_suffix(".tmp")
    tmp.write_text(HEADER + body, encoding="utf-8")
    tmp.replace(path)


def append_entry(entry: Any, path: Optional[Path | str] = None) -> dict:
    """追加一条假设：校验失败 / id 重复 → ValueError，且**原文件不变**。

    Returns:
        实际写入的条目（dict）。
    """
    errors = validate_entry(entry)
    if errors:
        raise ValueError("非法假设条目，未写入：" + "；".join(errors))
    target = Path(path) if path is not None else DEFAULT_PATH
    entries = load_registry(target)
    existing_ids = {
        str(e.get("id")) for e in entries if isinstance(e, dict) and _is_nonempty_str(e.get("id"))
    }
    if str(entry["id"]) in existing_ids:
        raise ValueError(f"id 重复，未写入：{entry['id']!r}")
    entries.append(entry)
    _dump_registry(entries, target)
    return entry


# ===== 汇总（纯函数） =====


def summarize(entries: Iterable[Any]) -> dict:
    """各 status 计数（含 total；未知 status 不计入任何一档）。"""
    counts = {status: 0 for status in STATUSES}
    total = 0
    for entry in entries or []:
        if not isinstance(entry, dict):
            continue
        total += 1
        status = entry.get("status")
        if status in counts:
            counts[status] += 1
    return {"total": total, **counts}
