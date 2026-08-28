"""第八轮清单（AI 收盘总结）测试：纯展示层 + 静默降级 + 开关一致性。

覆盖（docs/第八轮清单_AI收盘总结_for_Trae.md 第 5 节验收标准的可自动化部分）：
1. build_ai_summary 守卫：未配置/无 key/stub 配置 → None，零网络请求
2. 成功路径：payload 结构（model/messages/Bearer）+ max_chars 截断
3. 失败降级：超时 / HTTP 错误 → None，不抛异常
4. _build_prompt：rich summary 关键字段全部注入（零新增取数）
5. .md 报告 / 邮件日报：有 ai_summary → "八、AI 收盘总结"；None → 整节省略
6. 开关一致性：enabled=false/true 同日同场景——signals/executed/rejected/
   资产完全一致（证明 AI 纯展示、不影响决策）
"""
from __future__ import annotations

from datetime import date
from unittest.mock import MagicMock, patch

import pytest
import requests

from lihu_quantify.config import AiSummaryConfig

AI_TEXT = ("今日市场与账户：市场上涨，账户浮盈。持仓点评：持仓平稳。"
           "操作回顾：买入 2 笔。风险提示：无待执行止损。"
           "以上内容由 AI 自动生成，仅供参考，不构成投资建议。")


# ============================================================
# 1. 守卫路径（零网络）
# ============================================================

def test_guard_paths_return_none_without_network():
    from lihu_quantify.monitor.ai_summary import build_ai_summary

    summary = {"trade_date": "2026-08-28", "total_asset": 100000.0}
    with patch("lihu_quantify.monitor.ai_summary.requests.post",
               side_effect=AssertionError("不应发起网络请求")):
        # 开关关闭
        assert build_ai_summary(
            summary, AiSummaryConfig(enabled=False, api_key="sk-x"), "sk-x") is None
        # enabled 但无 key
        assert build_ai_summary(
            summary, AiSummaryConfig(enabled=True, api_key=""), "") is None
        # cfg 非 AiSummaryConfig（测试 stub / 兼容旧调用方）→ 零侵入
        assert build_ai_summary(summary, MagicMock(), "sk-x") is None
        assert build_ai_summary(summary, None, "sk-x") is None


# ============================================================
# 2. 成功路径：payload + 截断
# ============================================================

def test_success_payload_and_truncation():
    from lihu_quantify.monitor import ai_summary as mod

    cfg = AiSummaryConfig(enabled=True, api_key="sk-test", max_chars=50)
    resp = MagicMock()
    resp.raise_for_status.return_value = None
    resp.json.return_value = {"choices": [{"message": {"content": AI_TEXT}}]}
    with patch.object(mod.requests, "post", return_value=resp) as post:
        out = mod.build_ai_summary({"trade_date": "2026-08-28"}, cfg, "sk-test")

    assert out == AI_TEXT[:50]                      # max_chars 截断
    kwargs = post.call_args.kwargs
    assert kwargs["headers"]["Authorization"] == "Bearer sk-test"
    body = kwargs["json"]
    assert body["model"] == cfg.model
    assert body["messages"][0]["role"] == "system"
    assert body["messages"][1]["role"] == "user"
    assert "2026-08-28" in body["messages"][1]["content"]
    # OpenAI 兼容端点 URL：{api_base}/chat/completions
    assert post.call_args.args[0].endswith("/chat/completions")


def test_system_prompt_constraints():
    """SYSTEM_PROMPT 四段式 + 免责声明 + 禁荐股约束（清单第 3 节）。"""
    from lihu_quantify.monitor.ai_summary import SYSTEM_PROMPT

    assert "四段" in SYSTEM_PROMPT and "禁止编造" in SYSTEM_PROMPT
    assert "不输出" in SYSTEM_PROMPT and "买入/卖出" in SYSTEM_PROMPT
    assert "以上内容由 AI 自动生成，仅供参考，不构成投资建议。" in SYSTEM_PROMPT


# ============================================================
# 3. 失败降级
# ============================================================

def test_failure_degrades_silently():
    from lihu_quantify.monitor import ai_summary as mod

    cfg = AiSummaryConfig(enabled=True, api_key="sk-test", timeout=1)
    # 断网/超时
    with patch.object(mod.requests, "post",
                      side_effect=requests.Timeout("connect timeout")):
        assert mod.build_ai_summary({"trade_date": "x"}, cfg, "sk-test") is None
    # key 错误（401）
    resp = MagicMock()
    resp.raise_for_status.side_effect = requests.HTTPError("401 Unauthorized")
    with patch.object(mod.requests, "post", return_value=resp):
        assert mod.build_ai_summary({"trade_date": "x"}, cfg, "sk-test") is None
    # 响应结构异常
    resp2 = MagicMock()
    resp2.raise_for_status.return_value = None
    resp2.json.return_value = {}
    with patch.object(mod.requests, "post", return_value=resp2):
        assert mod.build_ai_summary({"trade_date": "x"}, cfg, "sk-test") is None
    # 空输出
    resp3 = MagicMock()
    resp3.raise_for_status.return_value = None
    resp3.json.return_value = {"choices": [{"message": {"content": "  "}}]}
    with patch.object(mod.requests, "post", return_value=resp3):
        assert mod.build_ai_summary({"trade_date": "x"}, cfg, "sk-test") is None


# ============================================================
# 4. _build_prompt：字段注入
# ============================================================

def test_build_prompt_contains_key_fields():
    from lihu_quantify.monitor.ai_summary import _build_prompt

    summary = {
        "trade_date": date(2026, 8, 28), "market_state": "上涨",
        "entry_scale": 0.5,
        "total_asset": 101000.0, "cash": 5000.0, "init_capital": 100000.0,
        "prev_total_asset": 100000.0,
        "positions": [{
            "ts_code": "600000.SH", "name": "浦发银行", "volume": 1000,
            "cost": 10.0, "price": 10.5, "float_pnl": 500.0,
            "float_pnl_pct": 0.05, "stop_price": 9.0, "weight": 0.104,
        }],
        "executed": [{"ts_code": "000001.SZ", "name": "平安银行",
                      "price": 20.0, "volume": 400, "stop": 18.0}],
        "sells_today": [{"ts_code": "600000.SH", "name": "浦发银行",
                         "price": 9.9, "volume": 100, "pnl": -110.0,
                         "reason": "止损离场"}],
        "rejected": [
            {"ts_code": "300001.SZ", "reasons": ["铁律1：止损价不低于买入价"]},
            {"ts_code": "300002.SZ", "reasons": ["铁律1：止损价不低于买入价"]},
            {"ts_code": "300003.SZ", "reasons": ["资金不足"]},
        ],
        "pending_stops": [{"ts_code": "600000.SH", "name": "浦发银行",
                           "volume": 1000, "stop_price": 9.0,
                           "reason": "price_stop"}],
        "halted_codes": {"000002.SZ": "2026-09-01"},
        "alerts": [{"level": "warn", "title": "风控拦截", "detail": "x"}],
    }
    prompt = _build_prompt(summary)
    for kw in ("2026-08-28", "上涨", "仓位减半",             # 市场与过滤
               "101,000", "5,000", "+1.00%", "+1,000",      # 账户（总资产/现金/累计/今日盈亏）
               "浦发银行", "10.50", "+5.00%", "止损线 9.00",    # 持仓明细
               "平安银行", "止损离场", "-110",               # 今日操作
               "铁律1：止损价不低于买入价（2 次）",           # 拒绝原因聚合
               "待执行止损", "连亏停手", "风控拦截"):         # 风险
        assert kw in prompt, f"prompt 缺少: {kw}"
    # 空仓场景
    assert "当前空仓" in _build_prompt({"trade_date": "x", "positions": []})


# ============================================================
# 5. .md 报告 / 邮件日报渲染
# ============================================================

def _rich_base() -> dict:
    return {
        "trade_date": date(2026, 8, 28), "market_state": "上涨",
        "signals": 2, "entry_scale": 1.0,
        "total_asset": 100000.0, "cash": 5000.0, "init_capital": 100000.0,
        "prev_total_asset": None,
        "positions": [], "executed": [], "sells_today": [], "rejected": [],
        "pending_stops": [], "halted_codes": {}, "alerts": [],
    }


def test_md_report_ai_section(tmp_path):
    from lihu_quantify.monitor.report import ReportGenerator

    rich = _rich_base() | {"ai_summary": AI_TEXT}
    path = ReportGenerator(tmp_path).daily_report(
        trade_date=rich["trade_date"], market_state="上涨",
        total_asset=100000.0, cash=5000.0,
        positions=[], signals=[], executed=[], rejected=[],
        stop_orders=[], alerts=[], mode="paper", rich=rich,
    )
    content = path.read_text(encoding="utf-8")
    assert "## 八、AI 收盘总结" in content
    assert "持仓点评：持仓平稳" in content and "不构成投资建议" in content

    # None / 缺字段 → 整节省略
    rich2 = _rich_base()
    path2 = ReportGenerator(tmp_path / "sub").daily_report(
        trade_date=date(2026, 8, 29), market_state="上涨",
        total_asset=1.0, cash=1.0, positions=[], signals=[],
        executed=[], rejected=[], stop_orders=[], alerts=[],
        mode="paper", rich=rich2,
    )
    assert "AI 收盘总结" not in path2.read_text(encoding="utf-8")


def test_email_ai_block():
    from lihu_quantify.monitor.daily_report import build_daily_report_email

    d = {"trade_date": "2026-08-28", "market_state": "上涨",
         "total_asset": 100000.0, "mode": "paper", "ai_summary": AI_TEXT}
    _, html = build_daily_report_email(d)
    assert "八、AI 收盘总结" in html
    assert "white-space:pre-wrap" in html            # 灰底引用块（保留四段换行）
    assert "不构成投资建议" in html

    # None / 空 → 整节省略
    d2 = {"trade_date": "2026-08-28", "market_state": "上涨",
          "total_asset": 100000.0, "mode": "paper"}
    _, html2 = build_daily_report_email(d2)
    assert "AI 收盘总结" not in html2


# ============================================================
# 6. 开关一致性（验收标准2：AI 纯展示、不影响决策）
# ============================================================

def test_ai_toggle_does_not_affect_decisions(tmp_path, monkeypatch):
    """同日同场景分别跑 enabled=false/true——signals/executed/rejected/
    资产完全一致；enabled=true 时 ai_summary 有值、false 时为 None。"""
    from test_round8_capital_heatmap import (
        _make_scanner, _run_scan, _sig, _wire_scan,
    )
    from lihu_quantify.monitor import ai_summary as mod

    codes = {"600000.SH": None,
             "000001.SZ": _sig("000001.SZ", 20.0),
             "000002.SZ": _sig("000002.SZ", 20.0)}
    closes = {"600000.SH": 10.0, "000001.SZ": 20.0, "000002.SZ": 20.0}

    def run(enabled: bool, workdir):
        workdir.mkdir(parents=True, exist_ok=True)
        s, broker = _make_scanner(workdir, monkeypatch)
        s.settings.ai_summary = AiSummaryConfig(
            enabled=enabled, api_key="sk-test" if enabled else "")
        broker.set_price("600000.SH", 10.0)
        broker.set_price("000001.SZ", 20.0)
        broker.set_price("000002.SZ", 20.0)
        _wire_scan(workdir, monkeypatch, s, codes, closes=closes)
        return _run_scan(s)

    off = run(False, tmp_path / "off")
    resp = MagicMock()
    resp.raise_for_status.return_value = None
    resp.json.return_value = {"choices": [{"message": {"content": AI_TEXT}}]}
    with patch.object(mod.requests, "post", return_value=resp):
        on = run(True, tmp_path / "on")

    # 决策结果完全一致（AI 调用发生在巡检主体之后）
    for key in ("signals", "executed", "rejected",
                "total_asset", "cash", "sells_today", "pending_stops"):
        assert off[key] == on[key], f"开关改变了决策结果字段: {key}"
    # 展示层差异仅在 ai_summary
    assert off["ai_summary"] is None
    assert on["ai_summary"] == AI_TEXT
