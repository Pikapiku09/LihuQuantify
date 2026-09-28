"""WeeklyBandReversal：周频波段反转策略（P7 低频研究的策略化）。

因子来源：research wk_intraday_rev（P7 门禁2）
    因子值 = -Σ ln(close/open)（近 20 交易日=4周 累计日内收益取负）
    与 IntradayReversal 同源，差异在**调仓频率**：仅每周一生成信号。

P7 门禁证据（docs/papers/P7_终版报告_低频波段.md）：
    全市场 2810 只 + 流动性过滤（4周均成交额≥5000万）
    门禁2 RankIC=+0.0728 ICIR=0.32 t=6.7 多空年化=+65.61% 单调ρ=0.90 icDSR=0.973 → PASS

设计要点：
    1. 周频调仓（仅周一信号）→ 年交易 10-15 笔（日频版 60 笔），成本拖累降 ~80%
    2. 无状态实现（靠"星期几"过滤），复用引擎全部风控（止损/止盈/T+1/涨跌停）
    3. 流动性门槛对齐 P7-R2（20日均成交额 ≥1250万 ≈ 4周5000万）
    4. 继承 P1 教训：反转天然逆势 → 建议配 block 择时（scheduler reduce 亦可用）
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from ..indicators.standard import add_all_standard
from ..risk.stop_loss import StopLossManager
from ..types import Signal
from .base import StrategyBase


class WeeklyBandReversal(StrategyBase):
    """周频波段反转（仅周一开仓，持有约 20 个交易日）。"""

    name = "WeeklyBandReversal"
    stateless = True

    EXCLUDED_PREFIX = ("688",)          # 排除科创板（20% 涨跌幅制度）；主板+创业板暂全纳
    MIN_LIST_DAYS = 60
    # P7-R2 流动性门槛：4周均成交额 ≥5000万元 → 20日均 ≥1250万元
    MIN_AVG_AMOUNT_20D = 1.25e7

    def __init__(
        self,
        lookback: int = 20,
        entry_pct: float = 0.20,
        hist_window: int = 250,
        max_position_pct: float = 0.20,
        stop_loss_force_pct: float = -0.10,       # 波段容忍度略高于日频（-8%→-10%）
        rebalance_weekday: int = 0,               # 0=周一
        use_cross_section: bool = True,           # v2：截面口径（对齐门禁2），需预注入 cs_pct 列
        cs_entry_pct: float = 0.10,               # 截面最超卖 10%
        **kwargs,
    ):
        super().__init__(**kwargs)
        self.lookback = lookback
        self.entry_pct = entry_pct
        self.hist_window = hist_window
        self.max_position_pct = max_position_pct
        self.rebalance_weekday = rebalance_weekday
        self.use_cross_section = use_cross_section
        self.cs_entry_pct = cs_entry_pct
        self.stop_loss_mgr = StopLossManager(force_pct=stop_loss_force_pct)

    def _prepare_indicators(self, df: pd.DataFrame) -> dict:
        if df is None or df.empty:
            return {"df": df}
        if {"wbr", "wbr_pct", "weekday"}.issubset(df.columns):
            return {"df": df}
        d = add_all_standard(df)
        intraday = np.log(d["close"] / d["open"])
        s = intraday.rolling(self.lookback, min_periods=self.lookback // 2).sum()
        d["wbr"] = -s
        d["wbr_pct"] = d["wbr"].rolling(self.hist_window, min_periods=120).rank(pct=True)
        d["weekday"] = pd.to_datetime(d["trade_date"]).dt.weekday
        # v2：若外层已注入截面分位列（cs_pct，按 trade_date 全市场 rank）则使用
        return {"df": d}

    def pre_filter(self, df: pd.DataFrame) -> bool:
        if df is None or len(df) < self.MIN_LIST_DAYS:
            return False
        ts_code = str(df["ts_code"].iloc[0]) if "ts_code" in df.columns else ""
        return not any(ts_code.startswith(p) for p in self.EXCLUDED_PREFIX)

    def _evaluate(self, df: pd.DataFrame, indicators: dict) -> list[Signal]:
        d = indicators.get("df", df)
        if d is None or d.empty or not self.pre_filter(d):
            return []
        ts_code = str(d["ts_code"].iloc[0]) if "ts_code" in d.columns else ""

        rolling_amount = d["amount"].rolling(20).mean() if "amount" in d.columns else None
        # v2 截面口径优先（wbr_cs_pct = 当日全市场 wbr 降序分位；越超卖分位越小）
        if self.use_cross_section and "wbr_cs_pct" in d.columns:
            mask = d["wbr_cs_pct"].notna() & (d["wbr_cs_pct"] <= self.cs_entry_pct)
        else:
            mask = d["wbr_pct"].notna() & (d["wbr_pct"] <= self.entry_pct)
        mask &= d["close"] > 0.5
        # ★ 周频核心：仅调仓日（周一）产生信号
        mask &= d["weekday"] == self.rebalance_weekday
        if rolling_amount is not None:
            mask &= rolling_amount.notna() & (rolling_amount >= self.MIN_AVG_AMOUNT_20D / 1e3)

        idx = d.index[mask.fillna(False).to_numpy()]
        signals: list[Signal] = []
        for i in idx:
            row = d.loc[i]
            close = float(row["close"])
            ma10 = float(row.get("ma10", close))
            stop_loss = self.stop_loss_mgr.calc_stop_price(close, ma10)
            targets = [close * 1.08, close * 1.16, close * 1.25, close * 1.35]   # 波段目标位放宽
            signals.append(Signal(
                kind="buy", ts_code=ts_code, suggested_price=close,
                stop_loss=stop_loss, take_profit=targets,
                suggested_position_pct=self.max_position_pct,
                strategy_name=self.name,
                reason=f"周频波段超卖(wbr分位={float(row['wbr_pct']):.2f})",
                trade_date=row["trade_date"],
            ))
        return signals
