"""第四轮（部署与运维）测试：邮件告警 / 心跳 / 幂等 / 日志轮转。

对应 docs/第四轮清单_部署与运维_for_Trae.md 清单 1/2/6/7。
"""

from __future__ import annotations

import json
from datetime import date
from unittest.mock import MagicMock, patch

import pytest

from lihu_quantify.monitor.alerts import Alerter, EmailAlerter, build_email_alerter
from lihu_quantify.monitor.heartbeat import Heartbeat, ping


# ============ 清单1：EmailAlerter ============

def test_email_alerter_ready():
    """配置齐全 → ready=True；缺任一项 → False。"""
    ok = EmailAlerter("smtp.qq.com", 465, "a@qq.com", "code", ["b@qq.com"])
    assert ok.ready
    assert not EmailAlerter("smtp.qq.com", 465, "a@qq.com", "", ["b@qq.com"]).ready
    assert not EmailAlerter("smtp.qq.com", 465, "a@qq.com", "code", []).ready


def test_email_alerter_send_mock():
    """send：SMTP_SSL 登录 + 发送，成功返回 True。"""
    ea = EmailAlerter("smtp.qq.com", 465, "a@qq.com", "code", ["b@qq.com"])
    with patch("lihu_quantify.monitor.alerts.smtplib.SMTP_SSL") as mock_smtp:
        ctx = mock_smtp.return_value.__enter__.return_value
        assert ea.send("测试主题", "正文") is True
        ctx.login.assert_called_once_with("a@qq.com", "code")
        assert ctx.sendmail.called
        _, kwargs = mock_smtp.call_args
        assert kwargs["timeout"] == 15


def test_email_alerter_send_failure_not_fatal():
    """发送异常：返回 False 不抛出（不影响主流程）。"""
    ea = EmailAlerter("smtp.qq.com", 465, "a@qq.com", "code", ["b@qq.com"])
    with patch("lihu_quantify.monitor.alerts.smtplib.SMTP_SSL",
               side_effect=ConnectionError("smtp down")):
        assert ea.send("主题", "正文") is False


def test_email_alerter_not_ready_skip():
    """配置不完整：直接 False，不触碰 SMTP。"""
    ea = EmailAlerter("", 465, "", "", [])
    with patch("lihu_quantify.monitor.alerts.smtplib.SMTP_SSL") as mock_smtp:
        assert ea.send("主题", "正文") is False
        mock_smtp.assert_not_called()


def test_build_email_alerter_disabled():
    """enabled=False → None。"""
    cfg = MagicMock(enabled=False)
    assert build_email_alerter(cfg) is None


def test_build_email_alerter_incomplete():
    """enabled=True 但配置不全 → None（且 warning）。"""
    cfg = MagicMock(enabled=True, smtp_host="smtp.qq.com", smtp_port=465,
                    username="", auth_code="", to=[])
    assert build_email_alerter(cfg) is None


def test_alerter_email_channel_warn_only():
    """Alerter 集成：warn/error 即时发邮件；info 不发（进每日摘要）。"""
    ea = MagicMock()
    ea.ready = True
    alerter = Alerter(serverchan_key="", email=ea)
    alerter.send("info 事件", level="info")
    ea.send.assert_not_called()
    alerter.send("warn 事件", level="warn")
    ea.send.assert_called_once()
    subject = ea.send.call_args[0][0]
    assert "warn 事件" in subject


def test_daily_digest_counts_lists():
    """摘要邮件：executed/rejected 为 list 时按数量展示。"""
    ea = MagicMock()
    ea.ready = True
    alerter = Alerter(email=ea)
    summary = {
        "trade_date": date(2026, 8, 26), "market_state": "上涨",
        "signals": 10, "executed": [{}, {}, {}], "rejected": [{}],
        "total_asset": 100753, "report": "outputs/reports/x.md",
    }
    alerter.send_daily_digest(summary)
    body = ea.send.call_args[0][1]
    assert "成交: 3 笔" in body
    assert "拦截: 1 个" in body


# ============ 清单2：Heartbeat ============

def test_ping_empty_url():
    """url 空 → False（no-op）。"""
    assert ping("", "") is False
    assert ping("", "/start") is False


def test_ping_suffix_concat():
    """suffix 拼接：/start /fail 与成功 ping。"""
    with patch("lihu_quantify.monitor.heartbeat.requests") as mock_req:
        mock_req.get.return_value = MagicMock(status_code=200)
        assert ping("https://hc-ping.com/uuid", "/start") is True
        url = mock_req.get.call_args[0][0]
        assert url == "https://hc-ping.com/uuid/start"

        mock_req.get.return_value = MagicMock(status_code=200)
        assert ping("https://hc-ping.com/uuid", "/fail") is True
        assert mock_req.get.call_args[0][0] == "https://hc-ping.com/uuid/fail"

        # 成功 ping（无 suffix）：末尾不带斜杠
        mock_req.get.return_value = MagicMock(status_code=200)
        assert ping("https://hc-ping.com/uuid/") is True
        assert mock_req.get.call_args[0][0] == "https://hc-ping.com/uuid"


def test_ping_error_not_fatal():
    """网络异常 → False 不抛出。"""
    with patch("lihu_quantify.monitor.heartbeat.requests.get",
               side_effect=TimeoutError("no net")):
        assert ping("https://hc-ping.com/uuid") is False


def test_heartbeat_class_dispatch():
    """Heartbeat 封装：start/success/fail 各 ping 一次对应后缀。"""
    hb = Heartbeat("https://hc-ping.com/uuid")
    assert hb.enabled
    with patch("lihu_quantify.monitor.heartbeat.requests") as mock_req:
        mock_req.get.return_value = MagicMock(status_code=200)
        hb.start()
        assert mock_req.get.call_args[0][0].endswith("/start")
        hb.success()
        assert mock_req.get.call_args[0][0].endswith("/uuid")
        hb.fail()
        assert mock_req.get.call_args[0][0].endswith("/fail")


def test_heartbeat_disabled_noop():
    """url 空 → 全部 no-op，不发起请求。"""
    hb = Heartbeat("")
    assert not hb.enabled
    with patch("lihu_quantify.monitor.heartbeat.requests") as mock_req:
        hb.start()
        hb.success()
        hb.fail()
        mock_req.get.assert_not_called()


# ============ 清单6：幂等保护 ============

def _scanner_stub():
    """跳过 __init__ 的 DailyScanner（避免真实 Tushare/DuckDB 连接）。"""
    from lihu_quantify.monitor.scheduler import DailyScanner

    s = DailyScanner.__new__(DailyScanner)
    s.heartbeat = Heartbeat("")
    s.alerter = Alerter()
    s.settings = MagicMock()
    s.mode = "paper"
    return s


def test_json_safe_recursive():
    """date→str 递归序列化。"""
    from lihu_quantify.monitor.scheduler import _json_safe

    out = _json_safe({
        "trade_date": date(2026, 8, 26),
        "executed": [{"ts_code": "600519.SH", "stop": 100.0}],
    })
    assert out["trade_date"] == "2026-08-26"
    assert out["executed"][0]["ts_code"] == "600519.SH"
    json.dumps(out)  # 可序列化


def test_last_scan_roundtrip(tmp_path, monkeypatch):
    """_write → _read 往返一致（写入 tmp，不污染真实 data/）。"""
    from lihu_quantify.monitor import scheduler as sched_mod

    monkeypatch.setattr(sched_mod, "_ROOT", tmp_path)
    s = _scanner_stub()
    summary = {"trade_date": date(2026, 8, 26), "signals": 3,
               "executed": [], "rejected": [], "report": "x.md", "total_asset": 1.0}
    s._write_last_scan(date(2026, 8, 26), summary)
    last = s._read_last_scan()
    assert last["trade_date"] == "2026-08-26"
    assert last["finished_at"]
    assert last["summary"]["signals"] == 3


def test_last_scan_read_missing(tmp_path, monkeypatch):
    """文件不存在 / 损坏 → None（视为未巡检）。"""
    from lihu_quantify.monitor import scheduler as sched_mod

    monkeypatch.setattr(sched_mod, "_ROOT", tmp_path)
    s = _scanner_stub()
    assert s._read_last_scan() is None
    (tmp_path / "data").mkdir(parents=True)
    (tmp_path / "data" / "last_scan.json").write_text("{bad json", encoding="utf-8")
    assert s._read_last_scan() is None


def test_scan_idempotent_skip():
    """当日已巡检 → 直接返回上次摘要，主体不执行。"""
    s = _scanner_stub()
    prev_summary = {"trade_date": "2026-08-26", "signals": 5, "executed": [],
                    "rejected": [], "report": "prev.md", "total_asset": 100.0}
    with patch.object(s, "_market_state", return_value=(date(2026, 8, 26), "上涨")), \
         patch.object(s, "_read_last_scan",
                      return_value={"trade_date": "2026-08-26",
                                    "finished_at": "2026-08-26 16:31:00",
                                    "summary": prev_summary}), \
         patch.object(s, "_scan_impl") as impl, \
         patch.object(s, "_write_last_scan") as w:
        out = s.scan(n=10)
    impl.assert_not_called()
    w.assert_not_called()
    assert out["report"] == "prev.md"


def test_scan_force_reruns():
    """force=True：跳过幂等检查，主体执行。"""
    s = _scanner_stub()
    with patch.object(s, "_market_state", return_value=(date(2026, 8, 26), "上涨")), \
         patch.object(s, "_read_last_scan",
                      return_value={"trade_date": "2026-08-26", "summary": {}}), \
         patch.object(s, "_scan_impl",
                      return_value={"trade_date": date(2026, 8, 26), "signals": 1,
                                    "executed": [], "rejected": [],
                                    "report": "new.md", "total_asset": 2.0}) as impl, \
         patch.object(s, "_write_last_scan") as w, \
         patch.object(s, "_send_digest") as d:
        out = s.scan(n=10, force=True)
    impl.assert_called_once()
    w.assert_called_once()
    d.assert_called_once()
    assert out["report"] == "new.md"


def test_scan_different_day_reruns():
    """上次巡检是昨日 → 今日正常执行（幂等只挡当日重复）。"""
    s = _scanner_stub()
    with patch.object(s, "_market_state", return_value=(date(2026, 8, 26), "上涨")), \
         patch.object(s, "_read_last_scan",
                      return_value={"trade_date": "2026-08-25", "summary": {}}), \
         patch.object(s, "_scan_impl",
                      return_value={"trade_date": date(2026, 8, 26), "signals": 0,
                                    "executed": [], "rejected": [],
                                    "report": "t.md", "total_asset": 0}), \
         patch.object(s, "_write_last_scan"), patch.object(s, "_send_digest"):
        out = s.scan(n=10)
    assert str(out["trade_date"]) == "2026-08-26"


def test_scan_heartbeat_success_flow():
    """成功路径：start → success，fail 不触发。"""
    s = _scanner_stub()
    hb = MagicMock()
    s.heartbeat = hb
    with patch.object(s, "_market_state", return_value=(date(2026, 8, 26), "上涨")), \
         patch.object(s, "_read_last_scan", return_value=None), \
         patch.object(s, "_scan_impl",
                      return_value={"trade_date": date(2026, 8, 26),
                                    "executed": [], "rejected": []}), \
         patch.object(s, "_write_last_scan"), patch.object(s, "_send_digest"):
        s.scan(n=10)
    hb.start.assert_called_once()
    hb.success.assert_called_once()
    hb.fail.assert_not_called()


def test_scan_heartbeat_fail_flow():
    """异常路径：start → fail，异常向上抛，last_scan 不写。"""
    s = _scanner_stub()
    hb = MagicMock()
    s.heartbeat = hb
    with patch.object(s, "_market_state", return_value=(date(2026, 8, 26), "上涨")), \
         patch.object(s, "_read_last_scan", return_value=None), \
         patch.object(s, "_scan_impl", side_effect=RuntimeError("tushare down")), \
         patch.object(s, "_write_last_scan") as w:
        with pytest.raises(RuntimeError):
            s.scan(n=10)
    hb.start.assert_called_once()
    hb.fail.assert_called_once()
    hb.success.assert_not_called()
    w.assert_not_called()


# ============ 清单7：日志轮转 ============

def test_setup_file_logging(tmp_path, monkeypatch):
    """文件 sink 启用：写入 INFO 落盘；同 name 重复调用幂等。"""
    from lihu_quantify.monitor import log_setup

    monkeypatch.setattr(log_setup, "_ROOT", tmp_path)
    log_setup.setup_file_logging("pytest_check")
    from loguru import logger

    logger.info("[测试] loguru 文件 sink 验证")
    logger.complete()  # flush
    logs = list((tmp_path / "data" / "logs").glob("pytest_check_*.log"))
    assert logs, "日志文件未生成"
    assert "[测试] loguru 文件 sink 验证" in logs[0].read_text(encoding="utf-8")

    # 幂等：重复调用不重复添加 sink
    import loguru as _loguru

    before = len(_loguru.logger._core.handlers)
    log_setup.setup_file_logging("pytest_check")
    assert len(_loguru.logger._core.handlers) == before
    # 清理：移除该 sink，避免影响其他测试
    _loguru.logger.remove()
    _loguru.logger.add(lambda msg: None)
