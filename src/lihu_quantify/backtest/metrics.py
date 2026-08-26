"""绩效指标：夏普/回撤/胜率/盈亏比/卡玛等。

绩效输出必须能填充 docs/月度复盘模板.md 的所有字段。
"""

from __future__ import annotations

from datetime import date

import numpy as np
import pandas as pd

from ..types import TradeRecord


def compute_metrics(
    equity_curve: pd.Series,
    trades: list[TradeRecord],
    risk_free_rate: float = 0.03,
    trading_days: int = 252,
) -> dict:
    """计算绩效指标。

    Args:
        equity_curve: 每日权益序列（按交易日升序，index=trade_date）
        trades: 成交记录列表
        risk_free_rate: 无风险利率（年化，默认 3%）
        trading_days: 年交易日数（A 股 252）
    """
    if equity_curve is None or len(equity_curve) < 2:
        return _empty_metrics()

    equity = equity_curve.astype(float).dropna()
    if len(equity) < 2:
        return _empty_metrics()

    # 收益率序列
    returns = equity.pct_change().dropna()
    total_return = equity.iloc[-1] / equity.iloc[0] - 1.0
    n_days = len(equity)
    years = n_days / trading_days
    annual_return = (1 + total_return) ** (1 / years) - 1 if years > 0 else 0.0

    # 夏普
    excess = returns - risk_free_rate / trading_days
    sharpe = (
        np.sqrt(trading_days) * excess.mean() / excess.std()
        if excess.std() > 0 else 0.0
    )

    # 最大回撤
    peak = equity.cummax()
    drawdown = (equity - peak) / peak
    max_drawdown = float(drawdown.min())

    # 卡玛
    calmar = annual_return / abs(max_drawdown) if max_drawdown < 0 else 0.0

    # 交易统计（修复5c：按买入-卖出轮次配对，FIFO）
    rounds = _pair_rounds(trades)
    win_rounds = [r for r in rounds if r > 0]
    loss_rounds = [r for r in rounds if r < 0]
    win_rate = len(win_rounds) / len(rounds) if rounds else 0.0
    avg_win = np.mean(win_rounds) if win_rounds else 0.0
    avg_loss = abs(np.mean(loss_rounds)) if loss_rounds else 0.0
    profit_loss_ratio = avg_win / avg_loss if avg_loss > 0 else 0.0

    # 费用占比（修复5c：费用/成交额）
    total_fees = sum(t.commission + t.stamp_tax for t in trades)
    total_turnover = sum(t.price * t.volume for t in trades)
    avg_cost_ratio = total_fees / total_turnover if total_turnover > 0 else 0.0

    # 持仓天数（买入到对应卖出）
    holding_days = _avg_holding_days(trades)

    # 月均交易次数
    months = max(1, int(np.ceil(n_days / 21)))
    monthly_trade_count = len(rounds) / months

    return {
        "total_return": total_return,
        "annual_return": annual_return,
        "sharpe": float(sharpe),
        "max_drawdown": max_drawdown,
        "calmar": calmar,
        "win_rate": win_rate,
        "profit_loss_ratio": profit_loss_ratio,
        "avg_holding_days": holding_days,
        "monthly_trade_count": monthly_trade_count,
        "total_trades": len(rounds),
        "final_equity": float(equity.iloc[-1]),
        "n_days": n_days,
        "avg_cost_ratio": avg_cost_ratio,
        "total_fees": total_fees,
    }


def _pair_rounds(trades: list[TradeRecord]) -> list[float]:
    """按 ts_code FIFO 配对买入-卖出轮次，每轮一个 pnl。

    修复5c：胜率按轮次而非单笔 sell 统计。
    注意：不修改原始 TradeRecord（用独立剩余量计数器）。
    """
    open_buys: dict[str, list] = {}   # 每项 = (buy_record, remaining_volume)
    rounds: list[float] = []
    sorted_trades = sorted(
        trades,
        key=lambda t: t.trade_date if isinstance(t.trade_date, date) else 0,
    )
    for t in sorted_trades:
        if t.side == "buy":
            open_buys.setdefault(t.ts_code, []).append([t, t.volume])  # [record, remaining]
        else:  # sell
            queue = open_buys.get(t.ts_code)
            if not queue:
                continue
            remaining = t.volume
            while remaining > 0 and queue:
                entry = queue[0]
                buy = entry[0]
                buy_remaining = entry[1]
                consume = min(remaining, buy_remaining)
                cost = buy.price * consume
                proceeds = t.price * consume
                # 费用按比例分摊（修复H.1：含买入侧佣金）
                buy_fee_share = buy.commission * consume / max(1, buy.volume)
                sell_fee_share = (t.commission + t.stamp_tax) * consume / max(1, t.volume)
                rounds.append(proceeds - cost - buy_fee_share - sell_fee_share)
                entry[1] -= consume
                remaining -= consume
                if entry[1] <= 0:
                    queue.pop(0)
    return rounds


def _avg_holding_days(trades: list[TradeRecord]) -> float:
    """平均持仓天数（买入到对应卖出）。"""
    # 简化：按 ts_code 配对最近的 buy/sell
    open_buys: dict[str, list] = {}
    holding_days = []
    sorted_trades = sorted(
        trades,
        key=lambda t: t.trade_date if isinstance(t.trade_date, type(t.trade_date)) else 0,
    )
    for t in sorted_trades:
        if t.side == "buy":
            open_buys.setdefault(t.ts_code, []).append(t)
        else:  # sell
            queue = open_buys.get(t.ts_code)
            if queue:
                buy = queue.pop(0)
                try:
                    d = (t.trade_date - buy.trade_date).days
                    holding_days.append(max(0, d))
                except (TypeError, AttributeError):
                    pass
    return float(np.mean(holding_days)) if holding_days else 0.0


def _empty_metrics() -> dict:
    return {
        "total_return": 0.0, "annual_return": 0.0, "sharpe": 0.0,
        "max_drawdown": 0.0, "calmar": 0.0, "win_rate": 0.0,
        "profit_loss_ratio": 0.0, "avg_holding_days": 0.0,
        "monthly_trade_count": 0.0, "total_trades": 0, "final_equity": 0.0, "n_days": 0,
        "avg_cost_ratio": 0.0, "total_fees": 0.0,
    }
