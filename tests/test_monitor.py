"""监控层测试：告警器 + 报告生成 + 巡检调度构建。"""

from __future__ import annotations

from datetime import date
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from lihu_quantify.monitor.alerts import Alerter, LEVEL_WARN
from lihu_quantify.monitor.report import ReportGenerator


# ============ Alerter ============

def test_alerter_console_only():
    """无 Server酱 key：仅控制台，不报错。"""
    alerter = Alerter(serverchan_key="")
    ok = alerter.send("测试标题", "测试详情", level=LEVEL_WARN)
    assert ok
    assert len(alerter.history) == 1
    assert alerter.history[0]["title"] == "测试标题"


def test_alerter_disabled():
    """enabled=False 完全静默。"""
    alerter = Alerter(enabled=False)
    assert alerter.send("不应记录") is False
    assert alerter.history == []


def test_alerter_business_methods():
    """业务告警便捷方法。"""
    alerter = Alerter()
    alerter.alert_checklist_reject("600519.SH", "仓位超限")
    alerter.alert_stop_loss("600584.SH", 75.0, 76.0)
    alerter.alert_halt("600584.SH", date(2026, 9, 26))
    alerter.alert_bought("000001.SZ", 100, 10.0, 9.2)
    assert len(alerter.history) == 4
    # 按级别过滤（修复3/第五轮清单：halt 由 error 降为 warn）
    warns = alerter.alerts_since(LEVEL_WARN)
    assert len(warns) == 3   # checklist_reject + stop_loss + halt


def test_alerter_serverchan_push_mock():
    """Server酱推送：mock requests，key 非空时调用。"""
    alerter = Alerter(serverchan_key="SCT_fake_key")
    with patch("lihu_quantify.monitor.alerts.requests") as mock_req:
        mock_resp = MagicMock()
        mock_resp.json.return_value = {"code": 0, "message": ""}
        mock_req.post.return_value = mock_resp
        alerter.send("推送测试", "详情")
        assert mock_req.post.called
        # URL 含 key
        url = mock_req.post.call_args[0][0]
        assert "SCT_fake_key" in url


def test_alerter_serverchan_push_failure_not_fatal():
    """Server酱推送失败不阻断主流程（P2-9-7：返回实通道失败结果）。"""
    alerter = Alerter(serverchan_key="SCT_bad")
    with patch("lihu_quantify.monitor.alerts.requests") as mock_req:
        mock_req.post.side_effect = Exception("网络异常")
        ok = alerter.send("推送失败测试")   # 实通道全失败 → 返回 False，但不抛异常
        assert ok is False


# ============ ReportGenerator ============

def test_daily_report_full(tmp_path: Path):
    """完整报告生成与归档。"""
    reporter = ReportGenerator(tmp_path)
    path = reporter.daily_report(
        trade_date=date(2026, 8, 26),
        market_state="震荡",
        total_asset=153619.0,
        cash=80000.0,
        positions=[
            {"ts_code": "600584.SH", "volume": 200, "cost": 77.7, "market_value": 15548.0},
        ],
        signals=[
            {"ts_code": "600036.SH", "price": 38.5, "reason": "三层过滤通过"},
        ],
        executed=[
            {"ts_code": "600036.SH", "volume": 600, "price": 38.5, "stop": 35.4},
        ],
        rejected=[
            {"ts_code": "600519.SH", "reasons": "仓位预算(占比 30% 超过 25%)"},
        ],
        stop_orders=[
            {"ts_code": "600584.SH", "stop_price": 71.5, "triggered": False},
        ],
        alerts=[{"level": "warn", "title": "风控拦截: 600519.SH", "detail": "仓位预算"}],
        mode="paper",
    )
    assert path.exists()
    assert path.name == "2026-08-26.md"
    content = path.read_text(encoding="utf-8")
    # 关键段落齐全
    assert "每日巡检报告" in content
    assert "数据基准" in content and "2026-08-26" in content
    assert "震荡" in content
    assert "持仓与止损监控" in content
    assert "600584.SH" in content and "71.50" in content
    assert "信号与执行" in content
    assert "买入+止损同时挂出" in content
    assert "Checklist 拒绝" in content
    assert "仓位预算" in content
    assert "铁律自检" in content
    assert "不构成任何投资建议" in content


def test_daily_report_empty_positions(tmp_path: Path):
    """空仓报告。"""
    reporter = ReportGenerator(tmp_path)
    path = reporter.daily_report(
        trade_date=date(2026, 8, 26),
        market_state="上涨",
        total_asset=100000, cash=100000,
        positions=[], signals=[], executed=[], rejected=[], stop_orders=[],
    )
    content = path.read_text(encoding="utf-8")
    assert "空仓" in content
    assert "无告警" in content


def test_daily_report_missing_stop_flag(tmp_path: Path):
    """持仓缺止损登记 → 报告标警告。"""
    reporter = ReportGenerator(tmp_path)
    path = reporter.daily_report(
        trade_date=date(2026, 8, 26),
        market_state="上涨",
        total_asset=100000, cash=50000,
        positions=[{"ts_code": "600519.SH", "volume": 100, "cost": 1800, "market_value": 185000}],
        signals=[], executed=[], rejected=[],
        stop_orders=[],   # 无止损登记
    )
    content = path.read_text(encoding="utf-8")
    assert "需重建" in content or "--rebuild" in content


# ============ Scheduler 构建 ============

def test_setup_scheduler_builds():
    """调度器可构建且注册了 daily_scan 任务。"""
    from lihu_quantify.config import get_settings
    from lihu_quantify.monitor.scheduler import setup_scheduler

    settings = get_settings("config/settings.yaml")
    sched = setup_scheduler(settings, mode="paper", n=10)
    jobs = sched.get_jobs()
    assert any(job.id == "daily_scan" for job in jobs)
