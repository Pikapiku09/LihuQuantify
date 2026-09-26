"""策略包装：个股级波动率目标仓位（P4，2026-09-26）。

仅用于趋势类策略（CherryClaw）；反转策略与波动率目标语义冲突（高波动=机会），
scheduler 在账本策略为 intraday_reversal 时不包装。
"""
from __future__ import annotations

from ..risk.vol_target import VolTargetSizer


def wrap_vol_target(inner, vt_cfg):
    """返回带 vol_target 缩放的策略代理（不修改原策略类）。"""

    sizer = VolTargetSizer(target_vol=vt_cfg.target_vol, window=vt_cfg.window,
                           min_scale=vt_cfg.min_scale)

    class _VolTargetWrapped:
        name = f"{inner.name}+VT"
        stateless = getattr(inner, "stateless", False)

        def __getattr__(self, item):
            return getattr(inner, item)

        def _prepare_indicators(self, df):
            return inner._prepare_indicators(df)

        def _evaluate(self, df, indicators):
            sigs = inner._evaluate(df, indicators)
            return sizer.apply(sigs, indicators.get("df", df))

        def scan(self, df):
            sigs = inner.scan(df)
            return sizer.apply(sigs, df)

        def latest_signal(self, df):
            sigs = self.scan(df)
            return sigs[-1] if sigs else None

    return _VolTargetWrapped()
