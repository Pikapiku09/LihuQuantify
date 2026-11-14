"""行为体检报告的组装、渲染与缓存（纯函数 + 轻量文件 IO）。

拆出本模块的原因：
    ``scripts/behavior_audit.py`` 需要连 DuckDB 取行情，**巡检进程不能碰**——
    每日巡检再读一遍 907 笔交割单 + 查几百次行情会显著拖慢流程。
    因此分工是：
        CLI 跑全量 → 写 JSON 缓存；日报只读缓存（本模块 load_cached）。

约束：
    - 本模块不连网、不连 DuckDB、不参与任何下单/风控/策略决策
    - 只读展示层，缓存缺失或损坏一律返回 None，绝不让日报失败
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import Path

from .metrics import (
    loss_add_ratio,
    peak_capture,
    pgr_plr,
    position_sizing,
    post_sell_upside,
    turnover,
)

# 告警阈值：(告警线, 方向, 中文名)
THRESHOLDS = {
    "loss_add_ratio.ratio": (0.10, ">", "浮亏加仓占买入额"),
    "position_sizing.loss_to_win_ratio": (1.10, ">", "盈亏单规模比"),
    "turnover.turnover_annual": (6.0, ">", "年化换手"),
    "peak_capture.median": (0.50, "<", "峰值捕获率(中位)"),
}


def build_report(fills: list[dict], rounds: list[dict], high_lookup,
                 capital_base: float | None = None) -> dict:
    """汇总 6 项指标 + 行情覆盖率。纯计算，无 IO。

    ``capital_base`` 传给换手率计算；不传时退回「资金余额」口径（会高估）。
    """
    codes = {r["ts_code"] for r in rounds}
    covered = 0
    if rounds:
        first, last = rounds[0]["buy_date"], rounds[-1]["sell_date"]
        for code in codes:
            if high_lookup(code, first, last) is not None:
                covered += 1
    return {
        "fills": {
            "n_fills": len(fills),
            "n_buys": sum(1 for f in fills if f["side"] == "buy"),
            "n_sells": sum(1 for f in fills if f["side"] == "sell"),
            "n_codes": len({f["ts_code"] for f in fills}),
            "first_date": str(min((f["trade_date"] for f in fills), default="")),
            "last_date": str(max((f["trade_date"] for f in fills), default="")),
        },
        "rounds": {
            "n_rounds": len(rounds),
            "total_pnl": round(sum(float(r["pnl"]) for r in rounds), 2),
            "win_rate": round(
                sum(1 for r in rounds if float(r["pnl"]) > 0) / len(rounds), 4
            ) if rounds else 0.0,
        },
        "quote_coverage": {"codes_total": len(codes), "codes_covered": covered},
        "pgr_plr": pgr_plr(rounds),
        "loss_add_ratio": loss_add_ratio(fills),
        "peak_capture": peak_capture(rounds, high_lookup),
        "post_sell_upside": post_sell_upside(rounds, high_lookup, window=20),
        "turnover": turnover(fills, capital_base),
        "position_sizing": position_sizing(rounds),
    }


def _get(report: dict, dotted: str):
    cur = report
    for part in dotted.split("."):
        if not isinstance(cur, dict) or part not in cur:
            return None
        cur = cur[part]
    return cur


def evaluate_thresholds(report: dict) -> list[dict]:
    """按 THRESHOLDS 逐项判定，返回 [{key,label,value,limit,op,hit}]。"""
    out = []
    for path, (limit, op, label) in THRESHOLDS.items():
        val = _get(report, path)
        if val is None:
            continue
        hit = val > limit if op == ">" else val < limit
        out.append({"key": path, "label": label, "value": val,
                    "limit": limit, "op": op, "hit": hit})
    return out


def _fmt_ratio(v: float | None, path: str) -> str:
    if v is None:
        return "-"
    if "ratio" in path or "median" in path or "pct" in path:
        return f"{v:.1%}"
    return f"{v:.2f}"


def format_markdown(report: dict) -> str:
    """生成行为体检 Markdown（中文、表格化）。"""
    f, r = report["fills"], report["rounds"]
    p, la, pc = report["pgr_plr"], report["loss_add_ratio"], report["peak_capture"]
    ps, pu, tu = report["position_sizing"], report["post_sell_upside"], report["turnover"]

    lines = [
        "# 行为体检报告", "",
        f"- 样本：{f['n_fills']} 笔成交（买 {f['n_buys']} / 卖 {f['n_sells']}），"
        f"{f['n_codes']} 个标的",
        f"- 区间：{f['first_date']} ~ {f['last_date']}",
        f"- FIFO 配对轮次：**{r['n_rounds']}** 笔，合计 **{r['total_pnl']:+,.0f}** 元，"
        f"胜率 {r['win_rate']:.1%}",
        f"- 行情覆盖：{report['quote_coverage']['codes_covered']}/"
        f"{report['quote_coverage']['codes_total']} 个标的（未覆盖自动跳过）",
        "",
        "## 告警判定", "",
        "| 指标 | 实测 | 告警线 | 状态 |",
        "|---|---|---|---|",
    ]
    for item in evaluate_thresholds(report):
        lines.append(
            f"| {item['label']} | {_fmt_ratio(item['value'], item['key'])} "
            f"| {item['op']} {_fmt_ratio(item['limit'], item['key'])} "
            f"| {'**告警**' if item['hit'] else '正常'} |")

    lines += [
        "", "## 1. 处置效应：持有期维度", "",
        "| 项 | 值 |", "|---|---|",
        f"| 盈利单持有均值 | {p['hold_win_mean']} 天 |",
        f"| 亏损单持有均值 | {p['hold_loss_mean']} 天 |",
        f"| 差值（盈利-亏损） | {p['hold_diff']:+} 天 |",
        f"| bootstrap 95% CI | [{p['diff_ci_low']:+.1f}, {p['diff_ci_high']:+.1f}] |",
        f"| 是否显著 | {'是' if p['significant'] else '**否（CI 跨 0）**'} |",
        f"| 盈利单均值盈亏 | {p['win_pnl_mean']:+,.0f} |",
        f"| 亏损单均值盈亏 | {p['loss_pnl_mean']:+,.0f} |",
        "",
        "## 2. 浮亏加仓", "",
        f"- 笔数：**{la['n_adds']}** / {la['n_buys']} 次买入",
        f"- 金额：**{la['add_amount']:,.0f}** 元",
        f"- 占全部买入额：**{la['ratio']:.1%}**",
        "",
        "## 3. 峰值捕获率（涨幅口径）", "",
        f"- 中位 **{pc['median']:.1%}**（均值 {pc['mean']:.1%}，"
        f"P25 {pc['p25']:.1%} / P75 {pc['p75']:.1%}）",
        f"- 捕获率 <50% 的占比：{pc['below_half_pct']:.1%}",
        f"- 持有期未上涨而跳过：{pc.get('skipped_no_rise', 0)} 轮",
        "",
        f"## 4. 卖出后走势（窗口 {pu['window']} 日）", "",
        f"- 继续上涨比例：**{pu['continued_up_pct']:.1%}**",
        f"- 平均还能涨：{pu['mean_gain_pct']:+.2%}（中位 {pu['median_gain_pct']:+.2%}）",
        "",
        "## 5. 换手与摩擦成本", "",
        f"- 年化换手：**{tu['turnover_annual']:.1f} 倍**",
        f"- 摩擦成本估算：**{tu['friction_cost_pct']:.2%}** / 年",
        f"- 分母口径：{tu.get('basis', '-')}（{tu['capital_base']:,.0f}，"
        f"跨度 {tu['span_days']} 天）",
        "",
        "## 6. 仓位规模", "",
        "| 分位 | 金额 |", "|---|---|",
        f"| P25 | {ps['p25']:,.0f} |", f"| P50 | {ps['p50']:,.0f} |",
        f"| P75 | {ps['p75']:,.0f} |", f"| P90 | {ps['p90']:,.0f} |",
        "",
        f"- 盈利单均规模 {ps['win_cost_mean']:,.0f} / 亏损单均规模 "
        f"{ps['loss_cost_mean']:,.0f} → 比 **{ps['loss_to_win_ratio']:.2f}x**",
        "",
        "---", "",
        "> 本报告纯只读审计，不参与任何信号/下单/风控决策。",
        "> 口径说明见 `docs/行为审计口径与校准_20261114.md`。",
        "",
    ]
    return "\n".join(lines)


def write_json(report: dict, path) -> Path:
    """把报告写成 JSON 缓存（供日报读取）。"""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    payload = dict(report)
    payload["_generated_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    p.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
    return p


def load_cached(path, stale_days: int = 7) -> dict | None:
    """读行为体检缓存。

    返回 None 的三种情况：文件不存在、JSON 损坏、结构不是 dict。
    读到但超过 ``stale_days`` 天时，返回体带 ``_stale=True``（日报据此标黄告警），
    而不是直接丢弃——陈旧数据仍有参考价值，但必须让读者知道。
    """
    p = Path(path)
    if not p.exists():
        return None
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict):
        return None
    mtime = None
    try:
        mtime = datetime.fromtimestamp(p.stat().st_mtime)
    except OSError:
        pass
    data["_cached_at"] = mtime.strftime("%Y-%m-%d %H:%M") if mtime else None
    data["_stale"] = bool(
        mtime is not None
        and datetime.now() - mtime > timedelta(days=max(1, int(stale_days)))
    )
    return data
