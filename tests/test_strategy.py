"""策略层测试：CherryClaw 三层过滤 + 六维诊断。"""

from __future__ import annotations

import math
from datetime import date, timedelta

import numpy as np
import pandas as pd
import pytest

from lihu_quantify.strategy.cherry_claw import CherryClaw
from lihu_quantify.strategy.deep_diagnose import DeepDiagnose


def _build_v_recovery(n_decline=30, n_flat=20, n_rise=20) -> pd.DataFrame:
    """构造"下跌 → 横盘 → 温和上涨"形态。

    温和上涨使 close 贴近 MA5（乖离 <1.5%），MA5 上穿 MA10 形成新鲜金叉，
    满足 CherryClaw 三层过滤。总长 ≥60 以通过 MIN_LIST_DAYS。
    """
    n = n_decline + n_flat + n_rise
    dates = [date(2026, 5, 1) + timedelta(days=i) for i in range(n)]
    closes = []
    # 下跌段：20 → 10
    for i in range(n_decline):
        closes.append(20 - 10 * (i / n_decline))
    # 横盘段：10 ± 0.15（MA5/MA10 收敛）
    for i in range(n_flat):
        closes.append(10 + 0.15 * math.sin(i))
    # 温和上涨段：10 → 11（+0.05/日，close 贴近 MA5，乖离<1.5%）
    for i in range(n_rise):
        closes.append(10 + 0.05 * (i + 1))
    # 收红 K 线（open ≥ low，body_ratio ≥ 40%）
    opens = [c - 0.3 for c in closes]
    highs = [c + 0.1 for c in closes]
    lows = [c - 0.4 for c in closes]
    # 成交量：上涨段放量
    vols = [1_000_000.0] * (n_decline + n_flat) + [1_500_000.0 + 50_000 * i for i in range(n_rise)]
    amounts = [v * c for v, c in zip(vols, closes)]
    pct_chg = [0.0] + [(closes[i] / closes[i - 1] - 1) * 100 for i in range(1, n)]
    pre_close = [closes[0]] + closes[:-1]
    return pd.DataFrame({
        "ts_code": "888888.SH", "trade_date": dates,
        "open": opens, "high": highs, "low": lows, "close": closes,
        "pre_close": pre_close, "pct_chg": pct_chg,
        "vol": vols, "amount": amounts,
    })


# ============ CherryClaw ============

def test_cherry_claw_synthetic_v_recovery():
    """V 形反转应在上涨段产生买入信号。"""
    df = _build_v_recovery()
    strategy = CherryClaw()
    signals = strategy.scan(df)
    # 至少有一个买入信号
    assert len(signals) > 0, "V 形反转上涨段应触发三层过滤"
    s = signals[-1]
    assert s.kind == "buy"
    assert s.stop_loss is not None and s.stop_loss > 0, "买入信号必须给出止损价"
    assert len(s.take_profit) == 4, "应给出 L1-L4 四个目标价"
    assert s.suggested_position_pct <= 0.25, "单票仓位 ≤25%"


def test_cherry_claw_600584_runs(daily_600584):
    """600584 真实数据：CherryClaw 能运行（不强求有信号）。"""
    strategy = CherryClaw()
    signals = strategy.scan(daily_600584)
    assert isinstance(signals, list)
    # 600584 在 6-8 月波动剧烈，可能有信号也可能无；关键是运行不报错
    for s in signals:
        assert s.kind == "buy"
        assert s.stop_loss > 0
        assert s.ts_code == "600584.SH"


def test_cherry_claw_empty_input():
    strategy = CherryClaw()
    assert strategy.scan(pd.DataFrame()) == []


def test_cherry_claw_exclude_chuangye():
    """创业板 300xxx 应被前置过滤排除。"""
    df = _build_v_recovery()
    df["ts_code"] = "300001.SZ"
    strategy = CherryClaw()
    signals = strategy.scan(df)
    assert len(signals) == 0, "创业板应被硬过滤排除"


# ============ 六维诊断 ============

def test_deep_diagnose_600584_with_819(daily_600584_with_819):
    """600584 含 8/19 天量大阴线：应触发蜡烛图预警 + 低评级。"""
    df = daily_600584_with_819
    diagnose = DeepDiagnose()
    report = diagnose.run("600584.SH", df)
    # 蜡烛图预警
    assert len(report.candle_warnings) > 0, "8/19 应触发蜡烛图预警"
    w = next((w for w in report.candle_warnings if str(w.get("date")) == str(date(2026, 8, 19))), None)
    assert w is not None, "8/19 应有蜡烛图预警"
    # 评级应为 C 或 D（低）
    assert report.rating in ("C", "D"), f"8/19 大跌后评级应 C/D，实际 {report.rating}"
    # 有止损方案
    assert report.stop_loss_price > 0
    assert "-3%" in report.stop_plan or "-8%" in report.stop_plan
    # 有 L1-L4
    assert len(report.targets) == 4
    # 九转状态应有描述
    assert report.td_desc
    # 结论含免责
    assert "不构成投资建议" in report.conclusion


def test_deep_diagnose_targets_structure(daily_600584):
    """L1-L4 目标价结构正确。"""
    diagnose = DeepDiagnose()
    report = diagnose.run("600584.SH", daily_600584)
    levels = [t.level for t in report.targets]
    assert any("L1" in l for l in levels)
    assert any("L4" in l for l in levels)
    # L1 < L2 < L3 < L4
    prices = [t.price for t in report.targets]
    assert prices[0] < prices[1] < prices[2] < prices[3]


def test_deep_diagnose_synthetic_uptrend():
    """合成上涨趋势应给出较高评级 + 均线多头。"""
    df = _build_v_recovery()
    diagnose = DeepDiagnose()
    report = diagnose.run("888888.SH", df)
    # 上涨段末端：均线应多头
    assert "多头" in report.six_dim.moving_avg or "短期多头" in report.six_dim.moving_avg
    # 评级不应是 D（无大跌预警）
    assert report.rating != "D"
    assert report.score > 40


def test_deep_diagnose_empty():
    """空数据不应报错。"""
    diagnose = DeepDiagnose()
    report = diagnose.run("600584.SH", pd.DataFrame())
    assert report.close == 0.0
    assert report.rating == "C"
