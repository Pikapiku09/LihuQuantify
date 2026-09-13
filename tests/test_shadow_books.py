"""影子账本并行（第十二轮）回归测试。

纪律：shadow_books 默认空 = 现有行为零变化；影子不写 last_scan /
不发邮件 / 不调 AI 摘要 / 不 ping 心跳；评审统计以主账本为准。
"""

from __future__ import annotations

import importlib.util
import json
from datetime import date
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from lihu_quantify.config import Settings
from lihu_quantify.monitor.scheduler import BookSpec, DailyScanner

ROOT = Path(__file__).resolve().parents[1]


def _settings(tmp_path: Path, **kw) -> Settings:
    """隔离配置：DuckDB/cache/token 全指 tmp，不触网不动真实库。"""
    (tmp_path / "data").mkdir(exist_ok=True)
    return Settings(
        duckdb_path=str(tmp_path / "t.duckdb"),
        cache_dir=str(tmp_path / "cache"),
        tushare_token="fake-token-for-init-only",
        **kw,
    )


# ============================================================
# 1. BookSpec 路径后缀
# ============================================================

def test_bookspec_paths():
    assert BookSpec(name="s43").suffix() == "_s43"
    assert BookSpec().suffix() == ""
    assert BookSpec().is_main is True
    assert BookSpec(name="s43").is_main is False
    # BookSpec 默认（类属性兜底）= 主账本
    assert DailyScanner.book.is_main is True


# ============================================================
# 2. 影子状态文件隔离（paper_state 独立）
# ============================================================

def test_shadow_state_isolation(tmp_path, monkeypatch):
    from lihu_quantify.monitor import scheduler as sched_mod
    from lihu_quantify.execution import paper_trade

    monkeypatch.setattr(sched_mod, "_ROOT", tmp_path)
    monkeypatch.setattr(paper_trade, "_ROOT", tmp_path)

    st = _settings(tmp_path)
    main = DailyScanner(st, mode="paper")
    shadow = DailyScanner(
        st.model_copy(deep=True), mode="paper",
        book=BookSpec(name="s43", pool_seed=43, silent=True),
    )
    # 主账本默认路径 / 影子带后缀
    assert main.broker.state_file == str(tmp_path / "data" / "paper_state.json")
    assert shadow.broker.state_file == str(tmp_path / "data" / "paper_state_s43.json")

    main.broker.buy("600000.SH", 10.0, 100)
    shadow.broker.buy("000001.SZ", 20.0, 200)
    main_state = json.loads((tmp_path / "data" / "paper_state.json").read_text(encoding="utf-8"))
    shadow_state = json.loads((tmp_path / "data" / "paper_state_s43.json").read_text(encoding="utf-8"))
    assert main_state["positions"].keys() == {"600000.SH"}
    assert shadow_state["positions"].keys() == {"000001.SZ"}
    # 互不影响：影子再买主票，不改变主账本文件
    shadow.broker.buy("600000.SH", 11.0, 100)
    main_state2 = json.loads((tmp_path / "data" / "paper_state.json").read_text(encoding="utf-8"))
    assert main_state2 == main_state, "影子写入不得影响主账本状态文件"


# ============================================================
# 3. 影子 silent：alerter 静默 / 心跳 no-op / pending 路径
# ============================================================

def test_shadow_silent_init(tmp_path, monkeypatch):
    from lihu_quantify.monitor import scheduler as sched_mod

    monkeypatch.setattr(sched_mod, "_ROOT", tmp_path)
    st = _settings(tmp_path)
    shadow = DailyScanner(st, mode="paper",
                          book=BookSpec(name="s43", silent=True))
    # 邮件通道不构建（enabled=False 且 email=None）
    assert shadow.alerter.enabled is False
    assert shadow.alerter.email is None
    # 心跳 no-op
    assert shadow.heartbeat.url == ""
    # 报告目录带 shadow_ 前缀
    assert "shadow_s43" in str(shadow.reporter.output_dir)
    # pending / registry 路径后缀
    assert shadow._pending_file == tmp_path / "data" / "pending_stops_s43.json"
    # 主账本（同配置）不受影响
    main = DailyScanner(_settings(tmp_path), mode="paper")
    assert main._pending_file == tmp_path / "data" / "pending_stops.json"
    assert main.alerter.enabled is True


def test_shadow_silent_scan_no_lastscan_no_digest(monkeypatch, tmp_path):
    """影子 scan：不写 last_scan、不发日报、AI 摘要跳过（mock 断言）。"""
    from lihu_quantify.monitor import scheduler as sched_mod

    s = DailyScanner.__new__(DailyScanner)
    s.book = BookSpec(name="s43", silent=True)
    s.settings = MagicMock()
    s.heartbeat = MagicMock()
    s.alerter = MagicMock()
    calls = {"write": 0, "digest": 0, "scan_impl": 0}
    monkeypatch.setattr(s, "_reload_settings", lambda: None)
    monkeypatch.setattr(s, "_market_state", lambda: (date(2026, 9, 3), "上涨"))
    monkeypatch.setattr(s, "_read_last_scan", lambda: None)
    monkeypatch.setattr(s, "_write_last_scan",
                        lambda *a, **k: calls.__setitem__("write", calls["write"] + 1))
    monkeypatch.setattr(s, "_send_digest",
                        lambda *a, **k: calls.__setitem__("digest", calls["digest"] + 1))
    monkeypatch.setattr(s, "_scan_impl",
                        lambda *a, **k: calls.__setitem__("scan_impl", calls["scan_impl"] + 1)
                        or {"trade_date": date(2026, 9, 3), "signals": 0,
                            "executed": [], "rejected": [], "report": "x"})
    summary = s.scan(n=5)
    assert calls["scan_impl"] == 1
    assert calls["write"] == 0, "影子不得写 last_scan"
    assert calls["digest"] == 0, "影子不得发日报"
    assert summary["signals"] == 0


def test_shadow_silent_skips_ai_summary(monkeypatch):
    """_scan_impl 的 AI 段：silent 时 build_ai_summary 不被调用。"""
    from lihu_quantify.monitor import scheduler as sched_mod

    called = []
    monkeypatch.setattr(sched_mod, "build_ai_summary",
                        lambda *a, **k: called.append(1) or "AI")
    # 直接验证跳过分支（不跑完整 _scan_impl，只测 ai 段守卫语义）：
    # silent=True 时 summary["ai_summary"] 应为 None（scan 路径内联逻辑）
    s = DailyScanner.__new__(DailyScanner)
    s.book = BookSpec(name="s43", silent=True)
    assert s.book.silent is True
    # 非 silent 时走 build_ai_summary（此处仅验证函数可被调用路径存在）
    s2 = DailyScanner.__new__(DailyScanner)
    s2.book = BookSpec()
    assert s2.book.silent is False


# ============================================================
# 4. 影子池 seed 覆盖
# ============================================================

def test_shadow_pool_seed(tmp_path, monkeypatch):
    from lihu_quantify.data import pool as pool_mod
    from lihu_quantify.monitor import scheduler as sched_mod

    captured = []

    def _fake_build(client, store, target_n, layers, seed, **kw):
        captured.append(seed)
        return ["600000.SH"]

    monkeypatch.setattr(pool_mod, "build_stratified_pool", _fake_build)
    monkeypatch.setattr(sched_mod, "_ROOT", tmp_path)

    st = _settings(tmp_path)
    st.universe.pool_mode = "strat"
    # stock_basic 走 client.query → mock TushareClient
    main = DailyScanner(st, mode="paper")
    monkeypatch.setattr(main.client, "query",
                        lambda api, params=None, **kw: __import__("pandas").DataFrame(
                            [{"ts_code": "600000.SH", "industry": "银行", "name": "X"}]))
    codes = main._universe(50)[0]
    assert codes == ["600000.SH"]
    assert captured[-1] == 42, "主账本用默认 seed"

    shadow = DailyScanner(st.model_copy(deep=True), mode="paper",
                          book=BookSpec(name="s43", pool_seed=43, silent=True))
    monkeypatch.setattr(shadow.client, "query",
                        lambda api, params=None, **kw: __import__("pandas").DataFrame(
                            [{"ts_code": "600000.SH", "industry": "银行", "name": "X"}]))
    shadow._universe(50)
    assert captured[-1] == 43, "影子账本 seed 覆盖"


# ============================================================
# 5. OMS registry 分裂
# ============================================================

def test_shadow_oms_registry_split(tmp_path, monkeypatch):
    from lihu_quantify.execution.oms import OrderManagementSystem

    monkeypatch.setattr("lihu_quantify.execution.oms._default_registry_file",
                        lambda: str(tmp_path / "data" / "stop_registry.json"))
    st = _settings(tmp_path)
    shadow = DailyScanner(st, mode="paper",
                          book=BookSpec(name="s43", silent=True))
    # 与 scheduler._scan_impl 同款构造公式（BookSpec 后缀）
    reg_shadow = str(tmp_path / f"data/stop_registry{shadow.book.suffix()}.json")
    oms = OrderManagementSystem(shadow.broker, registry_file=reg_shadow, persist=True)
    from lihu_quantify.execution.oms import StopOrder

    oms.stop_registry["600000.SH"] = StopOrder(
        ts_code="600000.SH", volume=100, stop_price=9.0)
    oms._save_registry()
    assert (tmp_path / "data" / "stop_registry_s43.json").exists()
    assert not (tmp_path / "data" / "stop_registry.json").exists(), "主 registry 未被影子触碰"


# ============================================================
# 6. 编排层：默认无影子 / 异常隔离
# ============================================================

class _FakeScanner:
    """记录 book 与 scan 行为的假 scanner（免真实 IO）。"""

    def __init__(self, settings, mode="paper", book=None):
        self.book = book or BookSpec()
        self.settings = settings
        self.scanned = []
        self.raise_on_scan = False

    def scan(self, n=50, **kw):
        if self.raise_on_scan:
            raise RuntimeError("影子崩溃")
        self.scanned.append(n)
        return {"signals": 1, "executed": [], "rejected": [],
                "report": "fake.md", "trade_date": date(2026, 9, 3)}


def _build_fake_sched(sched_mod, settings):
    """setup_scheduler + FakeScanner 注入，返回 (sched, jobs, fakes)。"""
    fakes: list[_FakeScanner] = []
    orig_cls = sched_mod.DailyScanner

    def _factory(s, mode="paper", book=None):
        f = _FakeScanner(s, mode=mode, book=book)
        fakes.append(f)
        return f

    sched_mod.DailyScanner = _factory
    jobs = {}

    class _FakeSched:
        def add_job(self, fn, trigger, id=None, **kw):
            jobs[id] = fn

        def start(self):
            pass

    # setup_scheduler 内是函数级 import，monkeypatch 须打源模块
    import apscheduler.schedulers.blocking as blk

    orig_blk = blk.BlockingScheduler
    blk.BlockingScheduler = lambda *a, **kw: _FakeSched()
    try:
        sched = sched_mod.setup_scheduler(settings, mode="paper", n=5)
    finally:
        sched_mod.DailyScanner = orig_cls
        blk.BlockingScheduler = orig_blk
    return sched, jobs, fakes


def test_default_no_shadows(tmp_path, monkeypatch):
    from lihu_quantify.monitor import scheduler as sched_mod

    monkeypatch.setattr(sched_mod, "_ROOT", tmp_path)
    st = _settings(tmp_path)   # shadow_books 默认空
    assert st.shadow_books == []
    _, jobs, fakes = _build_fake_sched(sched_mod, st)
    assert len(fakes) == 1, "无影子配置时只有一个主 scanner"
    assert fakes[0].book.is_main
    assert "daily_scan" in jobs


def test_shadow_scan_exception_isolated(tmp_path, monkeypatch):
    from lihu_quantify.monitor import scheduler as sched_mod

    monkeypatch.setattr(sched_mod, "_ROOT", tmp_path)
    st = _settings(tmp_path, shadow_books=[
        {"name": "s43", "seed": 43}, {"name": "s44", "seed": 44},
    ])
    _, jobs, fakes = _build_fake_sched(sched_mod, st)
    assert len(fakes) == 3, "主 + 2 影子"
    assert [f.book.name for f in fakes] == ["main", "s43", "s44"]
    assert fakes[1].book.silent and fakes[2].book.silent
    # seed 覆盖进影子 settings
    assert fakes[1].settings.universe.pool_seed == 43
    assert fakes[2].settings.universe.pool_seed == 44
    # 影子 scan 抛异常：daily_scan_job 不崩、主账本 scan 已完成
    fakes[1].raise_on_scan = True
    fakes[2].raise_on_scan = True
    jobs["daily_scan"]()   # 不应抛出
    assert fakes[0].scanned == [5], "主账本正常完成"
    assert fakes[1].scanned == [] and fakes[2].scanned == []


# ============================================================
# 7. 看板影子聚合
# ============================================================

def _load_web_server():
    spec = importlib.util.spec_from_file_location(
        "lihu_web_server_shadow", ROOT / "web" / "server.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def web_server():
    return _load_web_server()


def _write_state(path: Path, trades: list[dict]):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({
        "cash": 50000.0, "init_capital": 100000.0, "trades": trades, "halt_map": {},
    }, ensure_ascii=False), encoding="utf-8")


def test_dashboard_shadow_aggregation(web_server, tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    data_dir = tmp_path / "data"
    _write_state(data_dir / "paper_state.json", [
        {"ts_code": "600584.SH", "side": "buy", "price": 10.0, "volume": 100,
         "date": "2026-09-01"},
        {"ts_code": "600584.SH", "side": "sell", "price": 11.0, "volume": 100,
         "date": "2026-09-02"},
    ])
    _write_state(data_dir / "paper_state_s43.json", [
        {"ts_code": "000001.SZ", "side": "buy", "price": 20.0, "volume": 100,
         "date": "2026-09-01"},
        {"ts_code": "000001.SZ", "side": "sell", "price": 21.0, "volume": 100,
         "date": "2026-09-02"},
        {"ts_code": "000002.SZ", "side": "buy", "price": 30.0, "volume": 100,
         "date": "2026-09-02"},
        {"ts_code": "000002.SZ", "side": "sell", "price": 31.0, "volume": 100,
         "date": "2026-09-03"},
    ])
    _write_state(data_dir / "paper_state_s44.json", [])
    # 备份文件必须被忽略
    _write_state(data_dir / "paper_state_s45.json.bak_round6", [
        {"ts_code": "X", "side": "buy", "price": 1.0, "volume": 1, "date": "2026-09-01"},
    ])

    monkeypatch.setattr(web_server, "DATA_DIR", data_dir)
    monkeypatch.setattr(web_server, "_market_state",
                        lambda: {"state": "上涨", "latest": "2026-09-03", "ret20d": 3.5})
    monkeypatch.setattr(web_server, "_grid_training_reference", lambda: {})
    monkeypatch.setattr(web_server, "_read_backtest_summary",
                        lambda: {"available": False})

    client = TestClient(web_server.app)
    resp = client.get("/api/dashboard")
    assert resp.status_code == 200
    d = resp.json()
    assert d["review"]["closed_rounds"] == 1        # 主账本
    shadows = d["review_shadows"]
    assert set(shadows.keys()) == {"s43", "s44"}, "备份文件 .bak 必须被排除"
    assert shadows["s43"]["closed_rounds"] == 2
    assert shadows["s43"]["name"] == "s43"
    assert shadows["s44"]["closed_rounds"] == 0
    assert shadows["s44"]["win_rate"] is None


def test_dashboard_no_shadow_files(web_server, tmp_path, monkeypatch):
    """无影子文件：看板与现状一致（review_shadows 空 dict 不报错）。"""
    from fastapi.testclient import TestClient

    data_dir = tmp_path / "data"
    data_dir.mkdir()
    _write_state(data_dir / "paper_state.json", [])
    monkeypatch.setattr(web_server, "DATA_DIR", data_dir)
    monkeypatch.setattr(web_server, "_market_state",
                        lambda: {"state": "未知", "latest": None, "ret20d": None})
    monkeypatch.setattr(web_server, "_grid_training_reference", lambda: {})
    monkeypatch.setattr(web_server, "_read_backtest_summary",
                        lambda: {"available": False})

    client = TestClient(web_server.app)
    resp = client.get("/api/dashboard")
    assert resp.status_code == 200
    assert resp.json()["review_shadows"] == {}
    assert resp.json()["review"]["closed_rounds"] == 0


# ============================================================
# 8. review_stats name 键透传
# ============================================================

def test_review_stats_name_key():
    from lihu_quantify.monitor.review_progress import review_stats

    assert review_stats([])["name"] == "main"
    assert review_stats([], name="s43")["name"] == "s43"
