"""IS/OOS 样本切分工具（研究纪律第 2 道闸：样本外验证）。

为什么需要它
------------
2026-11-14 的事故复盘：在**全样本**上挑规则 / 调阈值，再把结果当成"策略有效"。
`lag=0` 未来函数虚增 52.49pp 与"MACD 死叉 +27,072 元"事后挑规则是同一类错误——
**没有把定参样本与验证样本分开**。

本模块提供最小、纯函数的实现（无 IO、无行情依赖）：

- :func:`parse_window`      窗口解析（严格 ``YYYY-MM-DD``）
- :func:`assert_no_overlap` 样本内/外不得重叠（重叠或顺序颠倒 → ValueError）
- :func:`split_by_date`     按日期把记录切成 IS/OOS 两段（边界日都含）
- :func:`compare_oos`       以"样本外增量是否达到预注册门槛"判定假设通过与否

口径提醒
--------
`compare_oos` 的入参是**比例值**：``0.0241`` 表示 +2.41%，输出 ``delta_pp`` 为
百分点（pp）。这与登记册 ``pass_criteria.min_oos_improvement_pp`` 的单位一致。
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any, Iterable, Optional, Sequence

__all__ = [
    "parse_window",
    "assert_no_overlap",
    "split_by_date",
    "compare_oos",
]


def _strict_window_date(value: Any, label: str) -> date:
    """窗口配置用日期：只接受严格 ``YYYY-MM-DD`` 字符串（拒绝带时间/简写）。"""
    if not isinstance(value, str):
        raise ValueError(f"{label} 必须是 'YYYY-MM-DD' 字符串，当前为 {type(value).__name__}")
    text = value.strip()
    if len(text) != 10:
        raise ValueError(f"{label} 必须是 'YYYY-MM-DD'（10 位），当前为 {value!r}")
    try:
        parsed = date.fromisoformat(text)
    except ValueError as exc:
        raise ValueError(f"{label} 不是合法日期：{value!r}") from exc
    if parsed.isoformat() != text:
        raise ValueError(f"{label} 必须写成 'YYYY-MM-DD'，当前为 {value!r}")
    return parsed


def parse_window(spec: Sequence[str]) -> tuple[date, date]:
    """把 ``["YYYY-MM-DD", "YYYY-MM-DD"]`` 解析为 ``(start, end)``（闭区间）。

    Raises:
        ValueError: 形状不是 2 元素、日期格式非法、或开始日晚于结束日。
    """
    if not isinstance(spec, (list, tuple)) or len(spec) != 2:
        raise ValueError(f"时间窗口必须是包含 2 个 'YYYY-MM-DD' 的列表，当前为 {spec!r}")
    start = _strict_window_date(spec[0], "窗口开始日")
    end = _strict_window_date(spec[1], "窗口结束日")
    if start > end:
        raise ValueError(f"窗口顺序颠倒：开始日 {start} 晚于结束日 {end}（{spec!r}）")
    return start, end


def assert_no_overlap(
    is_window: Sequence[str], oos_window: Sequence[str],
) -> tuple[tuple[date, date], tuple[date, date]]:
    """断言样本内/样本外不重叠且顺序正确；否则 raise ValueError。

    返回解析后的 ``(is_window, oos_window)``（便于调用方复用，避免二次解析）。
    规则：``is_end < oos_start``；相等也算重叠（禁止把边界日同时用于定参与验证）。
    """
    is_start, is_end = parse_window(is_window)
    oos_start, oos_end = parse_window(oos_window)
    if is_end >= oos_start:
        raise ValueError(
            f"样本内窗口 {is_window} 与样本外窗口 {oos_window} 重叠或顺序颠倒："
            f"is 结束日 {is_end} 必须早于 oos 开始日 {oos_start}"
            "（is/oos 不得重叠，禁止全样本调参）")
    return (is_start, is_end), (oos_start, oos_end)


def _row_date(value: Any) -> Optional[date]:
    """记录日期归一化：兼容 date / datetime / 字符串（容忍带时间的 ISO 串）。

    无法判定 → None（调用方丢弃该行，不抛异常——数据脏不该杀死研究脚本）。
    """
    if value is None:
        return None
    if isinstance(value, datetime):          # 含 pandas.Timestamp（datetime 子类）
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        try:
            return date.fromisoformat(text[:10])
        except ValueError:
            return None
    return None


def split_by_date(
    rows: Iterable[dict],
    date_key: str,
    is_window: Sequence[str],
    oos_window: Sequence[str],
) -> tuple[list[dict], list[dict]]:
    """按 ``rows[*][date_key]`` 切成 ``(is_rows, oos_rows)``，两窗口均为闭区间。

    行为约定：
    - 非 dict 行、日期缺失或无法解析的行 → 两段都不包含（不静默塞进某一段）；
    - 落在两个窗口之外的行 → 丢弃（本函数只负责 IS/OOS 切分，不做全样本分析）；
    - 返回顺序保持输入顺序（便于与成交流水对齐）。

    Raises:
        ValueError: 窗口非法 / 重叠（见 :func:`assert_no_overlap`）。
    """
    (is_start, is_end), (oos_start, oos_end) = assert_no_overlap(is_window, oos_window)
    is_rows: list[dict] = []
    oos_rows: list[dict] = []
    for row in rows or []:
        if not isinstance(row, dict):
            continue
        d = _row_date(row.get(date_key))
        if d is None:
            continue
        if is_start <= d <= is_end:
            is_rows.append(row)
        elif oos_start <= d <= oos_end:
            oos_rows.append(row)
    return is_rows, oos_rows


def _as_metric(value: Any, label: str) -> float:
    """指标数值校验（比例口径；bool / NaN / 非数字 → ValueError）。"""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{label} 必须是数值（比例口径），当前为 {value!r}")
    metric = float(value)
    if metric != metric:   # NaN
        raise ValueError(f"{label} 不得为 NaN")
    return metric


def compare_oos(
    is_metric: float,
    oos_metric: float,
    min_improvement_pp: float,
) -> dict:
    """比较样本内/样本外指标，判定样本外增量是否达到预注册门槛。

    Args:
        is_metric: 样本内指标（比例，如累计收益 0.0241 = +2.41%）。
        oos_metric: 样本外指标（同口径）。
        min_improvement_pp: 预注册的最小样本外改善（百分点），如 2.0 表示 +2.00pp。

    Returns:
        ``{"is": float, "oos": float, "delta_pp": float, "passed": bool}``；
        ``delta_pp = (oos - is) × 100``，``passed = delta_pp >= min_improvement_pp``。

    Raises:
        ValueError: 入参不是有效数值。
    """
    is_value = _as_metric(is_metric, "is_metric")
    oos_value = _as_metric(oos_metric, "oos_metric")
    threshold = _as_metric(min_improvement_pp, "min_improvement_pp")
    delta_pp = (oos_value - is_value) * 100.0
    return {
        "is": is_value,
        "oos": oos_value,
        "delta_pp": round(delta_pp, 6),
        # 加 1e-12 容忍浮点误差：delta 恰好等于门槛时应判通过
        "passed": bool(delta_pp + 1e-12 >= threshold),
    }
