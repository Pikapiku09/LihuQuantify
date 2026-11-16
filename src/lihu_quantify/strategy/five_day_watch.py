"""五日观察哨包装策略（研究用，未接入实盘）。

规则
----
内层策略在信号日 T 发出买入信号、成交价为 P 时，取 T 之后第 ``days`` 根 bar 的收盘价 C：
若 ``C / P - 1 < threshold``（默认 0），则在那一刻发出**全部离场**信号。

为什么用「5 日涨幅」而不是「进场涨幅」
--------------------------------------
行为归因（docs/收益归因_基本面与消息面_20261116.md）里区分度最大的是「进场涨幅」
（Cliff's delta 0.625），但它是**事后变量**——要用持有期最高价算，实盘拿不到。
实时可得的代理是「买入后 5 日涨幅」（delta 0.463 / OOS 0.528 / FDR 0.001），本包装用它。

为什么是 stateless 预计算形态
------------------------------
第一版让包装策略走引擎的**有状态 on_bar 路径**，每根 bar 都重跑一次
``_prepare_indicators``，50 只 × 510 根 bar 是 O(n²)，实测 20 分钟跑不完；
而且 IntradayReversal 这类 stateless 策略的指标列（ir20_pct）由 ``scan()`` 一次性
算好，走 on_bar 会 KeyError。
引擎里 ``kind == "sell"`` 的分支在 signal_table 查表**之后**执行，并不只限 on_bar，
所以保持 stateless、把 sell 信号在 ``_evaluate`` 里一次性预计算出来即可，复杂度 O(n)。

已知偏差（写在前面，避免事后找补）
------------------------------------
1. 引擎的 sell 信号只支持**全部卖出**（``vol = position.volume // 100 * 100``），
   所以实现的是「离场」而非原始假设里的「减半」。属于更严的版本。
2. 计数基准是**信号 bar**（记作第 0 根），不是成交 bar。买单 T+1 撮合，
   故实际是「成交后第 days-1 根 bar」。基线与实验组同口径，可比。
3. 只在第 ``days`` 根 bar 判断一次，不持续监控（对应「观察哨」语义）。
4. 若该票在观察哨之前已被止损离场，这条 sell 信号会因「不持仓」而自然失效。

上线与否由 H-2026-11-16-008 判定，本文件保持 opt-in，不接 scheduler。
"""

from __future__ import annotations

import pandas as pd

from .base import StrategyBase
from ..types import Signal


class FiveDayWatchStrategy(StrategyBase):
    """把内层策略包一层：买入后第 N 根 bar 涨幅不达标则离场（预计算形态）。"""

    name = "five_day_watch"
    stateless = True          # 关键：让引擎走信号表快速路径（O(n)）

    def __init__(self, inner: StrategyBase, days: int = 5, threshold: float = 0.0, **params):
        super().__init__(**params)
        self.inner = inner
        self.days = int(days)
        self.threshold = float(threshold)
        self.n_triggered = 0

    # ---- 委托给内层策略 ----
    def _prepare_indicators(self, df):
        return self.inner._prepare_indicators(df)

    def _evaluate(self, df: pd.DataFrame, indicators: dict) -> list[Signal]:
        buys = self.inner._evaluate(df, indicators)
        if not buys:
            return []
        self.n_triggered = 0
        dates = pd.to_datetime(df["trade_date"]).reset_index(drop=True)
        closes = pd.to_numeric(df["close"], errors="coerce").reset_index(drop=True)

        out: list[Signal] = list(buys)
        seen: set[tuple[str, object]] = set()
        for s in buys:
            if getattr(s, "kind", "") != "buy" or s.trade_date is None:
                continue
            idx = dates[dates == pd.Timestamp(s.trade_date)].index
            if len(idx) == 0:
                continue
            j = int(idx[0]) + self.days
            if j >= len(df):
                continue                      # 窗口末尾，观察哨还没到期
            px = closes.iloc[j]
            entry = float(getattr(s, "suggested_price", 0.0) or 0.0)
            if not (px == px) or entry <= 0:   # NaN / 无效价
                continue
            ret = float(px) / entry - 1.0
            if ret >= self.threshold:
                continue
            key = (s.ts_code, dates.iloc[j])
            if key in seen:
                continue
            seen.add(key)
            self.n_triggered += 1
            out.append(Signal(
                kind="sell",
                ts_code=s.ts_code,
                suggested_price=float(px),
                strategy_name=self.name,
                trade_date=dates.iloc[j].date(),
                reason=(f"五日观察哨：第 {self.days} 根 bar 收益 {ret:+.2%} "
                        f"< {self.threshold:+.0%}，离场"),
            ))
        return out
