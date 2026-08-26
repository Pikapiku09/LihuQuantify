"""MACD 背离检测 —— 自研实现。

定义（来自 dsh-invest-plugin prompts.js P_DEEP §8）：
    对比近 60-120 日价格高低点与 MACD 柱/DIF 峰值谷值
    - 价格创新高而指标峰值走低 = 顶背离（看跌）
    - 价格创新低而指标谷值抬高 = 底背离（看涨）
    标注背离级别（本级别/次级别）并纳入评分与风险提示

回归基线（samples/reports/长电科技_深度分析_含Checklist闸门.md §10）：
    600584.SH 36 日窗口内无标准顶/底背离
    （DIF 全程零轴下、峰值在 8/18 反弹端）
"""

from __future__ import annotations

from typing import Literal

import numpy as np
import pandas as pd
from scipy.signal import find_peaks


def _find_local_extrema(series: pd.Series, window: int = 5) -> list[tuple[int, float, Literal["peak", "trough"]]]:
    """找局部峰谷。返回 [(idx, value, type), ...]。"""
    arr = series.to_numpy(dtype=float)
    # 处理 NaN：find_peaks 不接受 NaN
    arr_filled = np.where(np.isfinite(arr), arr, np.nan)
    if np.all(np.isnan(arr_filled)):
        return []
    # 峰
    peaks, _ = find_peaks(np.nan_to_num(arr_filled, nan=-np.inf), distance=window)
    # 谷（取负找峰）
    troughs, _ = find_peaks(np.nan_to_num(-arr_filled, nan=-np.inf), distance=window)
    result: list[tuple[int, float, Literal["peak", "trough"]]] = []
    for p in peaks:
        if np.isfinite(arr[p]):
            result.append((int(p), float(arr[p]), "peak"))
    for t in troughs:
        if np.isfinite(arr[t]):
            result.append((int(t), float(arr[t]), "trough"))
    result.sort(key=lambda x: x[0])
    return result


def detect_macd_divergence(
    df: pd.DataFrame,
    window: int = 60,
    min_window: int = 36,
    price_col: str = "close",
    macd_col: str = "dif",
) -> list[dict]:
    """检测 MACD 背离。

    要求 df 含 close 与 dif 列（先用 indicators.standard.add_macd 计算）。
    只在最近 window 根且长度 >= min_window 时检测，避免短窗口误判。

    Returns:
        背离点列表，每项：
            {date, type:'top'|'bottom', level, price_high/low, macd_peak/trough, desc}
    """
    if macd_col not in df.columns or price_col not in df.columns:
        return []
    if len(df) < min_window:
        return []

    # 只看最近 window 根
    recent = df.tail(window).reset_index(drop=True)
    price = recent[price_col]
    macd = recent[macd_col]
    if "trade_date" in recent.columns:
        dates = recent["trade_date"]
    else:
        dates = pd.Series(recent.index)

    price_extrema = _find_local_extrema(price, window=5)
    macd_extrema = _find_local_extrema(macd, window=5)
    macd_peaks = [(i, v) for i, v, t in macd_extrema if t == "peak"]
    macd_troughs = [(i, v) for i, v, t in macd_extrema if t == "trough"]
    price_peaks = [(i, v) for i, v, t in price_extrema if t == "peak"]
    price_troughs = [(i, v) for i, v, t in price_extrema if t == "trough"]

    divergences: list[dict] = []

    # 顶背离：相邻两个价格峰，价格创新高但 MACD 峰走低
    if len(price_peaks) >= 2 and len(macd_peaks) >= 2:
        for i in range(1, len(price_peaks)):
            p_prev, v_prev = price_peaks[i - 1]
            p_cur, v_cur = price_peaks[i]
            if v_cur > v_prev:
                # 找对应的 MACD 峰（时间最近）
                macd_prev = _nearest_macd(macd_peaks, p_prev)
                macd_cur = _nearest_macd(macd_peaks, p_cur)
                if macd_prev is not None and macd_cur is not None:
                    if macd_cur[1] < macd_prev[1]:
                        divergences.append({
                            "date": _safe_date(dates, p_cur),
                            "type": "top",
                            "level": "本级别",
                            "price_high": v_cur,
                            "macd_peak": macd_cur[1],
                            "desc": f"价格创新高({v_cur:.2f} > {v_prev:.2f})但 MACD 峰值走低"
                            f"({macd_cur[1]:.2f} < {macd_prev[1]:.2f})",
                        })

    # 底背离：相邻两个价格谷，价格创新低但 MACD 谷抬高
    if len(price_troughs) >= 2 and len(macd_troughs) >= 2:
        for i in range(1, len(price_troughs)):
            p_prev, v_prev = price_troughs[i - 1]
            p_cur, v_cur = price_troughs[i]
            if v_cur < v_prev:
                macd_prev = _nearest_macd(macd_troughs, p_prev)
                macd_cur = _nearest_macd(macd_troughs, p_cur)
                if macd_prev is not None and macd_cur is not None:
                    if macd_cur[1] > macd_prev[1]:
                        divergences.append({
                            "date": _safe_date(dates, p_cur),
                            "type": "bottom",
                            "level": "本级别",
                            "price_low": v_cur,
                            "macd_trough": macd_cur[1],
                            "desc": f"价格创新低({v_cur:.2f} < {v_prev:.2f})但 MACD 谷值抬高"
                            f"({macd_cur[1]:.2f} > {macd_prev[1]:.2f})",
                        })

    return divergences


def _nearest_macd(extrema: list[tuple[int, float]], idx: int) -> tuple[int, float] | None:
    """找离 idx 最近的 MACD 极值点。"""
    if not extrema:
        return None
    return min(extrema, key=lambda x: abs(x[0] - idx))


def _safe_date(dates: pd.Series, idx: int):
    if idx < 0 or idx >= len(dates):
        return None
    val = dates.iloc[idx]
    return val
