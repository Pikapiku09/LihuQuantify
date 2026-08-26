"""六维诊断 + L1-L4 目标价 + 综合评分。

对应 dsh-invest-plugin prompts.js P_DEEP：
    1. 蜡烛图优先检查（最高优先级预警）
    2. 六维诊断：K线/均线/量价/技术指标/资金面/板块地位
    3. L1-L4 目标价
    4. 止损方案：-3%预警 -5%执行 -8%强制
    5. 综合评分 S/A/B/C/D
    6. 九转序列 + MACD 背离

报告基线：samples/reports/长电科技_深度分析_含Checklist闸门.md
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal, Optional

import pandas as pd

from ..indicators.candlestick import detect_candlestick_warning
from ..indicators.divergence import detect_macd_divergence
from ..indicators.standard import add_all_standard
from ..indicators.td_sequential import latest_td_state, describe_td_state
from ..risk.stop_loss import StopLossManager
from ..types import Position

Rating = Literal["S", "A", "B", "C", "D"]


@dataclass
class SixDimScore:
    """六维诊断每维评分。"""

    kline: str = "未知"           # K线形态
    moving_avg: str = "未知"      # 均线系统
    volume_price: str = "未知"    # 量价
    indicators: str = "未知"     # 技术指标 MACD/BOLL/RSI
    capital: str = "未知"         # 资金面
    sector: str = "未知"          # 板块地位

    def to_dict(self) -> dict:
        return {
            "K线": self.kline,
            "均线": self.moving_avg,
            "量价": self.volume_price,
            "指标": self.indicators,
            "资金": self.capital,
            "板块": self.sector,
        }


@dataclass
class TargetPrice:
    """L1-L4 目标价。"""

    level: str
    price: float
    probability: float = 0.0
    trigger: str = ""


@dataclass
class DeepReport:
    """深度分析报告。"""

    ts_code: str
    trade_date: Optional[object] = None
    close: float = 0.0
    # 蜡烛图预警
    candle_warnings: list[dict] = field(default_factory=list)
    # 六维诊断
    six_dim: SixDimScore = field(default_factory=SixDimScore)
    six_dim_scores: dict = field(default_factory=dict)   # 每维 0-100 分
    # L1-L4
    targets: list[TargetPrice] = field(default_factory=list)
    # 止损方案
    stop_loss_price: float = 0.0
    stop_plan: str = ""
    # 综合评分
    rating: Rating = "C"
    score: float = 0.0
    bull_reasons: list[str] = field(default_factory=list)
    bear_reasons: list[str] = field(default_factory=list)
    risks: list[str] = field(default_factory=list)
    # 九转 + 背离
    td_state: dict = field(default_factory=dict)
    td_desc: str = ""
    divergences: list[dict] = field(default_factory=list)
    # 结论
    conclusion: str = ""


class DeepDiagnose:
    """六维诊断。"""

    def __init__(self, stop_loss_mgr: Optional[StopLossManager] = None):
        self.stop_loss_mgr = stop_loss_mgr or StopLossManager()

    def run(
        self,
        ts_code: str,
        df: pd.DataFrame,
        moneyflow: Optional[pd.DataFrame] = None,
        sector_pct_chg: Optional[float] = None,
        fundamentals: Optional[dict] = None,
    ) -> DeepReport:
        """对单只股票做完整深度分析。"""
        if df is None or df.empty:
            return DeepReport(ts_code=ts_code)
        df = df.sort_values("trade_date").reset_index(drop=True)
        df_ind = add_all_standard(df)
        last = df_ind.iloc[-1]
        last_date = last.get("trade_date")
        close = float(last["close"])

        report = DeepReport(ts_code=ts_code, trade_date=last_date, close=close)

        # 1. 蜡烛图检查（最高优先级）
        report.candle_warnings = detect_candlestick_warning(df_ind)

        # 2. 六维诊断
        report.six_dim, report.six_dim_scores = self._six_dim_diagnose(
            df_ind, last, moneyflow, sector_pct_chg
        )

        # 3. L1-L4 目标价
        report.targets = self._calc_targets(df_ind, last)

        # 4. 止损方案
        ma10 = float(last.get("ma10", close))
        report.stop_loss_price = self.stop_loss_mgr.calc_stop_price(close, ma10)
        report.stop_plan = self._stop_plan(close, ma10)

        # 5. 综合评分
        report.score = self._overall_score(report.six_dim_scores, report.candle_warnings)
        report.rating = self._rating(report.score, report.candle_warnings)
        report.bull_reasons, report.bear_reasons = self._bull_bear_reasons(df_ind, last, report)
        report.risks = self._risks(df_ind, last, report)

        # 6. 九转 + MACD 背离
        state = latest_td_state(df_ind["close"])
        report.td_state = state
        report.td_desc = describe_td_state(state)
        report.divergences = detect_macd_divergence(df_ind, window=60, min_window=36)

        # 结论
        report.conclusion = self._conclusion(report)
        return report

    # ===== 六维诊断 =====

    def _six_dim_diagnose(
        self,
        df: pd.DataFrame,
        last: pd.Series,
        moneyflow: Optional[pd.DataFrame],
        sector_pct_chg: Optional[float],
    ) -> tuple[SixDimScore, dict]:
        scores: dict = {}

        # K线形态
        if last.get("is_red"):
            kline = "收红"
            scores["kline"] = 70
        else:
            kline = "收阴"
            scores["kline"] = 40
        # 大阴线减分
        if last.get("pct_chg", 0) < -5:
            kline = "大阴线"
            scores["kline"] = 20

        # 均线系统：多头排列 MA5>MA10>MA20>MA60
        ma5, ma10, ma20, ma60 = (
            last.get("ma5"), last.get("ma10"), last.get("ma20"), last.get("ma60")
        )
        if all(pd.notna([ma5, ma10, ma20, ma60])) and ma5 > ma10 > ma20 > ma60:
            ma_str = "多头排列"
            scores["moving_avg"] = 80
        elif pd.notna(ma5) and pd.notna(ma10) and ma5 > ma10:
            ma_str = "短期多头"
            scores["moving_avg"] = 60
        elif pd.notna(ma5) and pd.notna(ma10) and ma5 < ma10:
            ma_str = "短期空头"
            scores["moving_avg"] = 35
        else:
            ma_str = "未就绪"
            scores["moving_avg"] = 50

        # 量价
        vol_ratio = last.get("vol_ratio", 1.0)
        if vol_ratio > 1.5 and last.get("is_red"):
            vp = "放量上涨"
            scores["volume_price"] = 75
        elif vol_ratio > 1.5 and not last.get("is_red"):
            vp = "放量下跌"
            scores["volume_price"] = 30
        elif vol_ratio < 0.7:
            vp = "缩量"
            scores["volume_price"] = 50
        else:
            vp = "量价平衡"
            scores["volume_price"] = 55

        # 技术指标 MACD/BOLL/RSI
        dif = last.get("dif")
        dea = last.get("dea")
        rsi = last.get("rsi14")
        if pd.notna(dif) and pd.notna(dea):
            if dif > dea and dif > 0:
                ind_str = "MACD零轴上金叉"
                scores["indicators"] = 75
            elif dif > dea:
                ind_str = "MACD金叉"
                scores["indicators"] = 60
            elif dif < dea and dif < 0:
                ind_str = "MACD零轴下死叉"
                scores["indicators"] = 35
            else:
                ind_str = "MACD死叉"
                scores["indicators"] = 45
        else:
            ind_str = "MACD未就绪"
            scores["indicators"] = 50
        if pd.notna(rsi):
            if rsi > 70:
                ind_str += "/RSI超买"
                scores["indicators"] -= 10
            elif rsi < 30:
                ind_str += "/RSI超卖"
                scores["indicators"] -= 5

        # 资金面
        if moneyflow is not None and not moneyflow.empty and "net_amount" in moneyflow.columns:
            latest_flow = moneyflow["net_amount"].iloc[-1] if len(moneyflow) > 0 else 0
            if pd.notna(latest_flow):
                if latest_flow > 0:
                    cap = f"主力净流入 {latest_flow:.2f}亿"
                    scores["capital"] = 70
                else:
                    cap = f"主力净流出 {latest_flow:.2f}亿"
                    scores["capital"] = 35
            else:
                cap = "资金面数据缺失"
                scores["capital"] = 50
        else:
            cap = "资金面未提供"
            scores["capital"] = 50

        # 板块地位
        if sector_pct_chg is not None:
            if sector_pct_chg > 1:
                sec = f"板块 +{sector_pct_chg:.2f}% 领涨"
                scores["sector"] = 70
            elif sector_pct_chg < -1:
                sec = f"板块 {sector_pct_chg:.2f}% 走弱"
                scores["sector"] = 35
            else:
                sec = f"板块 {sector_pct_chg:.2f}% 平衡"
                scores["sector"] = 50
        else:
            sec = "板块未提供"
            scores["sector"] = 50

        return SixDimScore(
            kline=kline, moving_avg=ma_str, volume_price=vp,
            indicators=ind_str, capital=cap, sector=sec,
        ), scores

    def _calc_targets(self, df: pd.DataFrame, last: pd.Series) -> list[TargetPrice]:
        """L1-L4 目标价。"""
        close = float(last["close"])
        # L1: 通道上轨（MA10 上方 5% 或近期小高）
        l1 = close * 1.05
        # L2: 量度目标（+10%）
        l2 = close * 1.10
        # L3: 突破延伸（+15% 或前高）
        recent_high = df["high"].tail(20).max() if len(df) >= 20 else close
        l3 = max(close * 1.15, float(recent_high))
        # L4: 周线机会（+20%）
        l4 = close * 1.20
        return [
            TargetPrice("L1 通道上轨", l1, 0.35, "缩量企稳后收复 MA10"),
            TargetPrice("L2 量度目标", l2, 0.20, "放量站稳近期高点"),
            TargetPrice("L3 突破延伸", l3, 0.12, "有效突破前高"),
            TargetPrice("L4 周线机会", l4, 0.08, "周线收复颈线区"),
        ]

    def _stop_plan(self, close: float, ma10: float) -> str:
        warn = close * 0.97
        exec_ = close * 0.95
        force = close * 0.92
        return (
            f"-3% 预警 {warn:.2f} / -5% 执行 {exec_:.2f} / -8% 强制 {force:.2f}；"
            f"或跌破 10 日线 {ma10:.2f} 离场"
        )

    def _overall_score(self, scores: dict, warnings: list[dict]) -> float:
        weights = {
            "kline": 0.15, "moving_avg": 0.20, "volume_price": 0.15,
            "indicators": 0.20, "capital": 0.15, "sector": 0.15,
        }
        total = sum(scores.get(k, 50) * w for k, w in weights.items())
        # 蜡烛图预警扣分
        for w in warnings:
            if w.get("action") == "清仓":
                total -= 15
            elif w.get("action") == "减仓":
                total -= 8
        return max(0, min(100, total))

    def _rating(self, score: float, warnings: list[dict]) -> Rating:
        # 有清仓预警直接降级
        if any(w.get("action") == "清仓" for w in warnings):
            return "D"
        if score >= 80:
            return "S"
        if score >= 70:
            return "A"
        if score >= 55:
            return "B"
        if score >= 40:
            return "C"
        return "D"

    def _bull_bear_reasons(
        self, df: pd.DataFrame, last: pd.Series, report: DeepReport
    ) -> tuple[list[str], list[str]]:
        bull, bear = [], []
        if report.six_dim_scores.get("moving_avg", 0) >= 60:
            bull.append("均线多头排列，趋势向上")
        if report.six_dim_scores.get("indicators", 0) >= 60:
            bull.append("MACD 金叉/零轴上方")
        if report.six_dim_scores.get("capital", 0) >= 65:
            bull.append("主力资金净流入")
        if report.six_dim_scores.get("volume_price", 0) >= 70:
            bull.append("放量上涨，量价配合")
        if report.td_state.get("dir") == "buy" and report.td_state.get("exhausted"):
            bull.append("九转买入序列衰竭，底部反转信号")
        if not bull:
            bull.append("无明显做多信号")

        if report.candle_warnings:
            bear.append("蜡烛图预警：" + ",".join(w["pattern"] for w in report.candle_warnings))
        if report.six_dim_scores.get("moving_avg", 0) < 40:
            bear.append("均线空头排列，趋势向下")
        if report.six_dim_scores.get("indicators", 0) < 40:
            bear.append("MACD 死叉/零轴下方")
        if report.six_dim_scores.get("capital", 0) < 40:
            bear.append("主力资金净流出")
        if report.td_state.get("dir") == "sell" and report.td_state.get("exhausted"):
            bear.append("九转卖出序列衰竭，顶部反转风险")
        if not bear:
            bear.append("无明显做空信号")
        return bull[:3], bear[:3]

    def _risks(self, df: pd.DataFrame, last: pd.Series, report: DeepReport) -> list[str]:
        risks = []
        if report.candle_warnings:
            risks.append("蜡烛图看跌形态：" + ",".join(w["pattern"] for w in report.candle_warnings))
        if report.divergences:
            for d in report.divergences:
                risks.append(f"MACD {'顶' if d['type']=='top' else '底'}背离：{d['desc']}")
        if last.get("pct_chg", 0) < -5:
            risks.append(f"单日大跌 {last['pct_chg']:.2f}%，波动风险高")
        rsi = last.get("rsi14")
        if pd.notna(rsi) and rsi > 70:
            risks.append(f"RSI {rsi:.0f} 超买，回调风险")
        if not risks:
            risks.append("无明显风险点")
        return risks

    def _conclusion(self, report: DeepReport) -> str:
        if report.rating in ("S", "A"):
            action = "可买入"
        elif report.rating == "B":
            action = "持有/小仓位"
        elif report.rating == "C":
            action = "观望"
        else:
            action = "回避"
        td_note = f"；九转：{report.td_desc}" if report.td_desc else ""
        return f"评级 {report.rating}（{action}）{td_note}。仅供参考，不构成投资建议。"
