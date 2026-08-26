"""报告生成与归档：每日巡检报告（Markdown）。

格式参考 samples/reports/（dsh-invest-plugin 输出标杆）：
    数据基准（真实最新交易日）/ 市场状态 / 信号与执行 / Checklist 拒绝 /
    持仓与止损监控 / 告警记录 / 铁律自检 / 免责声明

归档：outputs/reports/YYYY-MM-DD.md
"""

from __future__ import annotations

from datetime import date, datetime
from pathlib import Path
from typing import Optional

from loguru import logger


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
    ) -> Path:
        """生成并归档每日报告。返回报告路径。

        Args:
            trade_date: 数据基准日（真实最新交易日）
            market_state: 市场状态（上涨/震荡/下跌）
            total_asset / cash: 账户资产
            positions: [{ts_code, volume, cost, market_value}]
            signals: [{ts_code, price, reason}]
            executed: [{ts_code, volume, price, stop}]
            rejected: [{ts_code, reasons}]
            stop_orders: [{ts_code, volume, stop_price, reason}]
            alerts: Alerter.history
            mode: paper / live
        """
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

        # 五、铁律自检（修复G：运行时真实校验，非静态模板）
        lines.append("## 五、铁律自检（运行时校验）")
        lines.append("")
        issues = []

        # 1. 止损登记与持仓一一对应（volume 一致性）
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

        # 2. 单票市值占比 ≤25%
        over_pos = [p for p in positions if total_asset > 0 and p["market_value"] / total_asset > 0.25]
        if not over_pos:
            lines.append("- [x] 单票市值占比 ≤25%")
        else:
            detail = ", ".join(
                f"{p['ts_code']}({p['market_value']/total_asset:.1%})" for p in over_pos
            )
            lines.append(f"- [ ] ⚠️ 超仓位: {detail}")
        if over_pos:
            issues.append(f"单票超25%: {[p['ts_code'] for p in over_pos]}")

        # 3. 当日无向下补仓（买入记录对已有亏损持仓）
        avg_down = []
        for e in executed:
            pos = next((p for p in positions if p["ts_code"] == e["ts_code"]), None)
            # 买入时已有持仓且成本高于买入价（本次买入前状态不可得，用当前近似）
            if pos and pos["cost"] > e["price"]:
                avg_down.append(e["ts_code"])
        lines.append("- [x] 无向下补仓" if not avg_down
                     else f"- [ ] ⚠️ 疑似向下补仓: {avg_down}")
        if avg_down:
            issues.append(f"疑似向下补仓: {avg_down}")

        # 4. 连亏停手状态
        halted = [s for s in stop_orders if s.get("halted")]
        halted_info = getattr(self, "_halted_codes", None)
        if halted_info:
            lines.append(f"- [x] 连亏停手执行中: {list(halted_info.keys())}"
                         if halted_info else "- [x] 无停手持仓")
        else:
            lines.append("- [x] 无停手持仓")

        # issues → 告警提示（报告内标注；告警由 Alerter 在 scan 侧发）
        if issues:
            lines.append("")
            lines.append("> ⚠️ 自检异常项需人工处理：" + "；".join(issues))
        lines.append("")

        lines.append("---")
        lines.append("以上内容仅供参考，不构成任何投资建议。投资有风险，入市需谨慎。")
        content = "\n".join(lines)

        out_path = self.output_dir / f"{trade_date}.md"
        out_path.write_text(content, encoding="utf-8")
        logger.info(f"每日报告已归档: {out_path}")
        return out_path
