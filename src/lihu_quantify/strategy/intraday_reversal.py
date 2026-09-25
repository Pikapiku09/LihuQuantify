"""IntradayReversal：20日日内收益反转策略（P1 首个通过三级门禁的因子策略化）。

因子来源：research/factor.py intraday_rev_20（国泰海通「20日日内收益」口径）
    因子值 = -Σ ln(close/open)（近20日累计日内收益取负 → 日内跌得越多越超卖看多）

门禁记录（docs/papers/P1_门禁3_验证报告.md）：
    门禁2 RankIC=+0.0690 ICIR=0.47 t=17.6 icDSR=0.976 单调ρ=1.00（R4 PASS）
    门禁3 样本外(2025-2026.8) RankIC=+0.0804，5/5 稳健性 gate 全过

单股时序实现说明：门禁2 为截面分位选股；单股事件驱动引擎中用
「因子相对自身过去 250 日滚动分位 ≤20%」近似（每期截面低分位 ≈ 自身历史低分位）。
持有周期：与门禁一致按 20 日兑现（目标/止损/移动止盈由风控层执行）。

铁律对齐：止损=成本-8% 或破10日线（先到先走）；单票仓位 ≤25%。
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from ..data import adjustment  # noqa: F401  (保持包引用一致性)
from ..indicators.standard import add_all_standard
from ..risk.stop_loss import StopLossManager
from ..types import Signal
from .base import StrategyBase


class IntradayReversal(StrategyBase):
    """20日日内收益反转（超卖买入，持有约20个交易日）。"""

    name = "IntradayReversal"
    stateless = True          # 信号只依赖预计算的指标列（滚动分位可预计算）

    EXCLUDED_PREFIX = ("688", "300", "301")
    MIN_LIST_DAYS = 60
    MIN_AVG_AMOUNT_20D = 1e8

    def __init__(
        self,
        lookback: int = 20,               # 日内收益累计窗口
        entry_pct: float = 0.20,          # 自身历史分位入场阈值（≤20% = 超卖）
        hist_window: int = 250,           # 分位参考的历史窗口
        max_position_pct: float = 0.25,
        stop_loss_force_pct: float = -0.08,
        **kwargs,
    ):
        super().__init__(**kwargs)
        self.lookback = lookback
        self.entry_pct = entry_pct
        self.hist_window = hist_window
        self.max_position_pct = max_position_pct
        self.stop_loss_mgr = StopLossManager(force_pct=stop_loss_force_pct)

    def _prepare_indicators(self, df: pd.DataFrame) -> dict:
        if df is None or df.empty:
            return {"df": df}
        need = {"ir20", "ir20_pct"}
        if need.issubset(df.columns):
            return {"df": df}
        d = add_all_standard(df)
        intraday = np.log(d["close"] / d["open"])
        s = intraday.rolling(self.lookback, min_periods=self.lookback // 2).sum()
        d["ir20"] = -s
        d["ir20_pct"] = d["ir20"].rolling(self.hist_window, min_periods=120).rank(pct=True)
        return {"df": d}

    def pre_filter(self, df: pd.DataFrame) -> bool:
        if df is None or len(df) < self.MIN_LIST_DAYS:
            return False
        ts_code = str(df["ts_code"].iloc[0]) if "ts_code" in df.columns else ""
        if any(ts_code.startswith(p) for p in self.EXCLUDED_PREFIX):
            return False
        return True

    def _evaluate(self, df: pd.DataFrame, indicators: dict) -> list[Signal]:
        d = indicators.get("df", df)
        if d is None or d.empty or not self.pre_filter(d):
            return []
        ts_code = str(d["ts_code"].iloc[0]) if "ts_code" in d.columns else ""

        # 基础过滤（逐 bar 滚动，无前视）
        rolling_amount = d["amount"].rolling(20).mean() if "amount" in d.columns else None
        mask = d["ir20_pct"].notna() & (d["ir20_pct"] <= self.entry_pct)
        mask &= d["close"] > 0.5
        if rolling_amount is not None:
            mask &= rolling_amount.notna() & (rolling_amount >= self.MIN_AVG_AMOUNT_20D / 1e3)
        # 上市天数精确判定（元数据注入时）
        list_date = getattr(d, "attrs", {}).get("list_date", None)
        if list_date is not None:
            try:
                ld = pd.to_datetime(list_date).date()
                tds = pd.to_datetime(d["trade_date"]).dt.date
                mask &= pd.Series([(t - ld).days >= self.MIN_LIST_DAYS if t else False for t in tds],
                                  index=d.index)
            except Exception:
                pass

        # 防重复信号：分位持续低位时每根bar都会触发 → 限频：距上一信号至少 10 个交易日
        idx = d.index[mask.fillna(False).to_numpy()]
        signals: list[Signal] = []
        last_i = None
        for i in idx:
            if last_i is not None and (d.loc[i, "trade_date"] and d.loc[last_i, "trade_date"]
                                       and (pd.Timestamp(d.loc[i, "trade_date"]) - pd.Timestamp(d.loc[last_i, "trade_date"])).days < 10):
                continue
            last_i = i
            row = d.loc[i]
            close = float(row["close"])
            ma10 = float(row.get("ma10", close))
            stop_loss = self.stop_loss_mgr.calc_stop_price(close, ma10)
            targets = [close * 1.06, close * 1.12, close * 1.18, close * 1.25]
            td = row["trade_date"]
            signals.append(Signal(
                kind="buy", ts_code=ts_code, suggested_price=close,
                stop_loss=stop_loss, take_profit=targets,
                suggested_position_pct=self.max_position_pct,
                strategy_name=self.name,
                reason=f"日内反转超卖(ir20分位={float(row['ir20_pct']):.2f})",
                trade_date=td,
            ))
        return signals
