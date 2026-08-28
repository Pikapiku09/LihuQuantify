"""报告生成与归档：每日巡检报告（Markdown）。

第六轮清单1：rich 版报告（与邮件日报同源数据，scheduler._build_daily_summary）：
    一、账户总览（总资产/现金/持仓市值/今日盈亏/累计盈亏）
    二、当前持仓（代码/名称/持股/成本/现价/市值/浮动盈亏/盈亏%/占比/止损线）
    三、今日操作（买入成交/卖出记录含实现盈亏与原因/被拒信号）
    四、盈亏分析（今日已实现/浮动/累计/持仓盈亏分布）
    五、市场与风险（市场状态/过滤模式/待执行止损/停手票/当日告警）
    六、铁律自检（运行时校验，保留修复G 逻辑）
    七、资金利用（第八轮：止损回笼/同轮再投资/闲置现金/守卫与 top-N）
传 rich=None 时回退旧薄版格式（兼容旧调用/测试）。

归档：outputs/reports/YYYY-MM-DD.md
"""

from __future__ import annotations

from datetime import date, datetime
from pathlib import Path
from typing import Optional

from loguru import logger


def _fmt_money(v) -> str:
    if v is None:
        return "-"
    return f"{v:+,.0f}" if v < 0 else f"{v:,.0f}"


def _fmt_pnl(v) -> str:
    """盈亏专用：正数带 +，负数带 -，零为 0。"""
    if v is None:
        return "-"
    return f"{v:+,.0f}"


def _fmt_pct(v, sign: bool = True) -> str:
    if v is None:
        return "-"
    return f"{v:+.2%}" if sign else f"{v:.2%}"


class ReportGenerator:
    """每日巡检报告。"""

    def __init__(self, output_dir: Path | str = "outputs/reports"):
        self.output_dir = Path(output_dir)
        self.output_dir.mkdir(parents=True, exist_ok=True)

    def daily_report(
        self,
        trade_date: date,
        market_state: str,
        total_asset: float,
        cash: float,
        positions: list[dict],
        signals: list[dict],
        executed: list[dict],
        rejected: list[dict],
        stop_orders: list[dict],
        alerts: Optional[list[dict]] = None,
        mode: str = "paper",
        rich: Optional[dict] = None,
    ) -> Path:
        """生成并归档每日报告。返回报告路径。

        Args:
            trade_date: 数据基准日（真实最新交易日）
            market_state: 市场状态（上涨/震荡/下跌）
            total_asset / cash: 账户资产（交易后口径）
            positions: [{ts_code, volume, cost, market_value}]
            signals: [{ts_code, price, reason}]
            executed: [{ts_code, volume, price, stop}]
            rejected: [{ts_code, reasons}]
            stop_orders: [{ts_code, volume, stop_price, reason}]
            alerts: Alerter.history
            mode: paper / live
            rich: scheduler._build_daily_summary 的 rich 数据（第六轮清单1；
                提供时渲染五模块 rich 版，None 回退旧薄版）
        """
        if rich is not None:
            return self._daily_report_rich(
                trade_date=trade_date, market_state=market_state,
                total_asset=total_asset, cash=cash, positions=positions,
                executed=executed, stop_orders=stop_orders, mode=mode, rich=rich,
            )
        return self._daily_report_legacy(
            trade_date, market_state, total_asset, cash, positions, signals,
            executed, rejected, stop_orders, alerts, mode,
        )

    # ===== rich 版（第六轮清单1） =====

    def _daily_report_rich(
        self,
        trade_date: date,
        market_state: str,
        total_asset: float,
        cash: float,
        positions: list[dict],
        executed: list[dict],
        stop_orders: list[dict],
        mode: str,
        rich: dict,
    ) -> Path:
        """rich 版报告：与邮件日报（monitor/daily_report.py）字段同源。"""
        now = datetime.now().strftime("%H:%M:%S")
        lines: list[str] = []
        lines.append(f"# 每日巡检报告 {trade_date}")
        lines.append("")
        lines.append(f"- 模式：{'模拟盘' if mode == 'paper' else '实盘'}")
        lines.append(f"- 生成时间：{now}")
        lines.append(f"- 数据基准：真实最新交易日 {trade_date}")
        lines.append(f"- 市场状态：**{market_state}**")
        lines.append("")

        # ---- 一、账户总览 ----
        init_cap = rich.get("init_capital") or total_asset
        prev = rich.get("prev_total_asset")
        cum_pnl = total_asset - init_cap
        lines.append("## 一、账户总览")
        lines.append("")
        lines.append("| 指标 | 数值 |")
        lines.append("|---|---|")
        lines.append(f"| 总资产 | {total_asset:,.0f} |")
        lines.append(f"| 可用现金 | {cash:,.0f} |")
        lines.append(f"| 持仓市值 | {total_asset - cash:,.0f} |")
        if prev is not None and prev > 0:
            day_pnl = total_asset - prev
            lines.append(f"| 今日盈亏 | {_fmt_pnl(day_pnl)}（{_fmt_pct(day_pnl / prev)}） |")
        lines.append(f"| 累计盈亏 | {_fmt_pnl(cum_pnl)}（{_fmt_pct(cum_pnl / init_cap if init_cap else 0)}） |")
        lines.append("")

        # ---- 二、当前持仓 ----
        rich_positions = rich.get("positions") or []
        lines.append("## 二、当前持仓")
        lines.append("")
        if rich_positions:
            lines.append("| 代码 | 名称 | 持股 | 成本 | 现价 | 市值 | 浮动盈亏 | 盈亏% | 占比 | 止损线 |")
            lines.append("|---|---|---|---|---|---|---|---|---|---|")
            for p in rich_positions:
                stop = p.get("stop_price")
                lines.append(
                    f"| {p['ts_code']} | {p.get('name') or '-'} | {p.get('volume', 0):,} | "
                    f"{p.get('cost', 0):.2f} | {p.get('price', 0):.2f} | "
                    f"{_fmt_money(p.get('market_value', 0))} | "
                    f"{_fmt_pnl(p.get('float_pnl', 0))} | "
                    f"{_fmt_pct(p.get('float_pnl_pct', 0))} | "
                    f"{_fmt_pct(p.get('weight', 0), sign=False)} | "
                    f"{stop:.2f} |" if stop else
                    f"| {p['ts_code']} | {p.get('name') or '-'} | {p.get('volume', 0):,} | "
                    f"{p.get('cost', 0):.2f} | {p.get('price', 0):.2f} | "
                    f"{_fmt_money(p.get('market_value', 0))} | "
                    f"{_fmt_pnl(p.get('float_pnl', 0))} | "
                    f"{_fmt_pct(p.get('float_pnl_pct', 0))} | "
                    f"{_fmt_pct(p.get('weight', 0), sign=False)} | 未登记 ⚠️ |"
                )
        else:
            lines.append("空仓。")
        lines.append("")

        # ---- 三、今日操作 ----
        sells_today = rich.get("sells_today") or []
        rejected = rich.get("rejected") or []
        lines.append("## 三、今日操作")
        lines.append("")
        if executed:
            lines.append(f"### 买入成交（{len(executed)} 笔）")
            lines.append("")
            lines.append("| 代码 | 名称 | 价格 | 股数 | 金额 | 止损线 |")
            lines.append("|---|---|---|---|---|---|")
            for e in executed:
                lines.append(
                    f"| {e['ts_code']} | {e.get('name') or '-'} | {e.get('price', 0):.2f} | "
                    f"{e.get('volume', 0):,} | {_fmt_money(e.get('price', 0) * e.get('volume', 0))} | "
                    f"{e.get('stop', 0):.2f} |"
                )
            lines.append("")
        else:
            lines.append("今日无买入。")
            lines.append("")
        if sells_today:
            lines.append(f"### 卖出记录（{len(sells_today)} 笔）")
            lines.append("")
            lines.append("| 代码 | 名称 | 成交价 | 股数 | 金额 | 实现盈亏 | 原因 |")
            lines.append("|---|---|---|---|---|---|---|")
            for sl in sells_today:
                lines.append(
                    f"| {sl['ts_code']} | {sl.get('name') or '-'} | {sl.get('price', 0):.2f} | "
                    f"{sl.get('volume', 0):,} | {_fmt_money(sl.get('price', 0) * sl.get('volume', 0))} | "
                    f"{_fmt_pnl(sl.get('pnl', 0))} | {sl.get('reason', '-')} |"
                )
            lines.append("")
        else:
            lines.append("今日无卖出。")
            lines.append("")
        if rejected:
            lines.append(f"### 被拒信号（{len(rejected)} 个，风控拦截）")
            lines.append("")
            for rj in rejected:
                price = rj.get("price")
                price_note = f"，参考价 {price:.2f}" if price else ""
                lines.append(f"- **{rj['ts_code']}**（{rj.get('name') or '-'}{price_note}）："
                             f"{rj.get('reasons', '-')}")
            lines.append("")
        else:
            lines.append("今日无被拒信号。")
            lines.append("")

        # ---- 四、盈亏分析 ----
        realized = sum(s.get("pnl", 0) or 0 for s in sells_today)
        floating = sum(p.get("float_pnl", 0) or 0 for p in rich_positions)
        lines.append("## 四、盈亏分析")
        lines.append("")
        lines.append("| 指标 | 数值 |")
        lines.append("|---|---|")
        lines.append(f"| 今日已实现盈亏 | {_fmt_pnl(realized)}（{len(sells_today)} 笔卖出） |")
        lines.append(f"| 当前浮动盈亏 | {_fmt_pnl(floating)}（{len(rich_positions)} 只持仓） |")
        if prev is not None and prev > 0:
            lines.append(f"| 今日总盈亏 | {_fmt_pnl(total_asset - prev)} |")
        lines.append(f"| 累计盈亏 | {_fmt_pnl(cum_pnl)} |")
        if rich_positions:
            winners = sum(1 for p in rich_positions if (p.get("float_pnl", 0) or 0) > 0)
            losers = sum(1 for p in rich_positions if (p.get("float_pnl", 0) or 0) < 0)
            lines.append(f"| 持仓盈亏分布 | {winners} 盈 / {losers} 亏 / "
                         f"{len(rich_positions) - winners - losers} 平 |")
        lines.append("")

        # ---- 五、市场与风险提示 ----
        scale = rich.get("entry_scale", 1.0)
        scale = 1.0 if scale is None else scale
        if scale == 0.0:
            filter_note = "非上涨段，市场过滤生效：今日禁止开新仓"
        elif scale == 0.5:
            filter_note = "非上涨段，市场过滤生效：今日新仓仓位减半"
        else:
            filter_note = "上涨段，允许正常开仓"
        lines.append("## 五、市场与风险提示")
        lines.append("")
        lines.append(f"- 市场状态：**{market_state}**（{filter_note}；扫描信号 {rich.get('signals', 0)} 个）")
        pending = rich.get("pending_stops") or []
        if pending:
            lines.append("")
            lines.append(f"### 待执行止损（{len(pending)} 个，次日开盘价执行）")
            lines.append("")
            lines.append("| 代码 | 名称 | 股数 | 触发线 | 原因 |")
            lines.append("|---|---|---|---|---|")
            for p in pending:
                lines.append(
                    f"| {p['ts_code']} | {p.get('name') or '-'} | {p.get('volume', 0):,} | "
                    f"{p.get('stop_price', 0):.2f} | {p.get('reason', '-')} |"
                )
        halted = rich.get("halted_codes") or {}
        if halted:
            items = "、".join(f"{c}（至 {u}）" for c, u in halted.items())
            lines.append(f"- ⛔ 连亏停手（{len(halted)} 只）：{items}")
        day_alerts = rich.get("alerts") or []
        if day_alerts:
            lines.append("")
            lines.append(f"### 当日告警（{len(day_alerts)} 条）")
            lines.append("")
            for a in day_alerts:
                tag = {"error": "ERROR", "warn": "WARN"}.get(a.get("level", "info"), "INFO")
                lines.append(f"- [{tag}] {a.get('title', '')}"
                             + (f" | {a.get('detail', '')}" if a.get("detail") else ""))
        lines.append("")

        # ---- 六、铁律自检（运行时校验，修复G 保留） ----
        lines.extend(self._iron_rules_check(
            positions, stop_orders, executed, total_asset,
        ))

        # ---- 七、资金利用（需求3/第八轮：先卖后买 + 守卫/top-N 透视） ----
        cap = rich.get("capital") or {}
        if cap:
            lines.append("## 七、资金利用")
            lines.append("")
            lines.append("| 指标 | 数值 |")
            lines.append("|---|---|")
            lines.append(f"| 今日止损回笼 | {_fmt_money(cap.get('released', 0))} |")
            lines.append(f"| 同轮再投资 | {_fmt_money(cap.get('reinvested', 0))} |")
            lines.append(f"| 期末闲置现金 | {_fmt_money(cap.get('idle_cash', 0))} |")
            lines.append(f"| 常规单笔预算 | {_fmt_money(cap.get('budget', 0))} |")
            if cap.get("guard_skipped"):
                lines.append("| 资金守卫 | ⛔ 现金低于阈值，本轮未尝试买入 |")
            elif cap.get("topn_used"):
                lines.append(f"| Top-N 筛选 | 保留 {cap['topn_used']} 个"
                             f"（{cap.get('topn_skipped', 0)} 个未尝试） |")
            else:
                lines.append("| 资金守卫 | 未触发 |")
            lines.append("")
            if cap.get("idle_warn"):
                lines.append(f"- ⚠️ 闲置现金 {_fmt_money(cap.get('idle_cash', 0))} 超过 "
                             f"2 倍单笔预算（{_fmt_money(cap.get('budget', 0))}）："
                             f"资金利用效率偏低，关注次日买入机会")
            lines.append("")

        # ---- 八、AI 收盘总结（第八轮清单：纯展示层；None/空 → 整节省略） ----
        ai_text = rich.get("ai_summary")
        if ai_text:
            lines.append("## 八、AI 收盘总结")
            lines.append("")
            lines.append(ai_text)
            lines.append("")

        lines.append("---")
        lines.append("以上内容仅供参考，不构成任何投资建议。投资有风险，入市需谨慎。")
        content = "\n".join(lines)
        out_path = self.output_dir / f"{trade_date}.md"
        out_path.write_text(content, encoding="utf-8")
        logger.info(f"每日报告已归档: {out_path}")
        return out_path

    def _iron_rules_check(
        self,
        positions: list[dict],
        stop_orders: list[dict],
        executed: list[dict],
        total_asset: float,
        section_no: str = "六",
    ) -> list[str]:
        """铁律自检（运行时校验，修复G；新旧两版报告共用）。"""
        lines = [f"## {section_no}、铁律自检（运行时校验）", ""]
        issues = []
        stop_map = {s["ts_code"]: s for s in stop_orders}
        missing_stop = [p for p in positions if p["ts_code"] not in stop_map]
        vol_mismatch = [
            p for p in positions
            if p["ts_code"] in stop_map
            and stop_map[p["ts_code"]].get("volume") is not None
            and stop_map[p["ts_code"]]["volume"] != p["volume"]
        ]
        lines.append("- [x] 持仓止损登记完整（一一对应）" if not missing_stop and not vol_mismatch
                     else f"- [ ] ⚠️ 止损登记缺失 {len(missing_stop)} 个 / 数量不符 {len(vol_mismatch)} 个，需 --rebuild")
        if missing_stop:
            issues.append(f"止损登记缺失: {[p['ts_code'] for p in missing_stop]}")

        over_pos = [p for p in positions if total_asset > 0 and p["market_value"] / total_asset > 0.25]
        if not over_pos:
            lines.append("- [x] 单票市值占比 ≤25%")
        else:
            detail = ", ".join(f"{p['ts_code']}({p['market_value']/total_asset:.1%})" for p in over_pos)
            lines.append(f"- [ ] ⚠️ 超仓位: {detail}")
        if over_pos:
            issues.append(f"单票超25%: {[p['ts_code'] for p in over_pos]}")

        avg_down = []
        for e in executed:
            pos = next((p for p in positions if p["ts_code"] == e["ts_code"]), None)
            if pos and pos["cost"] > e["price"]:
                avg_down.append(e["ts_code"])
        lines.append("- [x] 无向下补仓" if not avg_down
                     else f"- [ ] ⚠️ 疑似向下补仓: {avg_down}")
        if avg_down:
            issues.append(f"疑似向下补仓: {avg_down}")

        halted_info = getattr(self, "_halted_codes", None)
        if halted_info:
            lines.append(f"- [x] 连亏停手执行中: {list(halted_info.keys())}")
        else:
            lines.append("- [x] 无停手持仓")

        if issues:
            lines.append("")
            lines.append("> ⚠️ 自检异常项需人工处理：" + "；".join(issues))
        lines.append("")
        return lines

    # ===== 旧薄版（rich=None 兼容路径） =====

    def _daily_report_legacy(
        self,
        trade_date: date,
        market_state: str,
        total_asset: float,
        cash: float,
        positions: list[dict],
        signals: list[dict],
        executed: list[dict],
        rejected: list[dict],
        stop_orders: list[dict],
        alerts: Optional[list[dict]],
        mode: str,
    ) -> Path:
        now = datetime.now().strftime("%H:%M:%S")
        lines: list[str] = []
        lines.append(f"# 每日巡检报告 {trade_date}")
        lines.append("")
        lines.append(f"- 模式：{'模拟盘' if mode == 'paper' else '实盘'}")
        lines.append(f"- 生成时间：{now}")
        lines.append(f"- 数据基准：真实最新交易日 {trade_date}")
        lines.append(f"- 市场状态：**{market_state}**"
                     + ("（过滤生效：非上涨段不开新仓）" if market_state != "上涨" else "（允许开新仓）"))
        lines.append("")

        # 一、账户
        lines.append("## 一、账户概览")
        lines.append("")
        lines.append(f"| 指标 | 数值 |")
        lines.append(f"|---|---|")
        lines.append(f"| 总资产 | {total_asset:,.0f} |")
        lines.append(f"| 可用现金 | {cash:,.0f} |")
        lines.append(f"| 持仓市值 | {total_asset - cash:,.0f} |")
        lines.append("")

        # 二、持仓与止损监控
        lines.append("## 二、持仓与止损监控")
        lines.append("")
        if positions:
            lines.append("| 代码 | 持股 | 成本 | 市值 | 止损线 | 止损状态 |")
            lines.append("|---|---|---|---|---|---|")
            stop_map = {s["ts_code"]: s for s in stop_orders}
            for p in positions:
                stop = stop_map.get(p["ts_code"])
                if stop:
                    lines.append(
                        f"| {p['ts_code']} | {p['volume']} | {p['cost']:.2f} | "
                        f"{p['market_value']:,.0f} | {stop['stop_price']:.2f} | "
                        f"{'已触发' if stop.get('triggered') else '监控中'} |"
                    )
                else:
                    lines.append(
                        f"| {p['ts_code']} | {p['volume']} | {p['cost']:.2f} | "
                        f"{p['market_value']:,.0f} | 未登记 | ⚠️ 需重建 |"
                    )
        else:
            lines.append("空仓。")
        lines.append("")

        # 三、信号与执行
        lines.append("## 三、信号与执行")
        lines.append("")
        lines.append(f"扫描信号：{len(signals)} 个；执行买入：{len(executed)} 个；风控拦截：{len(rejected)} 个。")
        lines.append("")
        if executed:
            lines.append("### 已执行（买入+止损同时挂出）")
            lines.append("")
            lines.append("| 代码 | 股数 | 价格 | 止损线 |")
            lines.append("|---|---|---|---|")
            for e in executed:
                lines.append(f"| {e['ts_code']} | {e['volume']} | {e['price']:.2f} | {e['stop']:.2f} |")
            lines.append("")
        if rejected:
            lines.append("### Checklist 拒绝（铁律拦截）")
            lines.append("")
            for rj in rejected:
                lines.append(f"- **{rj['ts_code']}**：{rj['reasons']}")
            lines.append("")
        if signals and not executed and not rejected:
            lines.append("信号因市场过滤未提交（非上涨段）。")
            lines.append("")

        # 四、告警记录
        lines.append("## 四、告警记录")
        lines.append("")
        if alerts:
            for a in alerts:
                lines.append(f"- [{a['level']}] {a['title']}" + (f" | {a['detail']}" if a['detail'] else ""))
        else:
            lines.append("无告警。")
        lines.append("")

        # 五、铁律自检（修复G：运行时真实校验；第六轮抽取为共用方法）
        lines.extend(self._iron_rules_check(
            positions, stop_orders, executed, total_asset, section_no="五",
        ))

        lines.append("---")
        lines.append("以上内容仅供参考，不构成任何投资建议。投资有风险，入市需谨慎。")
        content = "\n".join(lines)

        out_path = self.output_dir / f"{trade_date}.md"
        out_path.write_text(content, encoding="utf-8")
        logger.info(f"每日报告已归档: {out_path}")
        return out_path
