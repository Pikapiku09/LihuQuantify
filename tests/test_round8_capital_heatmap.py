"""第八轮测试：智能资金控制（守卫/top-N）+ 先卖后买顺序修复 + 热力图。

覆盖：
1. _signal_score 信号评分（top-N 排序键）
2. 需求3：先卖后买——当日止损回笼资金同轮复用（与回测口径对齐）
3. 需求1：资金守卫（现金 < 阈值 → 跳过全部买入，汇总一条）
4. 需求1：top-N 筛选（资金紧张 → 按评分只尝试前 N 个）
5. summary["capital"] 资金利用字段 + .md 报告/邮件日报"七、资金利用"
6. 需求2：热力图数据聚合（纯函数）+ /api/heatmap + detail + /api/config
"""
from __future__ import annotations

import json
from datetime import date
from unittest.mock import MagicMock

import pytest


# ============================================================
# 1. 信号评分（纯函数）
# ============================================================

def test_signal_score_bounds():
    from lihu_quantify.monitor.scheduler import _signal_score

    # 满分：量比截断 3、实体满、金叉当日
    assert _signal_score({"vol_ratio": 9.0, "body_ratio": 1.0, "ma5_x_ma10": 0}) == pytest.approx(1.0)
    # 零分：无量、无实体、金叉 7 天
    assert _signal_score({"vol_ratio": 0, "body_ratio": 0, "ma5_x_ma10": 7}) == pytest.approx(0.0)
    # 缺列 / None → 0（不抛异常）
    assert _signal_score({}) == pytest.approx(0.0)
    # 中间值：vol=1.5 → 0.4*0.5=0.2；body=0.5 → 0.15；fresh=0 → 0.3
    assert _signal_score({"vol_ratio": 1.5, "body_ratio": 0.5,
                          "ma5_x_ma10": 0}) == pytest.approx(0.65)


# ============================================================
# _scan_impl 场景测试基建（借鉴 test_daily_report 的 stub 模式）
# ============================================================

def _make_scanner(tmp_path, monkeypatch, *, init: float = 1_000_000.0,
                  capital_guard=None):
    import pandas as pd

    from lihu_quantify.execution.oms import OrderManagementSystem
    from lihu_quantify.execution.paper_trade import PaperBroker
    from lihu_quantify.monitor import scheduler as sched_mod
    from lihu_quantify.monitor.alerts import Alerter
    from lihu_quantify.monitor.scheduler import DailyScanner

    broker = PaperBroker(init_capital=init, persist=False,
                         state_file=str(tmp_path / "state.json"))
    s = DailyScanner.__new__(DailyScanner)
    s.broker = broker
    s.alerter = Alerter()
    s.reporter = MagicMock()
    s.reporter.daily_report.return_value = tmp_path / "report.md"
    s.mode = "paper"
    s.settings = MagicMock()   # isinstance 守卫 → 资金守卫默认不启用
    if capital_guard is not None:
        s.settings.capital_guard = capital_guard

    monkeypatch.setattr(sched_mod, "_ROOT", tmp_path)
    monkeypatch.setattr(sched_mod, "_append_filter_stats", lambda rec: None)
    monkeypatch.setattr(
        sched_mod, "OrderManagementSystem",
        lambda b: OrderManagementSystem(b, registry_file=str(tmp_path / "stop.json")),
    )
    return s, broker


def _wire_scan(tmp_path, monkeypatch, s, signals_by_code: dict,
               closes: dict | None = None, opens: dict | None = None,
               vol_ratios: dict | None = None):
    """接线：client/策略/指标/池/闸门 全 stub。signals_by_code: {code: Signal|None}。"""
    import pandas as pd

    from lihu_quantify.monitor import scheduler as sched_mod

    closes = closes or {}
    opens = opens or {}
    vol_ratios = vol_ratios or {}

    def fake_query(api, params=None, use_cache=True):
        code = params["ts_code"]
        dates = pd.date_range(end="2026-08-28", periods=40, freq="D")
        close = closes.get(code, 10.0)
        return pd.DataFrame({
            "ts_code": code,
            "trade_date": [d.strftime("%Y%m%d") for d in dates],
            "open": opens.get(code, close), "high": close * 1.02,
            "low": close * 0.98, "close": close,
            "vol": 1000.0, "amount": 100000.0,
        })

    s.client = MagicMock()
    s.client.query.side_effect = fake_query

    def latest_signal(df):
        code = str(df["ts_code"].iloc[0])
        return signals_by_code.get(code)

    strategy = MagicMock()
    strategy.latest_signal.side_effect = latest_signal
    monkeypatch.setattr(sched_mod, "CherryClaw", lambda **kw: strategy)

    def add_std(df):
        code = str(df["ts_code"].iloc[0])
        close = closes.get(code, 10.0)
        return df.assign(ma10=close * 0.95, vol_ratio=vol_ratios.get(code, 1.0),
                         body_ratio=0.5, ma5_x_ma10=1)

    monkeypatch.setattr(sched_mod, "add_all_standard", add_std)

    codes = list(signals_by_code.keys())
    monkeypatch.setattr(s, "_universe", lambda n: (codes, {}, {
        c: f"股{c[:6]}" for c in codes}))

    gate = MagicMock()
    gate.check.return_value = MagicMock(approved=True, rejected_items=lambda: [])
    monkeypatch.setattr(sched_mod, "ChecklistGate", lambda **kw: gate)


def _run_scan(s, latest=date(2026, 8, 28)):
    from lihu_quantify.config import RiskConfig, StrategyConfig

    return s._scan_impl(
        latest, "上涨", n=50, days=120,
        s=StrategyConfig(market_filter=False), r=RiskConfig(),
        prev_total_asset=None,
    )


def _sig(code: str, price: float, pct: float = 0.10):
    from lihu_quantify.types import Signal

    return Signal(kind="buy", ts_code=code, suggested_price=price,
                  stop_loss=price * 0.9, suggested_position_pct=pct)


# ============================================================
# 2. 需求3：先卖后买（顺序修复）
# ============================================================

def test_sell_before_buy_reuses_released_cash(tmp_path, monkeypatch):
    """当日止损回笼资金同轮复用（旧顺序会被"资金不足"拒绝）。"""
    s, broker = _make_scanner(tmp_path, monkeypatch, init=100_000.0)
    # 建仓：600000.SH 9900 股 @10 → 现金 ~975（不够买新票一手）
    assert broker.buy("600000.SH", 10.0, 9900).success
    broker.set_price("600000.SH", 9.9)
    # 昨日登记的待执行止损
    (tmp_path / "data").mkdir(parents=True, exist_ok=True)
    (tmp_path / "data" / "pending_stops.json").write_text(
        json.dumps([{"ts_code": "600000.SH", "volume": 9900,
                     "stop_price": 9.0, "reason": "price_stop"}]),
        encoding="utf-8")
    # 信号：000001.SZ @20，invest=0.1*total ≈ 9898 → 400 股 = 8000（>975 旧现金）
    broker.set_price("000001.SZ", 20.0)
    _wire_scan(tmp_path, monkeypatch, s,
               {"600000.SH": None, "000001.SZ": _sig("000001.SZ", 20.0)},
               closes={"600000.SH": 9.9, "000001.SZ": 20.0},
               opens={"600000.SH": 10.0})

    summary = _run_scan(s)

    # 回笼资金被同轮复用：新仓成交（旧顺序下 975 现金必然资金不足）
    buys = [t for t in broker.trades if t["side"] == "buy"
            and t["ts_code"] == "000001.SZ"]
    assert len(buys) == 1
    # 卖出先于买入（trades 顺序）
    idx_sell = next(i for i, t in enumerate(broker.trades) if t["side"] == "sell")
    idx_buy = next(i for i, t in enumerate(broker.trades)
                   if t["side"] == "buy" and t["ts_code"] == "000001.SZ")
    assert idx_sell < idx_buy
    # 资金利用模块数据
    cap = summary["capital"]
    assert cap["released"] > 90000          # 回笼 ~9.89 万
    assert cap["reinvested"] == pytest.approx(8000, rel=1e-3)
    assert cap["guard_skipped"] is False and cap["topn_used"] == 0
    assert isinstance(cap["idle_warn"], bool)


# ============================================================
# 3. 需求1：资金守卫
# ============================================================

def test_capital_guard_skips_buy_attempts(tmp_path, monkeypatch):
    """现金 < 阈值 → 跳过全部买入尝试，仅一条汇总拒绝。"""
    from lihu_quantify.config import CapitalGuardConfig

    s, broker = _make_scanner(
        tmp_path, monkeypatch, init=1_000_000.0,
        capital_guard=CapitalGuardConfig(enabled=True, min_cash_threshold=5000.0),
    )
    # 建仓把现金压到 ~4752（< 5000）
    assert broker.buy("600000.SH", 10.0, 99500).success
    broker.set_price("600000.SH", 10.0)
    broker.set_price("000001.SZ", 20.0)
    _wire_scan(tmp_path, monkeypatch, s,
               {"600000.SH": None,
                "000001.SZ": _sig("000001.SZ", 20.0),
                "000002.SZ": _sig("000002.SZ", 20.0)},
               closes={"600000.SH": 10.0, "000001.SZ": 20.0, "000002.SZ": 20.0})

    summary = _run_scan(s)

    assert summary["executed"] == []                      # 无买入
    guard_rejects = [r for r in summary["rejected"] if "资金守卫" in r["reasons"]]
    assert len(guard_rejects) == 1                        # 仅一条汇总
    assert "000001.SZ" not in json.dumps(summary["rejected"], ensure_ascii=False)
    assert summary["capital"]["guard_skipped"] is True
    # 无新买入成交（trades 不增加 buy）
    assert not [t for t in broker.trades if t["side"] == "buy"
                and t["ts_code"] != "600000.SH"]


# ============================================================
# 4. 需求1：top-N 筛选
# ============================================================

def test_top_n_keeps_best_scored(tmp_path, monkeypatch):
    """资金紧张（3 过预筛 > 1 槽）→ 按评分只尝试前 2 个。"""
    from lihu_quantify.config import CapitalGuardConfig

    s, broker = _make_scanner(
        tmp_path, monkeypatch, init=1_000_000.0,
        capital_guard=CapitalGuardConfig(enabled=True, min_cash_threshold=1000.0,
                                         top_n_enabled=True, top_n=2),
    )
    # 现金 ~29.99 万，budget=25 万 → slots=1；3 个信号 > 1 → 触发 top-N
    assert broker.buy("600000.SH", 10.0, 70000).success
    broker.set_price("600000.SH", 10.0)
    for c in ("AAA001.SZ", "BBB002.SZ", "CCC003.SZ"):
        broker.set_price(c, 20.0)
    _wire_scan(
        tmp_path, monkeypatch, s,
        {"600000.SH": None,
         "AAA001.SZ": _sig("AAA001.SZ", 20.0, pct=0.12),
         "BBB002.SZ": _sig("BBB002.SZ", 20.0, pct=0.12),
         "CCC003.SZ": _sig("CCC003.SZ", 20.0, pct=0.12)},
        closes={"600000.SH": 10.0, "AAA001.SZ": 20.0,
                "BBB002.SZ": 20.0, "CCC003.SZ": 20.0},
        vol_ratios={"AAA001.SZ": 3.0, "BBB002.SZ": 2.0, "CCC003.SZ": 1.0},
    )

    summary = _run_scan(s)

    cap = summary["capital"]
    assert cap["topn_used"] == 2 and cap["topn_skipped"] == 1
    # 只尝试了评分最高的 2 只（AAA vol=3 最高、BBB 次之；CCC 未尝试）
    assert len(summary["executed"]) == 2
    assert [e["ts_code"] for e in summary["executed"]] == ["AAA001.SZ", "BBB002.SZ"]
    # CCC 不出现在任何逐票记录（Top-N 后未尝试）
    assert "CCC003.SZ" not in json.dumps(summary["rejected"], ensure_ascii=False)
    assert any("Top-N" in r["reasons"] for r in summary["rejected"])


# ============================================================
# 5. 报告渲染：七、资金利用（.md + 邮件）
# ============================================================

def _capital_summary() -> dict:
    return {
        "released": 98901.0, "reinvested": 8000.0, "idle_cash": 52975.0,
        "budget": 25000.0, "idle_warn": True,
        "guard_skipped": False, "topn_used": 2, "topn_skipped": 1,
    }


def test_md_report_capital_module(tmp_path):
    from lihu_quantify.monitor.report import ReportGenerator

    rich = {
        "trade_date": date(2026, 8, 28), "market_state": "上涨",
        "signals": 3, "entry_scale": 1.0,
        "total_asset": 100000.0, "cash": 52975.0, "init_capital": 100000.0,
        "prev_total_asset": None,
        "positions": [], "executed": [], "sells_today": [], "rejected": [],
        "pending_stops": [], "halted_codes": {}, "alerts": [],
        "capital": _capital_summary(),
    }
    path = ReportGenerator(tmp_path).daily_report(
        trade_date=rich["trade_date"], market_state="上涨",
        total_asset=100000.0, cash=52975.0,
        positions=[], signals=[], executed=[], rejected=[],
        stop_orders=[], alerts=[], mode="paper", rich=rich,
    )
    content = path.read_text(encoding="utf-8")
    assert "七、资金利用" in content
    assert "98,901" in content and "8,000" in content     # 回笼/再投资
    assert "Top-N 筛选" in content and "保留 2 个" in content
    assert "闲置现金 52,975" in content                    # idle_warn 提示
    # 无 capital 字段 → 模块不渲染（旧数据兼容）
    rich.pop("capital")
    path2 = ReportGenerator(tmp_path / "sub").daily_report(
        trade_date=date(2026, 8, 29), market_state="上涨",
        total_asset=1.0, cash=1.0, positions=[], signals=[],
        executed=[], rejected=[], stop_orders=[], alerts=[],
        mode="paper", rich=rich,
    )
    assert "七、资金利用" not in path2.read_text(encoding="utf-8")


def test_email_capital_module():
    from lihu_quantify.monitor.daily_report import build_daily_report_email

    d = {"trade_date": "2026-08-28", "market_state": "上涨",
         "total_asset": 100000.0, "mode": "paper",
         "capital": _capital_summary()}
    _, html = build_daily_report_email(d)
    assert "七、资金利用" in html
    assert "98,901" in html and "Top-N" in html
    assert "闲置现金" in html


# ============================================================
# 6. 热力图（纯函数 + 端点）
# ============================================================

def _make_cache(tmp_path):
    """造 Tushare 响应格式缓存：2 只最新日 + 1 只陈旧日（应被过滤）。"""
    cache = tmp_path / "cache"
    cache.mkdir(parents=True, exist_ok=True)

    def write(code: str, safe: str, dates_pct: list[tuple[str, float]]):
        items = [[code, d, 10.0, 10.5, 9.5, 10.0, p, 1000.0, 500000.0 + i * 1000]
                 for i, (d, p) in enumerate(dates_pct)]
        resp = {"code": 0, "data": {
            "fields": ["ts_code", "trade_date", "open", "high", "low",
                       "close", "pct_chg", "vol", "amount"],
            "items": items}}
        (cache / f"daily_{safe}_20260828_20260429.json").write_text(
            json.dumps(resp), encoding="utf-8")

    write("600000.SH", "600000_SH", [("20260827", 1.5), ("20260828", 3.2)])
    write("000001.SZ", "000001_SZ", [("20260827", -1.0), ("20260828", -2.5)])
    write("300750.SZ", "300750_SZ", [("20260826", 5.0), ("20260827", 1.0)])
    return cache


def test_heatmap_rows_latest_day_only(tmp_path, monkeypatch):
    import web.server as ws

    monkeypatch.setattr(ws, "DATA_DIR", tmp_path)
    _make_cache(tmp_path)
    rows = ws._heatmap_rows()
    # 陈旧缓存（止于 0827）被过滤，仅保留最新交易日 2 只
    assert {r["ts_code"] for r in rows} == {"600000.SH", "000001.SZ"}
    top = rows[0]   # 成交额大者在前
    assert top["ts_code"] == "000001.SZ"
    assert top["pct_chg"] == -2.5 and top["trade_date"] == "20260828"
    # 行业映射：无 DuckDB → 降级"未知"，名称回退代码
    assert all(r["industry"] in ("未知",) or r["industry"] for r in rows)


def test_heatmap_endpoint_dims_and_detail(tmp_path, monkeypatch):
    fastapi_testclient = pytest.importorskip("fastapi.testclient")
    import web.server as ws

    monkeypatch.setattr(ws, "DATA_DIR", tmp_path)
    _make_cache(tmp_path)
    client = fastapi_testclient.TestClient(ws.app)

    # 默认行业维度（无行业数据 → 全部"未知"组）
    r = client.get("/api/heatmap")
    assert r.status_code == 200
    d = r.json()
    assert d["trade_date"] == "20260828" and d["count"] == 2
    leaves = [l for g in d["children"] for l in g["children"]]
    assert {l["code"] for l in leaves} == {"600000.SH", "000001.SZ"}

    # 涨跌幅维度：+3.2 → "⑤ 上涨 2~5%"；-2.5 → "② 下跌 -5%~-2%"
    r = client.get("/api/heatmap?dim=pct_bucket")
    groups = {g["name"] for g in r.json()["children"]}
    assert any("⑤ 上涨" in g for g in groups)
    assert any("② 下跌" in g for g in groups)

    # 非法维度
    assert client.get("/api/heatmap?dim=xxx").status_code == 400

    # 详情：近 10 日（本缓存 2 行）
    r = client.get("/api/heatmap/detail?code=600000.SH")
    assert r.status_code == 200
    detail = r.json()
    assert len(detail["bars"]) == 2
    assert detail["bars"][-1]["pct_chg"] == 3.2
    # 不在池内
    assert client.get("/api/heatmap/detail?code=999999.SH").status_code == 404


def test_config_endpoint(tmp_path, monkeypatch):
    fastapi_testclient = pytest.importorskip("fastapi.testclient")
    import web.server as ws

    client = fastapi_testclient.TestClient(ws.app)
    r = client.get("/api/config")
    assert r.status_code == 200
    d = r.json()
    assert "capital_guard" in d and "heatmap" in d
    assert d["capital_guard"]["top_n"] == 5
    # 脱敏：不含 token
    assert "token" not in json.dumps(d)
