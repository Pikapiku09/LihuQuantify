"""评审进度统计（100 笔 live 验收用，纯统计无 IO）。

口径：与 scheduler._monthly_review / backtest.metrics._pair_rounds 一致
（买入-卖出 FIFO 配对为一轮）。本模块是唯一统计入口，禁止另写配对算法。

计数口径：按"已平仓轮次"计（一买一卖配对为 1 轮），不是买入笔数、
也不是 sell 条数——只有配对完成的轮次才有胜率/盈亏比，可与回测基准
（58.9% / 1.25）三方对比（USER_GUIDE §5.0）。
"""

from __future__ import annotations

import datetime as _dt
from typing import Optional

from ..backtest.metrics import _pair_rounds
from ..types import TradeRecord

REVIEW_TARGET = 100          # 正式评审目标轮次（USER_GUIDE §5.0）
CHECKPOINT_50 = 50           # 预检点：胜率 <45% 需停下检查


def _as_date(v) -> Optional[_dt.date]:
    """date / datetime / ISO str 统一转 date（str 截断到 10 位）。"""
    if isinstance(v, _dt.date):
        return v
    if isinstance(v, str):
        try:
            return _dt.date.fromisoformat(v[:10])
        except ValueError:
            return None
    return None


def review_stats(trades: list[dict]) -> dict:
    """从成交流水算评审指标。trades 元素须含 ts_code/trade_date 或 date/side/
    price/volume/commission/stamp_tax；date 字段 date 或 ISO str 均可。"""
    recs = []
    for t in trades or []:
        d = _as_date(t.get("trade_date") or t.get("date"))
        recs.append(TradeRecord(
            ts_code=t["ts_code"],
            trade_date=d or _dt.date.today(),
            side=t["side"], price=t["price"], volume=t["volume"],
            commission=t.get("commission", 0),
            stamp_tax=t.get("stamp_tax", 0),
        ))
    rounds = _pair_rounds(recs)                    # list[float]，每轮净盈亏
    wins = [r for r in rounds if r > 0]
    losses = [r for r in rounds if r < 0]
    closed = len(rounds)
    win_rate = len(wins) / closed if closed else None
    avg_win = sum(wins) / len(wins) if wins else None
    avg_loss = abs(sum(losses) / len(losses)) if losses else None
    pl_ratio = avg_win / avg_loss if (avg_win is not None and avg_loss) else None
    return {
        "closed_rounds": closed,
        "target": REVIEW_TARGET,
        "remaining": max(0, REVIEW_TARGET - closed),
        "win_rate": win_rate,        # None=尚无平仓轮
        "avg_win": avg_win,
        "avg_loss": avg_loss,
        "pl_ratio": pl_ratio,
        "realized_pnl": sum(rounds),  # 已实现净盈亏（已含双边费用）
        "stage": _stage(closed),
    }


def _stage(closed: int) -> str:
    """评审阶段：<50 积累中 / ≥50 达预检点 / ≥100 达评审点。"""
    if closed >= REVIEW_TARGET:
        return "ready_review"          # 可进行 100 笔正式评审
    if closed >= CHECKPOINT_50:
        return "ready_checkpoint50"    # 达 50 轮预检点
    return "accumulating"


def fmt_stage(stage: str) -> str:
    return {
        "accumulating": "积累中",
        "ready_checkpoint50": "≥50 轮 · 可预检",
        "ready_review": "≥100 轮 · 可评审",
    }.get(stage, stage)
