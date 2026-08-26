"""蜡烛图形态识别 —— 最高优先级预警。

定义（来自 dsh-invest-plugin prompts.js P_DEEP §1）：
    射击之星（上影 > 实体 3 倍）/ 光头阴线 / 黄昏之星 / 看跌吞没 / 天量滞涨
    → 对应清仓或减仓预警

回归基线（samples/reports/长电科技_深度分析_含Checklist闸门.md §1）：
    600584.SH 8/19：天量长阴（开 81.11/高 83.23/低 76.95/收 77.74，-8.99%）
    上影 2.12 / 下影 0.79 / 实体 3.37 的光脚大阴线 → 清仓/减仓预警
"""

from __future__ import annotations

import numpy as np
import pandas as pd


def _bar_anatomy(row: pd.Series) -> dict:
    """计算单根 K 线的实体、上下影线。"""
    o, h, l, c = row["open"], row["high"], row["low"], row["close"]
    body = abs(c - o)
    upper = h - max(o, c)
    lower = min(o, c) - l
    rng = (h - l) or np.nan
    return {
        "body": body,
        "upper": upper,
        "lower": lower,
        "range": rng,
        "body_ratio": body / rng if rng and not np.isnan(rng) else 0.0,
        "is_red": c >= o,
    }


def detect_candlestick_warning(df: pd.DataFrame, vol_avg_window: int = 20) -> list[dict]:
    """识别看跌蜡烛图形态，返回预警列表。

    要求 df 至少含 open/high/low/close/vol 列，按 trade_date 升序。
    每项返回 {date, pattern, action, reason}。
    """
    if df.empty or not {"open", "high", "low", "close", "vol"}.issubset(df.columns):
        return []

    df = df.copy()
    df["vol_ma"] = df["vol"].rolling(vol_avg_window, min_periods=5).mean()
    warnings: list[dict] = []
    dates = df["trade_date"] if "trade_date" in df.columns else pd.Series(df.index)

    for i in range(len(df)):
        row = df.iloc[i]
        a = _bar_anatomy(row)
        date = dates.iloc[i] if i < len(dates) else None

        # 1. 射击之星：上影 > 3 倍实体，实体小，位于上涨后
        if not a["is_red"] and a["upper"] > 3 * max(a["body"], 1e-9) and a["body_ratio"] < 0.3:
            if i >= 3 and df["close"].iloc[i - 1] > df["close"].iloc[i - 3]:
                warnings.append({
                    "date": date, "pattern": "射击之星",
                    "action": "减仓", "reason": f"上影 {a['upper']:.2f} > 实体3倍 {a['body']:.2f}",
                })

        # 2. 光脚大阴线 / 大阴线：实体大且收阴，下影小
        if not a["is_red"] and a["body"] > 0:
            pct_chg = row.get("pct_chg", (row["close"] / row["pre_close"] - 1) if row.get("pre_close") else np.nan)
            is_big = (not pd.isna(pct_chg) and pct_chg < -5.0) or a["body"] > 3.0
            near_no_lower = a["lower"] < 0.3 * a["body"]
            if is_big:
                pattern = "光脚大阴线" if near_no_lower else "大阴线"
                # 量能确认
                vol_ratio = row["vol"] / row["vol_ma"] if row["vol_ma"] and not pd.isna(row["vol_ma"]) else 1.0
                if vol_ratio >= 1.5:
                    pattern = "天量" + pattern
                action = "清仓" if (is_big and vol_ratio >= 1.5 and (not pd.isna(pct_chg) and pct_chg < -7)) else "减仓"
                warnings.append({
                    "date": date, "pattern": pattern,
                    "action": action,
                    "reason": (
                        f"实体 {a['body']:.2f} 收阴，下影 {a['lower']:.2f}，"
                        f"上影 {a['upper']:.2f}，量比 {vol_ratio:.2f}"
                    ),
                })

        # 3. 看跌吞没：当前大阴线实体完全吞没上一根阳线实体
        if i >= 1:
            prev = df.iloc[i - 1]
            pa = _bar_anatomy(prev)
            if pa["is_red"] and not a["is_red"]:
                if row["open"] >= prev["close"] and row["close"] <= prev["open"]:
                    warnings.append({
                        "date": date, "pattern": "看跌吞没",
                        "action": "减仓",
                        "reason": "当前阴线实体吞没上一根阳线实体",
                    })

        # 4. 天量滞涨：成交量显著放大但价格变动小
        if not pd.isna(row["vol_ma"]) and row["vol_ma"] > 0:
            vol_ratio = row["vol"] / row["vol_ma"]
            pct_chg = row.get("pct_chg", np.nan)
            if vol_ratio >= 2.0 and not pd.isna(pct_chg) and abs(pct_chg) < 1.0:
                warnings.append({
                    "date": date, "pattern": "天量滞涨",
                    "action": "减仓",
                    "reason": f"量比 {vol_ratio:.2f} 但涨幅仅 {pct_chg:.2f}%",
                })

    # 按日期去重，同日保留 action 更重的一条
    deduped: dict = {}
    severity = {"清仓": 3, "减仓": 2, "观察": 1}
    for w in warnings:
        d = str(w["date"])
        if d not in deduped or severity.get(w["action"], 0) > severity.get(deduped[d]["action"], 0):
            deduped[d] = w
    return list(deduped.values())
