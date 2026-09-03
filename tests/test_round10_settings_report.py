"""第十轮测试：系统设置读写 + Markdown 表格渲染（vendor）+ 每日简报。

覆盖：
1. GET /api/settings（元数据/分组/冻结锁/密钥掩码）
2. POST /api/settings（confirm 防误触、冻结键 400、原子写保留注释、审计 jsonl）
3. secrets.json 解析链（config.py：secrets → env/yaml → 空）
4. 需求5：brief_rule 规则版兜底（AI 失败永不为空）+ dashboard brief + 邮件简报块
"""
from __future__ import annotations

import json

import pytest


# ============================================================
# 1/2. 设置读写 API（元数据 + 确认 + 原子写 + 审计）
# ============================================================

_YAML = """# LihuQuantify 全局配置（测试副本）
ai_summary:
  enabled: true                        # 注释必须保留
heartbeat:
  healthchecks_url: ""                 # 例 https://hc-ping.com/<uuid>
alert:
  serverchan_key: ""
  email:
    enabled: false                     # 总开关
heatmap:
  enabled: true
capital_guard:
  enabled: false                       # 冻结期关闭
  top_n_enabled: false
"""


def _setup_web(tmp_path, monkeypatch):
    import web.server as ws

    root = tmp_path / "proj"
    (root / "config").mkdir(parents=True, exist_ok=True)
    (root / "config" / "settings.yaml").write_text(_YAML, encoding="utf-8")
    (root / "data").mkdir(exist_ok=True)
    monkeypatch.setattr(ws, "ROOT", root)
    monkeypatch.setattr(ws, "DATA_DIR", root / "data")
    return ws, root


def test_settings_get_metadata(tmp_path, monkeypatch):
    fastapi_testclient = pytest.importorskip("fastapi.testclient")
    ws, _ = _setup_web(tmp_path, monkeypatch)
    client = fastapi_testclient.TestClient(ws.app)

    r = client.get("/api/settings")
    assert r.status_code == 200
    d = r.json()
    meta = d["metadata"]
    # 可编辑键带元数据（label/建议/分组）
    assert meta["ai_summary.enabled"]["group"] == "AI 总结"
    assert "建议开启" in meta["heatmap.enabled"]["recommendation"]
    assert meta["capital_guard.enabled"]["badge"] == "warn"
    # 冻结期锁
    for k in ("strategy", "risk", "universe"):
        assert meta[k]["editable"] is False
    assert d["settings"]["ai_summary.enabled"] is True
    # 密钥未配置 → 掩码为空（不报错）
    assert d["secrets"]["ai_summary_api_key"] == ""


def test_settings_post_requires_confirm(tmp_path, monkeypatch):
    fastapi_testclient = pytest.importorskip("fastapi.testclient")
    ws, _ = _setup_web(tmp_path, monkeypatch)
    client = fastapi_testclient.TestClient(ws.app)

    r = client.post("/api/settings", json={"changes": {"heatmap.enabled": False}})
    assert r.status_code == 400
    assert "confirm" in r.json()["detail"]
    # 无变更
    r = client.post("/api/settings", json={"confirm": True, "changes": {}})
    assert r.status_code == 400


def test_settings_post_locked_rejected(tmp_path, monkeypatch):
    """冻结期硬约束：strategy/risk/universe 一律 400。"""
    fastapi_testclient = pytest.importorskip("fastapi.testclient")
    ws, _ = _setup_web(tmp_path, monkeypatch)
    client = fastapi_testclient.TestClient(ws.app)

    r = client.post("/api/settings", json={
        "confirm": True, "changes": {"strategy.market_filter": False}})
    assert r.status_code == 400
    assert "冻结期锁定" in r.json()["detail"]
    r = client.post("/api/settings", json={
        "confirm": True, "changes": {"risk.stop_loss_force": -0.1}})
    assert r.status_code == 400


def test_settings_post_atomic_write_and_audit(tmp_path, monkeypatch):
    fastapi_testclient = pytest.importorskip("fastapi.testclient")
    ws, root = _setup_web(tmp_path, monkeypatch)
    client = fastapi_testclient.TestClient(ws.app)

    r = client.post("/api/settings", json={
        "confirm": True,
        "changes": {"ai_summary.enabled": False,
                    "heartbeat.healthchecks_url": "https://hc-ping.com/abc"},
        "secrets": {"ai_summary_api_key": "sk-test-123456789"},
    })
    assert r.status_code == 200
    d = r.json()
    assert d["ok"] is True and "下次巡检" in d["effective"]

    # yaml：值已改、注释保留、无 .tmp 残留
    text = (root / "config" / "settings.yaml").read_text(encoding="utf-8")
    assert "enabled: false" in text and "# 注释必须保留" in text
    assert '"https://hc-ping.com/abc"' in text
    assert not list((root / "config").glob("*.tmp"))

    # 重读生效
    d2 = client.get("/api/settings").json()
    assert d2["settings"]["ai_summary.enabled"] is False
    assert d2["settings"]["heartbeat.healthchecks_url"] == "https://hc-ping.com/abc"

    # secrets.json 已写 + GET 掩码（不回显明文）
    sec = json.loads((root / "data" / "secrets.json").read_text(encoding="utf-8"))
    assert sec["ai_summary_api_key"] == "sk-test-123456789"
    assert "sk-test-123456789" not in json.dumps(d2)
    assert d2["secrets"]["ai_summary_api_key"].startswith("sk-")

    # 审计：改动项 + 旧值→新值；密钥只记掩码
    lines = (root / "data" / "settings_history.jsonl").read_text(
        encoding="utf-8").strip().splitlines()
    recs = [json.loads(x) for x in lines]
    by_item = {r["item"]: r for r in recs}
    assert by_item["ai_summary.enabled"]["old"] is True
    assert by_item["ai_summary.enabled"]["new"] is False
    assert by_item["secrets.ai_summary_api_key"]["old"] == "***"
    assert "sk-test-123456789" not in json.dumps(recs, ensure_ascii=False)
    assert all("ts" in r for r in recs)


def test_yaml_set_value_preserves_structure():
    """行级手术：嵌套键定位准确（同名 enabled 多处不串）。"""
    from web.server import _yaml_set_value

    text = _YAML
    new, hit = _yaml_set_value(text, "alert.email.enabled", True)
    assert hit is True
    assert "    enabled: true                     # 总开关" in new
    # 其它 enabled（ai_summary/heatmap/capital_guard）不受影响
    assert "enabled: true                        # 注释必须保留" in new
    _, miss = _yaml_set_value(text, "strategy.market_filter", False)
    assert miss is False   # 不存在的键 → 未命中


# ============================================================
# 3. secrets 解析链（config.py）
# ============================================================

def test_secrets_resolution_order(tmp_path, monkeypatch):
    from lihu_quantify.config import AiSummaryConfig, Settings, read_secrets

    monkeypatch.chdir(tmp_path)
    (tmp_path / "data").mkdir(exist_ok=True)

    # 无 secrets → 回退 env/yaml 注入值
    st = Settings(ai_summary=AiSummaryConfig(api_key="sk-env"))
    assert st.resolved_ai_summary_api_key() == "sk-env"

    # 写 secrets → 优先
    (tmp_path / "data" / "secrets.json").write_text(
        json.dumps({"ai_summary_api_key": "sk-file", "email_auth_code": "cd"}),
        encoding="utf-8")
    assert read_secrets() == {"ai_summary_api_key": "sk-file",
                              "email_auth_code": "cd"}
    assert Settings(ai_summary=AiSummaryConfig(api_key="sk-env")) \
        .resolved_ai_summary_api_key() == "sk-file"

    # 损坏文件 → 空 dict 降级（不抛异常）
    (tmp_path / "data" / "secrets.json").write_text("{bad json", encoding="utf-8")
    assert read_secrets() == {}


# ============================================================
# 4. 需求5：规则版简报兜底 + dashboard/邮件
# ============================================================

def test_scan_brief_rule_fallback(tmp_path, monkeypatch):
    """AI 未配置（MagicMock settings → build_ai_summary 返回 None）→
    brief = 规则版，永不为空且含关键数字。"""
    from test_round8_capital_heatmap import (
        _make_scanner, _run_scan, _sig, _wire_scan,
    )

    s, broker = _make_scanner(tmp_path, monkeypatch, init=100_000.0)
    _wire_scan(tmp_path, monkeypatch, s,
               {"600000.SH": None, "000001.SZ": _sig("000001.SZ", 20.0)},
               closes={"600000.SH": 10.0, "000001.SZ": 20.0})
    summary = _run_scan(s)

    assert summary["ai_summary"] is None            # AI 静默降级
    assert summary["brief"]                          # 规则版兜底非空
    assert summary["brief"].startswith("今日巡检完成")
    for kw in ("信号", "拦截", "市场", "总资产", "持仓"):
        assert kw in summary["brief"]
    assert "brief_rule" not in summary               # 已消费，避免重复字段


def test_dashboard_brief_fields(tmp_path, monkeypatch):
    fastapi_testclient = pytest.importorskip("fastapi.testclient")
    import web.server as ws

    monkeypatch.setattr(ws, "DATA_DIR", tmp_path)
    (tmp_path / "last_scan.json").write_text(json.dumps({
        "trade_date": "2026-08-28",
        "summary": {"trade_date": "2026-08-28", "ai_summary": None,
                    "brief": "今日巡检完成：3 信号，1 成交；总资产 100,000。"},
    }, ensure_ascii=False), encoding="utf-8")
    client = fastapi_testclient.TestClient(ws.app)

    d = client.get("/api/dashboard").json()
    assert d["brief"].startswith("今日巡检完成")
    assert d["brief_is_ai"] is False                 # 规则版标记
    # 旧数据（无 brief）→ 回退 ai_summary 字段，不报错
    (tmp_path / "last_scan.json").write_text(json.dumps({
        "trade_date": "2026-08-27",
        "summary": {"trade_date": "2026-08-27",
                    "ai_summary": "旧版 AI 文本"},
    }, ensure_ascii=False), encoding="utf-8")
    d2 = client.get("/api/dashboard").json()
    assert d2["brief"] is None and d2["ai_summary"] == "旧版 AI 文本"


def test_email_brief_block():
    """邮件顶部简报块：AI 版/规则版标签；旧数据无 brief → 整块省略。"""
    from lihu_quantify.monitor.daily_report import build_daily_report_email

    base = {"trade_date": "2026-08-28", "market_state": "上涨",
            "total_asset": 100000.0, "mode": "paper"}
    # 规则版（AI 失败）
    _, html = build_daily_report_email(
        {**base, "brief": "今日巡检完成：3 信号，0 成交。", "ai_summary": None})
    assert "今日简报" in html and "规则版" in html
    assert "今日巡检完成" in html
    # AI 版
    _, html2 = build_daily_report_email(
        {**base, "brief": "AI 总结正文。", "ai_summary": "AI 总结正文。"})
    assert "AI 版" in html2
    # 旧数据：无 brief 且无 ai → 不出现简报块
    _, html3 = build_daily_report_email(dict(base))
    assert "今日简报" not in html3


# ============================================================
# 5. 需求4：marked vendor 存在（离线表格渲染的前置）
# ============================================================

def test_marked_vendor_present():
    from pathlib import Path

    p = Path(__file__).resolve().parents[1] / "web" / "static" / "vendor" / "marked.min.js"
    assert p.exists(), "marked.min.js 缺失（报告表格渲染依赖 vendor 离线文件）"
    assert p.stat().st_size > 10_000   # 完整压缩版约 40~50KB


# ============================================================
# 6. 需求1：配置热生效（scan() 前重载 settings.yaml + secrets）
# ============================================================

def test_reload_settings_hot_effect(tmp_path, monkeypatch):
    from lihu_quantify.config import Settings, load_yaml_config
    from lihu_quantify.monitor.scheduler import DailyScanner

    yaml_path = tmp_path / "settings.yaml"
    yaml_path.write_text(
        "ai_summary:\n  enabled: false\nheartbeat:\n  healthchecks_url: \"\"\n",
        encoding="utf-8")

    s = DailyScanner.__new__(DailyScanner)
    s._settings_path = str(yaml_path)
    s.settings = Settings(**load_yaml_config(str(yaml_path)))
    from lihu_quantify.monitor.alerts import Alerter
    from lihu_quantify.monitor.heartbeat import Heartbeat
    s.alerter = Alerter()          # 就地更新的对象（不应被替换）
    s.heartbeat = Heartbeat("")
    alerter_id, heartbeat_id = id(s.alerter), id(s.heartbeat)

    # 改 yaml（等价设置页保存）→ 热载生效
    yaml_path.write_text(
        "ai_summary:\n  enabled: true\nheartbeat:\n"
        "  healthchecks_url: \"https://hc-ping.com/x\"\n",
        encoding="utf-8")
    s._reload_settings()

    assert s.settings.ai_summary.enabled is True
    assert s.heartbeat.url == "https://hc-ping.com/x"
    # 对象未替换（保留告警历史/测试注入 mock）
    assert id(s.alerter) == alerter_id and id(s.heartbeat) == heartbeat_id

    # 坏 yaml → 沿用旧配置不抛异常
    yaml_path.write_text("{bad", encoding="utf-8")
    s._reload_settings()
    assert s.settings.ai_summary.enabled is True

    # stub 守卫：无 _settings_path / MagicMock settings → 直接跳过
    s2 = DailyScanner.__new__(DailyScanner)
    s2._reload_settings()   # 不抛异常即通过
    from unittest.mock import MagicMock

    s3 = DailyScanner.__new__(DailyScanner)
    s3._settings_path = str(yaml_path)
    s3.settings = MagicMock()
    s3._reload_settings()
    assert isinstance(s3.settings, MagicMock)   # 未被替换
