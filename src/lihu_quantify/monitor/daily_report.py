"""每日综合日报邮件：数据聚合 + HTML 渲染。

第五轮（邮件通知优化）：
    - 旧机制问题：WARN 级告警（风控拦截/止损触发）即时发邮件 → 每日几十封碎片邮件；
      摘要邮件仅 6 行纯文本（基准日/市场状态/信号数…），信息价值低。
    - 新机制：每交易日仅一封综合简报（Alerter 仅 ERROR 系统故障即时通知），
      内容覆盖账户总览 / 当前持仓 / 今日操作（买入·卖出·拒绝）/ 盈亏分析 / 风险提示。

数据来源（DailyScanner._scan_impl 组装的 summary 扩展字段）：
    trade_date / market_state / signals / entry_scale
    total_asset / cash / init_capital / prev_total_asset
    positions: [{ts_code,name,volume,cost,price,market_value,stop_price,
                 float_pnl,float_pnl_pct,weight}]
    executed:  [{ts_code,name,volume,price,stop}]          今日买入
    sells_today: [{ts_code,name,price,volume,pnl,reason}]  今日卖出（含止损执行）
    rejected:  [{ts_code,name,price,reasons}]              今日被拒信号
    pending_stops: [{ts_code,name,volume,stop_price,reason}] 收盘登记、次日开盘执行
    halted_codes: {ts_code: until_str}
    alerts: [{level,title,detail}]
"""

from __future__ import annotations

import html as _html
from typing import Optional

# A 股配色习惯：红涨绿跌
_COLOR_PROFIT = "#dc2626"
_COLOR_LOSS = "#16a34a"
_COLOR_FLAT = "#6b7280"

_TH = ('padding:6px 8px;border:1px solid #e2e8f0;background:#f1f5f9;'
       'text-align:left;font-weight:600;white-space:nowrap;')
_TD = 'padding:6px 8px;border:1px solid #e2e8f0;'
_TD_NUM = 'padding:6px 8px;border:1px solid #e2e8f0;text-align:right;white-space:nowrap;'
_H2 = ('font-size:15px;margin:22px 0 8px;padding-left:8px;'
       'border-left:4px solid #3b82f6;color:#111827;')
_TABLE = 'width:100%;border-collapse:collapse;font-size:13px;color:#1f2937;'


def realized_pnl_for_sell(trades: list[dict], sell: dict) -> float:
    """单笔卖出的已实现盈亏（扣双边费用）。

    口径与 PaperBroker._on_sell_halt_check 一致：
        pnl = (卖出价 - 买入价) * 股数 - 卖出费用 - 买入佣金按股数分摊
    买入价取该票卖出日之前（含当日）最近一笔买入。
    """
    def _key(t) -> str:
        return str(t.get("date", ""))[:10]

    sell_date = _key(sell)
    prior_buys = [
        b for b in trades
        if b.get("ts_code") == sell.get("ts_code")
        and b.get("side") == "buy"
        and _key(b) <= sell_date
    ]
    if not prior_buys:
        return 0.0
    last_buy = prior_buys[-1]
    sell_fees = sell.get("commission", 0) + sell.get("stamp_tax", 0)
    buy_fee_share = last_buy.get("commission", 0) * sell["volume"] / max(1, last_buy["volume"])
    return (sell["price"] - last_buy["price"]) * sell["volume"] - sell_fees - buy_fee_share


# ===== 格式化工具 =====

def _money(v: Optional[float], sign: bool = False) -> str:
    if v is None:
        return "-"
    s = f"{v:+,.0f}" if sign else f"{v:,.0f}"
    return s


def _pct(v: Optional[float], sign: bool = True) -> str:
    if v is None:
        return "-"
    return f"{v:+.2%}" if sign else f"{v:.2%}"


def _pnl_span(v: float, text: str) -> str:
    color = _COLOR_PROFIT if v > 0 else (_COLOR_LOSS if v < 0 else _COLOR_FLAT)
    return f'<span style="color:{color};font-weight:600;">{text}</span>'


def _th(cols: list[str]) -> str:
    return "<tr>" + "".join(f"<th style='{_TH}'>{c}</th>" for c in cols) + "</tr>"


def _td(v, num: bool = False) -> str:
    style = _TD_NUM if num else _TD
    return f"<td style='{style}'>{v}</td>"


def _section(title: str, body: str) -> str:
    return f"<h2 style='{_H2}'>{title}</h2>{body}"


def _table(headers: list[str], rows: list[list[str]]) -> str:
    if not rows:
        return ""
    trs = _th(headers) + "".join(
        "<tr>" + "".join(cells) + "</tr>" for cells in rows
    )
    return f"<table style='{_TABLE}'>{trs}</table>"


def _empty(text: str) -> str:
    return f"<p style='margin:4px 0;color:#6b7280;font-size:13px;'>{text}</p>"


def _esc(s) -> str:
    """修复5（第五轮清单）：转义外部来源字符串（股票名/拒绝原因/告警详情/
    报告路径），防 HTML 注入与表格结构破坏。"""
    return _html.escape(str(s)) if s is not None else ""


def _disp_name(item: dict) -> str:
    """条目显示名：优先条目自带 name（scheduler 已填充），缺省用代码前段。"""
    nm = item.get("name") or ""
    return _esc(nm) if nm else str(item.get("ts_code", "")).split(".")[0]


# ===== 各模块渲染 =====

def _render_overview(d: dict) -> str:
    total = d.get("total_asset", 0) or 0
    cash = d.get("cash", 0) or 0
    init_cap = d.get("init_capital") or total
    prev = d.get("prev_total_asset")
    mv = total - cash
    cum_pnl = total - init_cap
    cum_pct = cum_pnl / init_cap if init_cap else 0

    rows = [
        ("总资产", _money(total)),
        ("可用现金", _money(cash)),
        ("持仓市值", _money(mv)),
        ("累计盈亏", _pnl_span(cum_pnl, f"{_money(cum_pnl, sign=True)}（{_pct(cum_pct)}）")),
    ]
    if prev is not None and prev > 0:
        day_pnl = total - prev
        day_pct = day_pnl / prev
        rows.insert(3, ("今日盈亏",
                        _pnl_span(day_pnl, f"{_money(day_pnl, sign=True)}（{_pct(day_pct)}）")))
    body = _table(
        ["指标", "数值"],
        [[_td(k), _td(v, num=True)] for k, v in rows],
    )
    return _section("一、账户总览", body)


def _render_positions(d: dict) -> str:
    positions = d.get("positions") or []
    if not positions:
        return _section("二、当前持仓", _empty("当前空仓。"))
    total = d.get("total_asset", 0) or 0
    rows = []
    for p in positions:
        pnl = p.get("float_pnl", 0) or 0
        pct = p.get("float_pnl_pct", 0) or 0
        stop = p.get("stop_price")
        rows.append([
            _td(p.get("ts_code", "-")),
            _td(_disp_name(p)),
            _td(f"{p.get('volume', 0):,}", num=True),
            _td(f"{p.get('cost', 0):.2f}", num=True),
            _td(f"{p.get('price', 0):.2f}", num=True),
            _td(_money(p.get("market_value", 0)), num=True),
            _td(_pnl_span(pnl, _money(pnl, sign=True)), num=True),
            _td(_pnl_span(pnl, _pct(pct)), num=True),
            _td(_pct(p.get("weight", 0) or 0, sign=False), num=True),
            _td(f"{stop:.2f}" if stop else '<span style="color:#b45309;">未登记</span>', num=True),
        ])
    body = _table(
        ["代码", "名称", "持股", "成本", "现价", "市值", "浮动盈亏", "盈亏%", "占比", "止损线"],
        rows,
    )
    return _section("二、当前持仓", body)


def _render_operations(d: dict) -> str:
    executed = d.get("executed") or []
    sells = d.get("sells_today") or []
    rejected = d.get("rejected") or []
    parts: list[str] = []

    # 3.1 买入成交
    if executed:
        rows = []
        for e in executed:
            rows.append([
                _td(e.get("ts_code", "-")),
                _td(_disp_name(e)),
                _td(f"{e.get('price', 0):.2f}", num=True),
                _td(f"{e.get('volume', 0):,}", num=True),
                _td(_money(e.get("price", 0) * e.get("volume", 0)), num=True),
                _td(f"{e.get('stop', 0):.2f}", num=True),
            ])
        parts.append("<p style='margin:10px 0 4px;font-weight:600;font-size:13px;'>"
                     f"买入成交（{len(executed)} 笔）</p>")
        parts.append(_table(["代码", "名称", "价格", "股数", "金额", "止损线"], rows))
    else:
        parts.append("<p style='margin:10px 0 4px;font-weight:600;font-size:13px;'>买入成交</p>"
                     + _empty("今日无买入。"))

    # 3.2 卖出记录（含止损执行）
    if sells:
        rows = []
        for s in sells:
            pnl = s.get("pnl", 0) or 0
            rows.append([
                _td(s.get("ts_code", "-")),
                _td(_disp_name(s)),
                _td(f"{s.get('price', 0):.2f}", num=True),
                _td(f"{s.get('volume', 0):,}", num=True),
                _td(_money(s.get("price", 0) * s.get("volume", 0)), num=True),
                _td(_pnl_span(pnl, _money(pnl, sign=True)), num=True),
                _td(_esc(s.get("reason", "止损/离场"))),
            ])
        parts.append("<p style='margin:10px 0 4px;font-weight:600;font-size:13px;'>"
                     f"卖出记录（{len(sells)} 笔）</p>")
        parts.append(_table(
            ["代码", "名称", "成交价", "股数", "金额", "实现盈亏", "原因"], rows))
    else:
        parts.append("<p style='margin:10px 0 4px;font-weight:600;font-size:13px;'>卖出记录</p>"
                     + _empty("今日无卖出。"))

    # 3.3 被拒信号
    if rejected:
        rows = []
        for rj in rejected:
            price = rj.get("price")
            rows.append([
                _td(rj.get("ts_code", "-")),
                _td(_disp_name(rj)),
                _td(f"{price:.2f}" if price else "-", num=True),
                _td(_esc(rj.get("reasons", "-"))),
            ])
        parts.append("<p style='margin:10px 0 4px;font-weight:600;font-size:13px;'>"
                     f"被拒信号（{len(rejected)} 个，风控拦截）</p>")
        parts.append(_table(["代码", "名称", "参考价", "拒绝原因"], rows))
    else:
        parts.append("<p style='margin:10px 0 4px;font-weight:600;font-size:13px;'>被拒信号</p>"
                     + _empty("今日无被拒信号。"))

    return _section("三、今日操作", "".join(parts))


def _render_pnl_analysis(d: dict) -> str:
    sells = d.get("sells_today") or []
    positions = d.get("positions") or []
    total = d.get("total_asset", 0) or 0
    init_cap = d.get("init_capital") or total
    prev = d.get("prev_total_asset")

    realized = sum(s.get("pnl", 0) or 0 for s in sells)
    floating = sum(p.get("float_pnl", 0) or 0 for p in positions)
    cum_pnl = total - init_cap

    rows = [
        ("今日已实现盈亏", _pnl_span(realized, _money(realized, sign=True))
         + f'<span style="color:#6b7280;">（{len(sells)} 笔卖出）</span>'),
        ("当前浮动盈亏", _pnl_span(floating, _money(floating, sign=True))
         + f'<span style="color:#6b7280;">（{len(positions)} 只持仓）</span>'),
    ]
    if prev is not None and prev > 0:
        day_pnl = total - prev
        rows.append(("今日总盈亏（已实现+浮动）",
                     _pnl_span(day_pnl, _money(day_pnl, sign=True))))
    rows.append(("累计盈亏", _pnl_span(cum_pnl, _money(cum_pnl, sign=True))))

    # 盈利/亏损家数
    if positions:
        winners = sum(1 for p in positions if (p.get("float_pnl", 0) or 0) > 0)
        losers = sum(1 for p in positions if (p.get("float_pnl", 0) or 0) < 0)
        rows.append(("持仓盈亏分布",
                     f'<span style="color:{_COLOR_PROFIT};">{winners} 盈</span>'
                     f' / <span style="color:{_COLOR_LOSS};">{losers} 亏</span>'
                     f' / {len(positions) - winners - losers} 平'))

    body = _table(["指标", "数值"], [[_td(k), _td(v, num=True)] for k, v in rows])
    return _section("四、盈亏分析", body)


def _render_risk(d: dict) -> str:
    parts: list[str] = []
    market_state = d.get("market_state", "-")
    scale = d.get("entry_scale", 1.0)
    if scale is None:
        scale = 1.0
    if scale == 0.0:
        filter_note = "非上涨段，市场过滤生效：今日禁止开新仓"
    elif scale == 0.5:
        filter_note = "非上涨段，市场过滤生效：今日新仓仓位减半"
    else:
        filter_note = "上涨段，允许正常开仓"
    parts.append(
        f"<p style='margin:4px 0;font-size:13px;'>市场状态：<b>{market_state}</b>"
        f"（{filter_note}；扫描信号 {d.get('signals', 0)} 个）</p>"
    )

    # 待执行止损（收盘登记、次日开盘执行）
    pending = d.get("pending_stops") or []
    if pending:
        rows = [[
            _td(p.get("ts_code", "-")),
            _td(_disp_name(p)),
            _td(f"{p.get('volume', 0):,}", num=True),
            _td(f"{p.get('stop_price', 0):.2f}", num=True),
            _td(_esc(p.get("reason", "-"))),
        ] for p in pending]
        parts.append("<p style='margin:10px 0 4px;font-weight:600;font-size:13px;'>"
                     f"⚠️ 待执行止损（{len(pending)} 个，次日开盘价执行）</p>")
        parts.append(_table(["代码", "名称", "股数", "触发线", "原因"], rows))

    # 停手票
    halted = d.get("halted_codes") or {}
    if halted:
        items = "、".join(f"{_esc(code)}（至 {_esc(until)}）" for code, until in halted.items())
        parts.append(f"<p style='margin:10px 0 4px;font-weight:600;font-size:13px;'>"
                     f"⛔ 连亏停手（{len(halted)} 只）</p>"
                     f"<p style='margin:4px 0;font-size:13px;'>{items}</p>")

    # 告警汇总
    alerts = d.get("alerts") or []
    if alerts:
        rows = []
        for a in alerts:
            level = a.get("level", "info")
            badge = {
                "error": f'<span style="color:{_COLOR_LOSS};font-weight:600;">ERROR</span>',
                "warn": '<span style="color:#b45309;font-weight:600;">WARN</span>',
            }.get(level, "INFO")
            rows.append([_td(badge), _td(_esc(a.get("title", ""))), _td(_esc(a.get("detail", "")))])
        parts.append("<p style='margin:10px 0 4px;font-weight:600;font-size:13px;'>"
                     f"告警记录（{len(alerts)} 条）</p>")
        parts.append(_table(["级别", "事件", "详情"], rows))

    return _section("五、市场与风险提示", "".join(parts))


# ===== 主入口 =====

def build_daily_report_email(d: dict) -> tuple[str, str]:
    """构建日报邮件。返回 (subject, html_body)。

    Args:
        d: DailyScanner 巡检 summary（含扩展字段，见模块 docstring）
    """
    trade_date = str(d.get("trade_date", ""))
    market_state = d.get("market_state", "-")
    total = d.get("total_asset", 0) or 0
    mode = "模拟盘" if d.get("mode", "paper") == "paper" else "实盘"

    subject = f"📊 LihuQuantify {mode}日报 {trade_date} | {market_state} | 总资产 {_money(total)}"

    # 头部
    header = (
        '<div style="background:#0f172a;color:#ffffff;padding:16px 20px;'
        'border-radius:8px 8px 0 0;">'
        '<h1 style="margin:0;font-size:18px;">📊 LihuQuantify 每日交易报告</h1>'
        f'<p style="margin:6px 0 0;font-size:13px;color:#94a3b8;">'
        f'{trade_date} · {mode} · 市场状态：<b style="color:#ffffff;">{market_state}</b></p>'
        "</div>"
    )

    sections = "".join([
        _render_overview(d),
        _render_positions(d),
        _render_operations(d),
        _render_pnl_analysis(d),
        _render_risk(d),
    ])

    report = d.get("report", "")
    report_note = (
        f'<p style="margin:12px 0 0;font-size:12px;color:#9ca3af;">'
        f'完整巡检报告：{_esc(report)}</p>' if report else ""
    )
    footer = (
        '<p style="margin:16px 0 0;font-size:12px;color:#9ca3af;'
        'border-top:1px solid #e5e7eb;padding-top:10px;">'
        "本邮件由 LihuQuantify 系统自动生成。以上内容仅供参考，不构成任何投资建议。"
        "投资有风险，入市需谨慎。</p>"
    )

    html = (
        '<div style="font-family:\'Microsoft YaHei\',\'PingFang SC\',Arial,sans-serif;'
        'max-width:820px;margin:0 auto;padding:16px;">'
        + header
        + f'<div style="padding:4px 20px 16px;background:#f8fafc;'
          'border:1px solid #e5e7eb;border-top:none;border-radius:0 0 8px 8px;">'
        + sections + report_note + footer
        + "</div></div>"
    )
    return subject, html
