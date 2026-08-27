"""第五轮（邮件通知优化）测试：综合日报渲染 / 单封邮件机制 / 数据聚合。

对应改动：
    - monitor/daily_report.py：build_daily_report_email（HTML 结构化日报）
    - monitor/alerts.py：WARN/INFO 不再即时发邮件（进日报），仅 ERROR 即时；
      send_daily_report 每日一封
    - monitor/scheduler.py：_build_daily_summary 聚合持仓/操作/盈亏数据
    - 第五轮修复清单（邮件重构后问题）：
        修复1 报告资产口径（交易后重新 query_asset）
        修复2 on_new_day 前移（当日买入 date=latest，T+1 守卫生效）
        修复3 alert_halt 降级 WARN（不即时发邮件）
        修复4 stop_registry 持久化恢复原始止损价（行为回归锁定）
        修复5 HTML 转义
"""

from __future__ import annotations

from datetime import date
from unittest.mock import MagicMock, patch

from lihu_quantify.monitor.alerts import Alerter, EmailAlerter
from lihu_quantify.monitor.daily_report import (
    build_daily_report_email,
    realized_pnl_for_sell,
)


# ============ realized_pnl_for_sell：已实现盈亏口径 ============

def test_realized_pnl_fee_adjusted():
    """扣双边费用：价差盈利但费用吃掉部分。"""
    trades = [
        {"ts_code": "600000.SH", "side": "buy", "price": 10.0, "volume": 1000,
         "commission": 5.0, "date": "2026-08-25"},
        {"ts_code": "600000.SH", "side": "sell", "price": 10.05, "volume": 1000,
         "commission": 5.0, "stamp_tax": 5.025, "date": "2026-08-26"},
    ]
    # pnl = (10.05-10)*1000 - (5+5.025) - 5 = 50 - 10.025 - 5 = 34.975
    assert abs(realized_pnl_for_sell(trades, trades[1]) - 34.975) < 1e-9


def test_realized_pnl_loss_counts_fees():
    """价格持平但费用吃亏 → 计为亏损（负值）。"""
    trades = [
        {"ts_code": "000001.SZ", "side": "buy", "price": 10.0, "volume": 500,
         "commission": 5.0, "date": "2026-08-25"},
        {"ts_code": "000001.SZ", "side": "sell", "price": 10.0, "volume": 500,
         "commission": 5.0, "stamp_tax": 2.5, "date": "2026-08-26"},
    ]
    assert realized_pnl_for_sell(trades, trades[1]) < 0


def test_realized_pnl_uses_latest_prior_buy():
    """多笔买入：取卖出日之前最近一笔买入价。"""
    trades = [
        {"ts_code": "A.SH", "side": "buy", "price": 8.0, "volume": 1000,
         "commission": 5.0, "date": "2026-08-20"},
        {"ts_code": "A.SH", "side": "buy", "price": 9.0, "volume": 1000,
         "commission": 5.0, "date": "2026-08-25"},
        {"ts_code": "A.SH", "side": "sell", "price": 9.5, "volume": 1000,
         "commission": 5.0, "stamp_tax": 4.75, "date": "2026-08-26"},
    ]
    # 基准 9.0：pnl = 0.5*1000 - 9.75 - 5 = 485.25
    assert abs(realized_pnl_for_sell(trades, trades[2]) - 485.25) < 1e-9


def test_realized_pnl_no_prior_buy():
    """无对应买入记录 → 0（不抛异常）。"""
    sell = {"ts_code": "B.SH", "side": "sell", "price": 10.0, "volume": 100,
            "commission": 5.0, "date": "2026-08-26"}
    assert realized_pnl_for_sell([sell], sell) == 0.0


# ============ build_daily_report_email：HTML 日报 ============

def _full_summary() -> dict:
    return {
        "trade_date": date(2026, 8, 27),
        "market_state": "震荡",
        "mode": "paper",
        "signals": 69,
        "entry_scale": 0.5,
        "total_asset": 100753.0,
        "cash": 60000.0,
        "init_capital": 100000.0,
        "prev_total_asset": 100500.0,
        "positions": [
            {"ts_code": "600584.SH", "name": "长电科技", "volume": 200,
             "cost": 77.7, "price": 78.2, "market_value": 15640.0,
             "stop_price": 71.5, "float_pnl": 100.0,
             "float_pnl_pct": 0.0064, "weight": 0.155},
            {"ts_code": "000333.SZ", "name": "美的集团", "volume": 300,
             "cost": 68.0, "price": 65.0, "market_value": 19500.0,
             "stop_price": 62.56, "float_pnl": -900.0,
             "float_pnl_pct": -0.0441, "weight": 0.194},
        ],
        "executed": [
            {"ts_code": "600036.SH", "name": "招商银行", "volume": 600,
             "price": 38.5, "stop": 35.4},
        ],
        "sells_today": [
            {"ts_code": "600519.SH", "name": "贵州茅台", "price": 1450.0,
             "volume": 10, "pnl": -320.0, "reason": "MA10 破位"},
        ],
        "rejected": [
            {"ts_code": "601127.SH", "name": "赛力斯", "price": 85.0,
             "reasons": "铁律1：止损价 89.00 不低于买入价 85.00，拒绝下单"},
        ],
        "pending_stops": [
            {"ts_code": "000333.SZ", "name": "美的集团", "volume": 300,
             "stop_price": 66.5, "reason": "MA10 破位"},
        ],
        "halted_codes": {"600519.SH": "2026-09-26"},
        "alerts": [
            {"level": "warn", "title": "风控拦截: 601127.SH",
             "detail": "铁律1：止损价 89.00 不低于买入价 85.00，拒绝下单"},
        ],
        "report": "outputs/reports/2026-08-27.md",
    }


def test_build_email_subject():
    """主题：日期 + 市场状态 + 总资产。"""
    subject, _ = build_daily_report_email(_full_summary())
    assert "2026-08-27" in subject
    assert "震荡" in subject
    assert "100,753" in subject


def test_build_email_full_modules():
    """完整数据：五大模块与关键字段齐全。"""
    _, html = build_daily_report_email(_full_summary())
    # 模块标题
    for section in ("账户总览", "当前持仓", "今日操作", "盈亏分析", "市场与风险提示"):
        assert section in html
    # 持仓明细（代码/名称/成本/现价/止损线/浮动盈亏）
    assert "600584.SH" in html and "长电科技" in html
    assert "77.70" in html and "78.20" in html and "71.50" in html
    # 今日操作：买入/卖出/拒绝
    assert "600036.SH" in html and "35.40" in html       # 买入 + 止损线
    assert "600519.SH" in html and "MA10 破位" in html    # 卖出原因
    assert "铁律1" in html and "拒绝下单" in html         # 完整拒绝原因
    # 盈亏：今日总盈亏 = 100753 - 100500 = +253
    assert "+253" in html
    # 风险提示：待执行止损 / 停手票 / 市场过滤说明
    assert "待执行止损" in html
    assert "600519.SH" in html and "2026-09-26" in html
    assert "仓位减半" in html   # entry_scale=0.5
    # HTML 结构与免责声明
    assert "<table" in html and "<h2" in html
    assert "不构成任何投资建议" in html


def test_build_email_empty_day():
    """空仓无操作日：不抛异常，显示空仓/无操作文案。"""
    summary = {
        "trade_date": date(2026, 8, 27), "market_state": "上涨", "mode": "paper",
        "signals": 0, "entry_scale": 1.0,
        "total_asset": 100000.0, "cash": 100000.0, "init_capital": 100000.0,
        "prev_total_asset": None,
        "positions": [], "executed": [], "sells_today": [], "rejected": [],
        "pending_stops": [], "halted_codes": {}, "alerts": [],
        "report": "outputs/reports/2026-08-27.md",
    }
    subject, html = build_daily_report_email(summary)
    assert "上涨" in subject
    assert "当前空仓" in html
    assert "今日无买入" in html
    assert "今日无卖出" in html
    assert "今日无被拒信号" in html
    # prev=None → 不显示今日盈亏行，但累计盈亏仍在
    assert "累计盈亏" in html
    assert "今日盈亏" not in html


def test_build_email_block_mode_note():
    """entry_scale=0.0 → block 模式提示。"""
    d = _full_summary()
    d["entry_scale"] = 0.0
    _, html = build_daily_report_email(d)
    assert "禁止开新仓" in html


def test_build_email_pnl_colors():
    """盈亏着色：盈利红(#dc2626)、亏损绿(#16a34a)。"""
    _, html = build_daily_report_email(_full_summary())
    assert "#dc2626" in html   # 浮盈/今日盈利
    assert "#16a34a" in html   # 浮亏/卖出亏损


# ============ Alerter：单封邮件机制 ============

def test_alerter_warn_not_instant_email():
    """第五轮：WARN 告警不再即时发邮件（进日报）。"""
    ea = MagicMock()
    ea.ready = True
    alerter = Alerter(serverchan_key="", email=ea)
    alerter.send("风控拦截: 601127.SH", "铁律1：...", level="warn")
    ea.send.assert_not_called()
    # 但记录进历史（日报渲染用）
    assert len(alerter.history) == 1


def test_alerter_error_still_instant_email():
    """ERROR（系统故障）保留即时邮件（日报发不出时的兜底）。"""
    ea = MagicMock()
    ea.ready = True
    alerter = Alerter(serverchan_key="", email=ea)
    alerter.send("接口异常: daily", "timeout", level="error")
    ea.send.assert_called_once()
    assert "接口异常" in ea.send.call_args[0][0]


def test_alerter_send_daily_report_html():
    """send_daily_report：构建 HTML 日报并 html=True 发送。"""
    ea = MagicMock()
    ea.ready = True
    alerter = Alerter(serverchan_key="", email=ea)
    ok = alerter.send_daily_report(_full_summary())
    assert ok
    ea.send.assert_called_once()
    args, kwargs = ea.send.call_args
    subject, body = args[0], args[1]
    assert kwargs.get("html") is True or (len(args) > 2 and args[2] is True)
    assert "2026-08-27" in subject
    assert "当前持仓" in body


def test_alerter_send_daily_report_no_email():
    """未配置邮件通道 → False 不抛异常。"""
    alerter = Alerter(serverchan_key="")
    assert alerter.send_daily_report({}) is False


def test_email_alerter_send_html_subtype():
    """EmailAlerter.send(html=True) → MIMEText subtype=html。"""
    ea = EmailAlerter("smtp.qq.com", 465, "a@qq.com", "code", ["b@qq.com"])
    with patch("lihu_quantify.monitor.alerts.smtplib.SMTP_SSL"), \
         patch("lihu_quantify.monitor.alerts.MIMEText") as mock_mime:
        assert ea.send("主题", "<p>html</p>", html=True) is True
        mock_mime.assert_called_once()
        assert mock_mime.call_args[0][1] == "html"


# ============ scheduler._build_daily_summary：数据聚合 ============

def _stub_position(ts_code, volume, frozen, cost, market_value):
    from lihu_quantify.execution.base import PositionInfo

    return PositionInfo(ts_code=ts_code, volume=volume, frozen=frozen,
                        cost=cost, market_value=market_value)


def test_build_daily_summary_aggregation():
    """聚合：收盘资产/持仓明细/当日卖出盈亏/待执行止损/停手票。"""
    from lihu_quantify.monitor.scheduler import _build_daily_summary

    broker = MagicMock()
    broker.query_asset.return_value = {"cash": 60000.0, "total_asset": 100753.0,
                                       "market_value": 40753.0}
    broker.init_capital = 100000.0
    broker.get_price.side_effect = lambda c: {"600584.SH": 78.2}.get(c, 0.0)
    broker.trades = [
        {"ts_code": "600584.SH", "side": "buy", "price": 77.7, "volume": 200,
         "commission": 5.0, "date": "2026-08-26"},
        {"ts_code": "600584.SH", "side": "sell", "price": 78.2, "volume": 200,
         "commission": 5.0, "stamp_tax": 7.82, "date": "2026-08-27"},
    ]
    broker.halted_codes.return_value = {"600519.SH": date(2026, 9, 26)}
    stop = MagicMock()
    stop.stop_price = 71.5
    summary = _build_daily_summary(
        broker=broker,
        latest=date(2026, 8, 27),
        market_state="震荡",
        signals=5,
        entry_scale=0.5,
        executed=[{"ts_code": "600036.SH", "name": "招商银行", "volume": 600,
                   "price": 38.5, "stop": 35.4}],
        rejected=[{"ts_code": "601127.SH", "name": "赛力斯", "price": 85.0,
                   "reasons": "铁律1拒绝"}],
        executed_stops=[{"ts_code": "600584.SH", "volume": 200,
                         "stop_price": 71.5, "open_price": 78.2,
                         "reason": "ma_break"}],
        pending_stops=[{"ts_code": "000333.SZ", "volume": 300,
                        "stop_price": 66.5, "reason": "price_stop"}],
        positions=[_stub_position("600584.SH", 0, 200, 77.7, 15640.0)],
        stop_registry={"600584.SH": stop},
        name_map={"600584.SH": "长电科技"},
        prev_total_asset=100500.0,
        report_path="outputs/reports/2026-08-27.md",
        mode="paper",
        alerts=[{"level": "warn", "title": "t", "detail": "d"}],
    )
    # 资产
    assert summary["total_asset"] == 100753.0
    assert summary["cash"] == 60000.0
    assert summary["init_capital"] == 100000.0
    assert summary["prev_total_asset"] == 100500.0
    # 当日卖出：识别 + 盈亏 + 原因标签
    assert len(summary["sells_today"]) == 1
    sell = summary["sells_today"][0]
    assert sell["ts_code"] == "600584.SH"
    assert sell["name"] == "长电科技"
    assert sell["reason"] == "MA10 破位"
    # pnl = (78.2-77.7)*200 - (5+7.82) - 5 = 82.18
    assert abs(sell["pnl"] - 82.18) < 1e-9
    # 待执行止损：原因映射
    assert summary["pending_stops"][0]["reason"] == "价格止损"
    # 停手票
    assert summary["halted_codes"] == {"600519.SH": "2026-09-26"}
    # 昨日买入不计入当日卖出
    assert summary["positions"][0]["volume"] == 200
    assert summary["positions"][0]["float_pnl"] == (78.2 - 77.7) * 200


def test_build_daily_summary_expired_halt_filtered():
    """停手期已过 → 不显示。"""
    from lihu_quantify.monitor.scheduler import _build_daily_summary

    broker = MagicMock()
    broker.query_asset.return_value = {"cash": 1.0, "total_asset": 1.0,
                                       "market_value": 0.0}
    broker.init_capital = 1.0
    broker.get_price.return_value = 0.0
    broker.trades = []
    broker.halted_codes.return_value = {"600519.SH": date(2026, 8, 1)}
    summary = _build_daily_summary(
        broker=broker, latest=date(2026, 8, 27), market_state="上涨",
        signals=0, entry_scale=1.0, executed=[], rejected=[],
        executed_stops=[], pending_stops=[], positions=[],
        stop_registry={}, name_map={}, prev_total_asset=None,
        report_path="", mode="paper", alerts=[],
    )
    assert summary["halted_codes"] == {}


# ============ 第五轮修复清单：修复3（alert_halt 降级 WARN） ============

def test_alert_halt_no_instant_email():
    """修复3：连亏停手（原 ERROR→WARN）不再即时发邮件，进日报风险提示区。"""
    ea = MagicMock()
    ea.ready = True
    alerter = Alerter(serverchan_key="", email=ea)
    alerter.alert_halt("600519.SH", "2026-09-26")
    ea.send.assert_not_called()
    assert alerter.history[-1]["level"] == "warn"
    assert "600519.SH" in alerter.history[-1]["title"]


# ============ 第五轮修复清单：修复4（stop_registry 持久化） ============

def test_stop_registry_persists_original_stop_price(tmp_path):
    """修复4（行为已在第二轮修复B落地，此处回归锁定）：
    止损登记持久化 → 新 OMS 实例从文件恢复原始止损价，
    rebuild_stops_from_positions 不用"成本-8%"覆盖已有登记。
    """
    from lihu_quantify.execution.oms import OrderManagementSystem
    from lihu_quantify.execution.paper_trade import PaperBroker
    from lihu_quantify.types import Signal

    broker = PaperBroker(init_capital=1_000_000.0, persist=False,
                         state_file=str(tmp_path / "state.json"))
    broker.on_new_day(date(2026, 8, 27))
    sig = Signal(kind="buy", ts_code="600000.SH", suggested_price=10.0,
                 stop_loss=9.5, suggested_position_pct=0.10)
    oms1 = OrderManagementSystem(broker, registry_file=str(tmp_path / "stop.json"))
    buy, stop = oms1.place_buy_with_stop(sig, 1000, 10.0)
    assert buy.success and stop.stop_price == 9.5

    # 新 OMS 实例（模拟 scheduler 次日 scan：每次新建 OMS）
    oms2 = OrderManagementSystem(broker, registry_file=str(tmp_path / "stop.json"))
    assert "600000.SH" in oms2.stop_registry
    assert oms2.stop_registry["600000.SH"].stop_price == 9.5

    # rebuild 只补缺失登记：已有原始止损价不被成本-8%（10×0.92=9.2）覆盖
    oms2.rebuild_stops_from_positions()
    assert oms2.stop_registry["600000.SH"].stop_price == 9.5


# ============ 第五轮修复清单：修复5（HTML 转义） ============

def test_build_email_escapes_html():
    """修复5：外部来源字符串（股票名/拒绝原因/告警详情/报告路径）转义。"""
    d = {
        "trade_date": date(2026, 8, 27), "market_state": "上涨", "mode": "paper",
        "signals": 1, "entry_scale": 1.0,
        "total_asset": 100000.0, "cash": 100000.0, "init_capital": 100000.0,
        "prev_total_asset": None,
        "positions": [{
            "ts_code": "A.SH", "name": "<script>alert(1)</script>",
            "volume": 100, "cost": 10.0, "price": 10.0, "market_value": 1000.0,
            "stop_price": 9.0, "float_pnl": 0.0, "float_pnl_pct": 0.0,
            "weight": 0.01,
        }],
        "executed": [],
        "sells_today": [{
            "ts_code": "C.SH", "name": "X&Y", "price": 10.0, "volume": 100,
            "pnl": 0.0, "reason": "<离场原因>",
        }],
        "rejected": [{
            "ts_code": "B.SH", "name": "A&B公司", "price": 10.0,
            "reasons": "铁律1：<止损> & 仓位超限",
        }],
        "pending_stops": [{
            "ts_code": "D.SH", "name": "Y", "volume": 100,
            "stop_price": 9.0, "reason": "<触发原因>",
        }],
        "halted_codes": {"E.SH": "2026-09-26"},
        "alerts": [{"level": "warn", "title": "<事件标题>", "detail": "<详情&内容>"}],
        "report": "outputs/reports/<2026>.md",
    }
    _, body = build_daily_report_email(d)
    # 恶意/特殊字符不破坏 HTML 结构
    assert "<script>" not in body
    assert "<止损>" not in body
    assert "<事件标题>" not in body
    # 转义后的实体存在
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in body
    assert "A&amp;B公司" in body
    assert "铁律1：&lt;止损&gt; &amp; 仓位超限" in body
    assert "&lt;事件标题&gt;" in body
    assert "&lt;详情&amp;内容&gt;" in body
    assert "&lt;离场原因&gt;" in body
    assert "&lt;触发原因&gt;" in body
    assert "outputs/reports/&lt;2026&gt;.md" in body


# ============ 第五轮修复清单：修复1+2（_scan_impl 集成） ============

def test_scan_impl_report_asset_and_t1_guard(tmp_path, monkeypatch):
    """修复1+2（第五轮清单）：

    - 修复2：on_new_day 在买入前调用 → 当日买入记录 date=latest，
      当日新仓不做当日收盘止损判定（T+1 守卫生效，无 pending 登记）；
    - 修复1：.md 报告的 total_asset/cash 用交易后口径（与邮件日报一致）。
    """
    import pandas as pd

    from lihu_quantify.config import RiskConfig, StrategyConfig
    from lihu_quantify.execution.oms import OrderManagementSystem
    from lihu_quantify.execution.paper_trade import PaperBroker
    from lihu_quantify.monitor import scheduler as sched_mod
    from lihu_quantify.monitor.scheduler import DailyScanner
    from lihu_quantify.types import Signal

    latest = date(2026, 8, 27)

    # 真实 PaperBroker：手动注入现价 9.0（< 止损价 9.5——若 T+1 守卫失效，
    # 当日即触发"收盘≤止损线"登记，正是修复2要防的 churn）
    broker = PaperBroker(init_capital=1_000_000.0, persist=False,
                         state_file=str(tmp_path / "state.json"))
    broker.set_price("600000.SH", 9.0)

    # stub scanner（绕过 __init__ 的真实 Tushare/DuckDB 连接）
    s = DailyScanner.__new__(DailyScanner)
    s.broker = broker
    s.alerter = Alerter()
    s.reporter = MagicMock()
    s.reporter.daily_report.return_value = tmp_path / "report.md"
    s.mode = "paper"
    s.settings = MagicMock()

    # 隔离文件副作用：pending_stops/_ROOT、过滤统计
    monkeypatch.setattr(sched_mod, "_ROOT", tmp_path)
    monkeypatch.setattr(sched_mod, "_append_filter_stats", lambda rec: None)

    # client mock：daily 返回 40 行日线（close=10.0）
    def fake_query(api, params=None, use_cache=True):
        dates = pd.date_range(end="2026-08-27", periods=40, freq="D")
        return pd.DataFrame({
            "ts_code": "600000.SH",
            "trade_date": [d.strftime("%Y%m%d") for d in dates],
            "open": 10.0, "high": 10.5, "low": 9.5, "close": 10.0,
            "vol": 1000.0, "amount": 10000.0,
        })

    s.client = MagicMock()
    s.client.query.side_effect = fake_query

    # OMS：真实（registry_file 隔离到 tmp）
    monkeypatch.setattr(
        sched_mod, "OrderManagementSystem",
        lambda b: OrderManagementSystem(b, registry_file=str(tmp_path / "stop.json")),
    )

    # 策略 mock：返回 1 个买入信号（止损价 9.5 < 买入价 10.0）
    sig = Signal(kind="buy", ts_code="600000.SH", suggested_price=10.0,
                 stop_loss=9.5, suggested_position_pct=0.10)
    strategy = MagicMock()
    strategy.latest_signal.return_value = sig
    monkeypatch.setattr(sched_mod, "CherryClaw", lambda **kw: strategy)
    monkeypatch.setattr(sched_mod, "add_all_standard",
                        lambda df: df.assign(ma10=10.0))

    # 股票池：1 只
    monkeypatch.setattr(
        s, "_universe",
        lambda n: (["600000.SH"], {}, {"600000.SH": "浦发银行"}),
    )

    # Checklist 闸门：放行
    gate = MagicMock()
    gate.check.return_value = MagicMock(approved=True, rejected_items=lambda: [])
    monkeypatch.setattr(sched_mod, "ChecklistGate", lambda **kw: gate)

    summary = s._scan_impl(
        latest, "上涨", n=50, days=120,
        s=StrategyConfig(market_filter=False), r=RiskConfig(),
        prev_total_asset=None,
    )

    # ---- 修复2：当日买入记录 date == latest（on_new_day 前移生效） ----
    buys = [t for t in broker.trades if t["side"] == "buy"]
    assert len(buys) == 1
    assert buys[0]["date"] == latest

    # ---- 修复2：T+1 守卫生效——现价 9.0 ≤ 止损价 9.5，但当日新仓
    #      不做当日收盘判定 → 无 pending 登记 ----
    assert not (tmp_path / "data" / "pending_stops.json").exists()

    # ---- 修复1：报告用交易后资产（买入 10000 股 @10.0，佣金 25；
    #      现价 9.0 → 市值 90000；现金 1000000-100025=899975） ----
    kwargs = s.reporter.daily_report.call_args.kwargs
    assert kwargs["total_asset"] == 989_975.0
    assert kwargs["cash"] == 899_975.0
    # 报告与邮件日报（summary）数字一致
    assert summary["total_asset"] == kwargs["total_asset"]
    assert summary["cash"] == kwargs["cash"]
    # 总资产 = 现金 + 持仓市值
    assert kwargs["total_asset"] == kwargs["cash"] + 90_000.0

    # 日报数据：持仓止损线 = 买入时登记的原始止损价（修复4 联动）
    assert summary["positions"][0]["stop_price"] == 9.5
    assert summary["positions"][0]["name"] == "浦发银行"
