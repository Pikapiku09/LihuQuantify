"""第2+3层回归：nogate_half 半仓影子 + 连续风险预算 + 分批建仓（MDD 25% 硬约束）。

对应任务（2026-11-14）：
    任务一 nogate_half：entry_scale 恒 0.5（不看 market_state），与 main 共用信号源，
        独立 paper_state_nogate_half.json、silent 不发日报；不与 nogate（恒 1.0）混淆。
    任务二 gate_risk_budget：连续风险预算（默认 false）；关闭时退回旧三档 0/0.5/1.0。
    任务三 scale_in：分批建仓（默认 false）+ 组合层 MDD 25% 硬守卫
        （outputs/mdd_guard.json + 告警；只减不加，绝不自动平仓）。

三条铁律的测试落点：
    1. 默认关闭 → 闸门逐组合对照旧实现、summary 无新键、不新增任何落盘文件；
    2. 绝不自动平仓 → 分批加仓/MDD 守卫路径无 sell 调用（结构断言 + 端到端卖出数=0）；
    3. market_filter_mode=block 语义不变 → 预算关闭时 block=0.0 且不买入。
"""

from __future__ import annotations

import inspect
import json
from datetime import date
from pathlib import Path
from unittest.mock import MagicMock

import pandas as pd
import pytest
import yaml

from lihu_quantify.config import RiskConfig, ShadowBookConfig, StrategyConfig
from lihu_quantify.execution.oms import OrderManagementSystem
from lihu_quantify.execution.paper_trade import PaperBroker
from lihu_quantify.monitor import scheduler as sched_mod
from lihu_quantify.monitor.alerts import Alerter
from lihu_quantify.monitor.scheduler import (
    MDD_GUARD_POLICY,
    BookSpec,
    DailyScanner,
    _drawdown_metrics,
    _gate_scale,
    _load_risk_budget,
    _load_scale_in,
    _merge_equity_point,
    _risk_budget_coef,
    _scale_in_plan,
)
from lihu_quantify.types import Signal

ROOT = Path(__file__).resolve().parents[1]
LATEST = date(2026, 8, 28)

# 连续风险预算（默认映射，与 settings.yaml 一致）
BUDGET_ON = {"enabled": True, "uptrend": 1.0, "rangebound": 0.7,
             "downtrend": 0.4, "unknown": 0.0}
BUDGET_OFF = dict(BUDGET_ON, enabled=False)

# 分批建仓（默认参数，与 settings.yaml 一致）
SCALE_IN_ON = {"enabled": True, "max_tranches": 2, "tranche_gap_pct": -5.0,
               "tranche_size_ratio": 0.5, "mdd_limit": 0.25}
SCALE_IN_OFF = dict(SCALE_IN_ON, enabled=False)


class _StratCfg:
    """最小策略配置替身（默认 block 模式）。"""

    def __init__(self, mode: str = "block", market_filter: bool = True):
        self.market_filter = market_filter
        self.market_filter_mode = mode


def _legacy_gate_scale(strategy_cfg, market_state: str, sentiment_ebb: bool):
    """改造前 `_gate_scale` 的逐字实现（只到 gate=follow 为止）——对照基准。"""
    block_mode = bool(
        getattr(strategy_cfg, "market_filter", False)
        and market_state != "上涨"
        and getattr(strategy_cfg, "market_filter_mode", "") == "block"
    )
    reduce_scale = 0.5 if (
        getattr(strategy_cfg, "market_filter", False)
        and market_state != "上涨"
        and getattr(strategy_cfg, "market_filter_mode", "") == "reduce"
    ) else 1.0
    if sentiment_ebb:
        block_mode = True
        reduce_scale = 0.0
    return block_mode, (0.0 if block_mode else reduce_scale)


# ============================================================
# 0. 测试基建（stub scanner + 全接线，参照 test_round8_capital_heatmap）
# ============================================================

def _settings(tmp_path: Path) -> "object":
    from lihu_quantify.config import Settings

    (tmp_path / "data").mkdir(exist_ok=True)
    return Settings(
        duckdb_path=str(tmp_path / "t.duckdb"),
        cache_dir=str(tmp_path / "cache"),
        tushare_token="fake-token-for-init-only",
    )


def _make_scanner(tmp_path, monkeypatch, *, init: float = 1_000_000.0,
                  book=None, real_gate: bool = True):
    """stub scanner（绕过 __init__ 的真实 Tushare/DuckDB），返回 (scanner, broker, oms_list)。"""
    broker = PaperBroker(init_capital=init, persist=False,
                         state_file=str(tmp_path / "state.json"))
    s = DailyScanner.__new__(DailyScanner)
    if book is not None:
        s.book = book
    s.broker = broker
    s.alerter = Alerter()
    s.reporter = MagicMock()
    s.reporter.daily_report.return_value = tmp_path / "report.md"
    s.mode = "paper"
    s.settings = MagicMock()

    monkeypatch.setattr(sched_mod, "_ROOT", tmp_path)
    monkeypatch.setattr(sched_mod, "_append_filter_stats", lambda rec: None)

    made_oms: list = []

    def _oms(b=None, registry_file=None):
        inst = OrderManagementSystem(
            b, registry_file=registry_file or str(tmp_path / "stop.json"),
            persist=True)
        made_oms.append(inst)
        return inst

    monkeypatch.setattr(sched_mod, "OrderManagementSystem", _oms)
    if not real_gate:   # 只看成交结果时绕过 8 项闸门（避免 take_profit 等噪声）
        gate = MagicMock()
        gate.check.return_value = MagicMock(approved=True, rejected_items=lambda: [])
        monkeypatch.setattr(sched_mod, "ChecklistGate", lambda **kw: gate)
    return s, broker, made_oms


def _wire_scan(monkeypatch, s, signals_by_code: dict, closes: dict,
               opens: dict | None = None, ma10s: dict | None = None,
               sectors: dict | None = None):
    """接线：client/策略/指标/池 全 stub；closes 同时注入 broker 现价。"""
    opens = opens or {}
    ma10s = ma10s or {}
    sectors = sectors or {}

    def fake_query(api, params=None, use_cache=True):
        code = (params or {}).get("ts_code", "600000.SH")
        dates = pd.date_range(end="20260828", periods=40, freq="D")
        if api == "adj_factor":
            return pd.DataFrame({"ts_code": code,
                                 "trade_date": [d.strftime("%Y%m%d") for d in dates],
                                 "adj_factor": 1.0})
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

    strategy = MagicMock()
    strategy.latest_signal.side_effect = lambda df: signals_by_code.get(
        str(df["ts_code"].iloc[-1]))
    monkeypatch.setattr(sched_mod, "CherryClaw", lambda **kw: strategy)

    def add_std(df):
        code = str(df["ts_code"].iloc[-1])
        close = closes.get(code, 10.0)
        return df.assign(ma10=ma10s.get(code, close * 0.95),
                         vol_ratio=1.0, body_ratio=0.5, ma5_x_ma10=1)

    monkeypatch.setattr(sched_mod, "add_all_standard", add_std)

    codes = list(signals_by_code.keys())
    monkeypatch.setattr(
        s, "_universe",
        lambda n: (codes, {c: sectors.get(c, "") for c in codes},
                   {c: f"股{c[:6]}" for c in codes}))
    for code, close in closes.items():
        s.broker.set_price(code, close)


def _run_scan(s, *, market_state: str = "上涨", market_filter: bool = False,
              mode: str = "block", budget=None, scale_in_cfg=None,
              latest: date = LATEST):
    return s._scan_impl(
        latest, market_state, n=50, days=120,
        s=StrategyConfig(market_filter=market_filter,
                         market_filter_mode=mode),
        r=RiskConfig(), prev_total_asset=None,
        risk_budget=budget, scale_in_cfg=scale_in_cfg,
    )


def _sig(code: str, price: float, pct: float = 0.10) -> Signal:
    return Signal(kind="buy", ts_code=code, suggested_price=price,
                  stop_loss=round(price * 0.9, 4),
                  take_profit=[round(price * 1.1, 4)],
                  suggested_position_pct=pct, reason="测试信号")


def _buys(broker) -> list[dict]:
    return [t for t in broker.trades if t["side"] == "buy"]


def _sells(broker) -> list[dict]:
    return [t for t in broker.trades if t["side"] == "sell"]


# ============================================================
# 1. 默认关闭 → 线上零行为变化（核心证据）
# ============================================================

def test_repo_config_all_new_switches_off_and_block_unchanged():
    """线上 settings.yaml：新开关全 false；market_filter_mode=block 语义不动。"""
    raw = yaml.safe_load((ROOT / "config" / "settings.yaml").read_text(encoding="utf-8"))
    assert raw["gate_risk_budget"]["enabled"] is False
    assert raw["scale_in"]["enabled"] is False
    assert raw["strategy"]["market_filter_mode"] == "block"   # 既定决策，不得改动
    # 影子账本：nogate（恒 1.0）与 nogate_half（恒 0.5）同时存在且语义区分
    ledgers = {x["name"]: x for x in raw["shadow_ledgers"]}
    assert ledgers["nogate"]["market_gate"] == "off"
    assert ledgers["nogate_half"]["market_gate"] == "half"
    books = [x["name"] for x in raw["shadow_books"]]
    assert "nogate_half" in books and "nogate" in books


@pytest.mark.parametrize("mode", ["block", "reduce", "off"])
@pytest.mark.parametrize("state", ["上涨", "下跌", "震荡", "未知"])
@pytest.mark.parametrize("sentiment", [False, True])
@pytest.mark.parametrize("market_filter", [True, False])
def test_gate_scale_identical_to_legacy_when_budget_disabled(
        mode, state, sentiment, market_filter):
    """diff 为空：预算关闭时 _gate_scale 与改造前逐组合完全一致（48 组 × 2 种入参）。

    risk_budget=None → 走真实 settings.yaml（enabled=false）；
    risk_budget=BUDGET_OFF → 显式关闭。两条路径都必须等于旧实现。
    """
    cfg = _StratCfg(mode=mode, market_filter=market_filter)
    expected = _legacy_gate_scale(cfg, state, sentiment)
    assert _gate_scale(BookSpec(), cfg, state, sentiment,
                       risk_budget=None) == expected
    assert _gate_scale(BookSpec(), cfg, state, sentiment,
                       risk_budget=BUDGET_OFF) == expected


def test_default_off_scan_writes_no_new_files_and_no_new_summary_keys(
        tmp_path, monkeypatch):
    """默认关闭全链路：成交口径不变、summary 无新键、不落 equity_curve/mdd_guard/scale_in_state。"""
    s, broker, _ = _make_scanner(tmp_path, monkeypatch, init=1_000_000.0)
    _wire_scan(monkeypatch, s, {"600000.SH": _sig("600000.SH", 10.0)},
               {"600000.SH": 10.0})

    summary = _run_scan(s)   # 市场过滤关闭 → 旧三档 entry_scale=1.0

    assert summary["entry_scale"] == 1.0
    for key in ("risk_budget", "scale_in", "mdd_guard"):
        assert key not in summary, f"开关关闭时 summary 不得新增键 {key}"
    # 旧公式逐位一致：invest = 0.10 × 总资产 × 1.0 → 100000/10 = 10000 股
    buys = _buys(broker)
    assert len(buys) == 1 and buys[0]["volume"] == 10000
    assert buys[0]["price"] == 10.0
    # 新功能落盘文件一个都不能有
    assert not (tmp_path / "outputs" / "mdd_guard.json").exists()
    assert not (tmp_path / "data" / "equity_curve.json").exists()
    assert not (tmp_path / "data" / "scale_in_state.json").exists()
    # 绝不自动平仓：本轮无卖出
    assert _sells(broker) == []


def test_block_mode_semantics_unchanged_when_budget_off(tmp_path, monkeypatch):
    """market_filter_mode=block：非上涨段仍然 0 仓位 + 不买入（既定决策语义）。"""
    s, broker, _ = _make_scanner(tmp_path, monkeypatch)
    _wire_scan(monkeypatch, s, {"600000.SH": _sig("600000.SH", 10.0)},
               {"600000.SH": 10.0})
    summary = _run_scan(s, market_state="下跌", market_filter=True, mode="block")
    assert summary["entry_scale"] == 0.0
    assert _buys(broker) == []
    # 预算关闭时即便传入 BUDGET_OFF 也不能改变 block 语义
    s2, broker2, _ = _make_scanner(tmp_path, monkeypatch)
    _wire_scan(monkeypatch, s2, {"600000.SH": _sig("600000.SH", 10.0)},
               {"600000.SH": 10.0})
    summary2 = _run_scan(s2, market_state="下跌", market_filter=True,
                         mode="block", budget=BUDGET_OFF)
    assert summary2["entry_scale"] == 0.0 and _buys(broker2) == []


def test_disabled_config_blocks_are_inert(tmp_path, monkeypatch):
    """同一输入的 diff 为空：写入"新配置块（全 false）"与完全不写，巡检结果逐项一致。

    证据口径：summary 的实质字段（闸门系数/信号数/成交/拦截/资产/资金效率/
    市场闸门统计）完全相等，且 summary 键集合完全相同（无新增键）。
    """
    def _run(base: Path, with_blocks: bool):
        base.mkdir(parents=True, exist_ok=True)
        if with_blocks:
            (base / "config").mkdir(exist_ok=True)
            (base / "config" / "settings.yaml").write_text(
                "gate_risk_budget:\n"
                "  enabled: false\n"
                "  uptrend: 1.0\n  rangebound: 0.7\n  downtrend: 0.4\n  unknown: 0.0\n"
                "scale_in:\n"
                "  enabled: false\n"
                "  max_tranches: 2\n  tranche_gap_pct: -5\n"
                "  tranche_size_ratio: 0.5\n  mdd_limit: 0.25\n",
                encoding="utf-8")
        s, broker, _ = _make_scanner(base, monkeypatch)
        _wire_scan(monkeypatch, s, {"600000.SH": _sig("600000.SH", 10.0)},
                   {"600000.SH": 10.0})
        summary = _run_scan(s, market_state="下跌", market_filter=True,
                            mode="block")
        keep = ("entry_scale", "signals", "executed", "rejected", "total_asset",
                "cash", "capital", "market_gate", "stale_dropped", "positions")
        return ({k: summary[k] for k in keep},
                list(broker.trades), set(summary.keys()))

    no_blocks = _run(tmp_path / "a", with_blocks=False)
    with_blocks = _run(tmp_path / "b", with_blocks=True)
    assert no_blocks == with_blocks, "新配置块全 false 时不得产生任何行为差异"


# ============================================================
# 2. 任务一：nogate_half 半仓影子
# ============================================================

@pytest.mark.parametrize("state", ["上涨", "下跌", "震荡", "未知"])
@pytest.mark.parametrize("sentiment", [False, True])
def test_half_gate_always_half(state, sentiment):
    """gate=half：任何市场状态/情绪退潮下 entry_scale 恒 0.5 且不禁止开仓。"""
    cfg = _StratCfg(mode="block")
    assert _gate_scale(BookSpec(name="nogate_half", gate="half"),
                       cfg, state, sentiment) == (False, 0.5)
    # 与 nogate（恒 1.0）严格区分
    assert _gate_scale(BookSpec(name="nogate", gate="off"),
                       cfg, state, sentiment) == (False, 1.0)
    # 主账本（follow）仍按 settings 三档
    assert _gate_scale(BookSpec(), cfg, state, sentiment) == \
        _legacy_gate_scale(cfg, state, sentiment)


def test_three_shadow_books_isolated_state_files(tmp_path, monkeypatch):
    """main / nogate / nogate_half 三本账本资金与持仓互不影响、状态文件独立。"""
    from lihu_quantify.execution import paper_trade
    from lihu_quantify.monitor import scheduler as sm

    monkeypatch.setattr(sm, "_ROOT", tmp_path)
    monkeypatch.setattr(paper_trade, "_ROOT", tmp_path)
    st = _settings(tmp_path)
    main = DailyScanner(st, mode="paper")
    nogate = DailyScanner(
        st.model_copy(deep=True), mode="paper",
        book=BookSpec(name="nogate", pool_seed=42, silent=True,
                      strategy="intraday_reversal", gate="off"))
    half = DailyScanner(
        st.model_copy(deep=True), mode="paper",
        book=BookSpec(name="nogate_half", pool_seed=42, silent=True,
                      strategy="intraday_reversal", gate="half"))

    assert main.broker.state_file == str(tmp_path / "data" / "paper_state.json")
    assert nogate.broker.state_file == str(tmp_path / "data" / "paper_state_nogate.json")
    assert half.broker.state_file == \
        str(tmp_path / "data" / "paper_state_nogate_half.json")

    main_asset_before = main.broker.query_asset()
    main.broker.buy("600000.SH", 10.0, 100)
    nogate.broker.buy("600001.SH", 11.0, 100)
    half.broker.buy("600002.SH", 12.0, 200)

    # 资金/持仓互不影响（直接用 positions dict，避免触发真实行情查询）
    assert list(main.broker.positions) == ["600000.SH"]
    assert list(nogate.broker.positions) == ["600001.SH"]
    assert list(half.broker.positions) == ["600002.SH"]
    assert half.broker.positions["600002.SH"].volume == 200
    assert len({main.broker.cash, nogate.broker.cash, half.broker.cash}) == 3, \
        "三本账本现金互不影响"

    def _state(p):
        return json.loads(p.read_text(encoding="utf-8"))

    st_main = _state(tmp_path / "data" / "paper_state.json")
    st_ng = _state(tmp_path / "data" / "paper_state_nogate.json")
    st_half = _state(tmp_path / "data" / "paper_state_nogate_half.json")
    assert set(st_main["positions"]) == {"600000.SH"}
    assert set(st_ng["positions"]) == {"600001.SH"}
    assert set(st_half["positions"]) == {"600002.SH"}
    # 影子 silent：不发日报、邮箱通道不构建
    assert half.book.silent is True and half.alerter.enabled is False
    assert nogate.alerter.enabled is False


def test_half_ledger_never_enters_main_digest(tmp_path, monkeypatch):
    """半仓影子 silent：不进主账本日报的"无闸门影子"一行（只有 gate=off 才进）。"""
    monkeypatch.setattr(sched_mod, "_ROOT", tmp_path)
    (tmp_path / "data").mkdir(parents=True, exist_ok=True)
    (tmp_path / "data" / "shadow_ledger_stats.json").write_text(json.dumps({
        "trade_date": str(LATEST),
        "ledgers": {"nogate_half": {"signals": 9, "executed": 3,
                                    "entry_scale": 0.5, "gate": "half"}},
    }, ensure_ascii=False), encoding="utf-8")
    s = DailyScanner.__new__(DailyScanner)
    assert s._shadow_ledger_digest_line(LATEST) == ""


@pytest.mark.parametrize("market_gate,expected", [("half", "half"), ("reduce", "half")])
def test_setup_scheduler_builds_nogate_half(tmp_path, monkeypatch, market_gate, expected):
    """setup_scheduler：shadow_ledgers.market_gate=half（别名 reduce）→ BookSpec.gate=half。"""
    import apscheduler.schedulers.blocking as blk

    monkeypatch.setattr(sched_mod, "_ROOT", tmp_path)
    (tmp_path / "config").mkdir(exist_ok=True)
    (tmp_path / "config" / "settings.yaml").write_text(
        "shadow_ledgers:\n"
        "  - name: \"nogate_half\"\n"
        "    enabled: true\n"
        "    signal_source: \"main\"\n"
        f"    market_gate: \"{market_gate}\"\n",
        encoding="utf-8",
    )
    st = _settings(tmp_path)
    st.shadow_books = [ShadowBookConfig(name="nogate_half", seed=42,
                                        strategy="cherry_claw")]
    st.main_strategy = "intraday_reversal"
    st.universe.pool_seed = 42

    made: list = []

    class _FakeScanner:
        def __init__(self, settings, mode="paper", book=None):
            self.settings = settings
            self.book = book or BookSpec()
            made.append(self)

    class _FakeSched:
        def add_job(self, *a, **kw):
            pass

        def start(self):
            pass

    orig_cls, orig_blk = sched_mod.DailyScanner, blk.BlockingScheduler
    sched_mod.DailyScanner = _FakeScanner
    blk.BlockingScheduler = lambda *a, **kw: _FakeSched()
    try:
        sched_mod.setup_scheduler(st, mode="paper", n=5)
    finally:
        sched_mod.DailyScanner = orig_cls
        blk.BlockingScheduler = orig_blk

    by_name = {sc.book.name: sc for sc in made}
    assert set(by_name) == {"main", "nogate_half"}
    half = by_name["nogate_half"]
    assert half.book.gate == expected
    assert half.book.silent is True
    assert half.book.strategy == "intraday_reversal"      # 同主账本信号源
    assert half.book.pool_seed == st.universe.pool_seed   # 同主账本池 seed
    assert half.settings.universe.pool_seed == st.universe.pool_seed


# ============================================================
# 3. 任务二：连续风险预算
# ============================================================

def test_risk_budget_maps_market_states():
    """开启后按 market_state 映射连续系数（未知 → unknown 档）。"""
    cfg = _StratCfg(mode="block")
    assert _gate_scale(BookSpec(), cfg, "上涨", risk_budget=BUDGET_ON) == (False, 1.0)
    assert _gate_scale(BookSpec(), cfg, "震荡", risk_budget=BUDGET_ON) == (False, 0.7)
    assert _gate_scale(BookSpec(), cfg, "下跌", risk_budget=BUDGET_ON) == (False, 0.4)
    assert _gate_scale(BookSpec(), cfg, "未知", risk_budget=BUDGET_ON) == (False, 0.0)
    # reduce 模式下预算同样接管（否则会得到 0.5）
    assert _gate_scale(BookSpec(), _StratCfg(mode="reduce"), "下跌",
                       risk_budget=BUDGET_ON) == (False, 0.4)
    # 未知状态一律 unknown 档
    assert _risk_budget_coef(BUDGET_ON, "别的状态") == 0.0


def test_risk_budget_sentiment_still_blocks():
    """P6 情绪退潮独立于预算：退潮日仍禁开仓（block_mode=True → 0.0）。"""
    assert _gate_scale(BookSpec(), _StratCfg(), "上涨", sentiment_ebb=True,
                       risk_budget=BUDGET_ON) == (True, 0.0)


def test_risk_budget_ignored_when_market_filter_off():
    """market_filter=false（闸门整体关闭）→ 预算块不生效，仍是 1.0。"""
    cfg = _StratCfg(mode="block", market_filter=False)
    assert _gate_scale(BookSpec(), cfg, "下跌", risk_budget=BUDGET_ON) == (False, 1.0)


def test_risk_budget_respects_book_gate_overrides():
    """影子账本恒定系数优先于预算（half=0.5 / off=1.0 不被预算改写）。"""
    cfg = _StratCfg()
    assert _gate_scale(BookSpec(name="nogate_half", gate="half"), cfg, "下跌",
                       risk_budget=BUDGET_ON) == (False, 0.5)
    assert _gate_scale(BookSpec(name="nogate", gate="off"), cfg, "下跌",
                       risk_budget=BUDGET_ON) == (False, 1.0)


def test_load_risk_budget_yaml_and_clamp(tmp_path):
    p = tmp_path / "settings.yaml"
    p.write_text(
        "gate_risk_budget:\n"
        "  enabled: true\n"
        "  uptrend: 1.2\n"        # 上界裁剪 → 1.0
        "  rangebound: 0.6\n"
        "  downtrend: -1\n"       # 下界裁剪 → 0.0
        "  unknown: 0.1\n",
        encoding="utf-8")
    out = _load_risk_budget(p)
    assert out == {"enabled": True, "uptrend": 1.0, "rangebound": 0.6,
                   "downtrend": 0.0, "unknown": 0.1}
    # 缺块 → 默认（关闭）
    assert _load_risk_budget(tmp_path / "missing.yaml")["enabled"] is False


def test_scan_impl_applies_continuous_budget(tmp_path, monkeypatch):
    """端到端：预算开启 → entry_scale=0.7，买入股数按 0.7 折扣计算。"""
    s, broker, _ = _make_scanner(tmp_path, monkeypatch, init=1_000_000.0,
                                 real_gate=False)
    _wire_scan(monkeypatch, s, {"600000.SH": _sig("600000.SH", 10.0)},
               {"600000.SH": 10.0})
    summary = _run_scan(s, market_state="震荡", market_filter=True,
                        mode="block", budget=BUDGET_ON)
    assert summary["entry_scale"] == 0.7
    assert summary["risk_budget"]["coef"] == 0.7
    assert summary["risk_budget"]["map"]["rangebound"] == 0.7
    buys = _buys(broker)
    assert len(buys) == 1
    assert buys[0]["volume"] == 7000   # 0.10 × 1,000,000 × 0.7 / 10 = 7000 股
    assert summary["entry_scale"] == summary["risk_budget"]["coef"]


# ============================================================
# 4. 任务三：分批建仓 + MDD 25% 硬守卫
# ============================================================

def test_scale_in_plan_pure_function():
    """触发条件：较首笔成本再跌 gap% 才加仓；批数/开关/比例全部生效。"""
    st = {"first_cost": 10.0, "first_notional": 10_000.0, "tranches": 1}
    assert _scale_in_plan(9.6, 10.0, 1000, st, SCALE_IN_ON) is None      # 跌 4%，不触发
    plan = _scale_in_plan(9.5, 10.0, 1000, st, SCALE_IN_ON)              # 恰好 -5%
    assert plan is not None and plan["tranche"] == 2
    assert plan["notional"] == 5000.0                                    # 首笔 10000 × 0.5
    assert _scale_in_plan(9.5, 10.0, 1000, st, SCALE_IN_OFF) is None      # 开关关闭
    assert _scale_in_plan(9.5, 10.0, 1000, {**st, "tranches": 2},
                          SCALE_IN_ON) is None                           # 已满 2 批
    assert _scale_in_plan(9.5, 10.0, 1000, {"first_cost": 10.0, "tranches": 1},
                          SCALE_IN_ON)["notional"] == 5000.0             # 存量仓按 cost×vol 兜底
    # 正/负写法归一（配置层）：-5 ↔ 5 都表示"跌 5%"
    assert _load_scale_in()["tranche_gap_pct"] == -5.0


def test_load_scale_in_normalizes(tmp_path):
    p = tmp_path / "settings.yaml"
    p.write_text(
        "scale_in:\n"
        "  enabled: true\n"
        "  max_tranches: 0\n"        # 下限裁剪 → 1
        "  tranche_gap_pct: 5\n"     # 正数写法 → -5
        "  tranche_size_ratio: 1.5\n"  # 上限裁剪 → 1.0
        "  mdd_limit: 25\n",         # 百分数 → 0.25
        encoding="utf-8")
    out = _load_scale_in(p)
    assert out["enabled"] is True
    assert out["max_tranches"] == 1
    assert out["tranche_gap_pct"] == -5.0
    assert out["tranche_size_ratio"] == 1.0
    assert out["mdd_limit"] == 0.25
    # 缺块 → 默认关闭
    default = _load_scale_in(tmp_path / "missing.yaml")
    assert default["enabled"] is False
    assert default["max_tranches"] == 2 and default["mdd_limit"] == 0.25


def _seed_position(broker, code: str, price: float, volume: int):
    """在"前一交易日"建仓（成本=price），返回 (cost, notional)。"""
    broker.on_new_day(date(2026, 8, 27))
    res = broker.buy(code, price, volume)
    assert res.success
    return price, price * volume


def test_scale_in_adds_on_dip_end_to_end(tmp_path, monkeypatch):
    """端到端：持仓跌 6% + 当日信号 → 按 0.5 比例加仓，止损合并登记，无卖出。"""
    s, broker, made_oms = _make_scanner(tmp_path, monkeypatch, init=1_000_000.0)
    _seed_position(broker, "600000.SH", 10.0, 5000)   # 首笔 50,000（存量仓，1 批）
    _wire_scan(monkeypatch, s,
               {"600000.SH": _sig("600000.SH", 9.4)},
               {"600000.SH": 9.4}, ma10s={"600000.SH": 9.7})

    summary = _run_scan(s, scale_in_cfg=SCALE_IN_ON)

    assert len(_buys(broker)) == 2, "应发生一次加仓买入"
    add = _buys(broker)[-1]
    assert add["ts_code"] == "600000.SH" and add["price"] == 9.4
    assert add["volume"] == 2600, "本批金额 50000×0.5=25000 / 9.4 → 2600 股"
    # 状态落盘：批数=2、首笔成本保留
    st = json.loads((tmp_path / "data" / "scale_in_state.json").read_text(encoding="utf-8"))
    assert st["positions"]["600000.SH"]["tranches"] == 2
    assert st["positions"]["600000.SH"]["first_cost"] == 10.0
    # summary 与报告口径
    assert summary["scale_in"]["adds"][0]["tranche"] == 2
    assert summary["executed"][-1]["scale_in"] is True
    # 止损合并登记（旧仓 9.2 + 新仓 8.46 → 取更保守价、累计股数）
    stop = made_oms[-1].stop_registry["600000.SH"]
    assert stop.volume == 7600
    assert stop.stop_price == pytest.approx(8.46)
    # 铁律：绝不自动平仓
    assert _sells(broker) == []
    assert summary["sells_today"] == []


def test_scale_in_skips_when_no_dip_or_tranches_exhausted(tmp_path, monkeypatch):
    """未达 -5% 或批数已满 → 不加仓；且不写 scale_in_state（无成交无状态变更）。"""
    # 情形1：仅跌 2%（未触发）
    s, broker, _ = _make_scanner(tmp_path, monkeypatch, init=1_000_000.0)
    _seed_position(broker, "600000.SH", 10.0, 5000)
    _wire_scan(monkeypatch, s, {"600000.SH": _sig("600000.SH", 9.8)},
               {"600000.SH": 9.8})
    _run_scan(s, scale_in_cfg=SCALE_IN_ON)
    assert len(_buys(broker)) == 1, "未达触发线不得加仓"

    # 情形2：已达 2 批（状态文件预置）
    data = tmp_path / "data"
    data.mkdir(parents=True, exist_ok=True)
    (data / "scale_in_state.json").write_text(json.dumps({
        "positions": {"600000.SH": {"first_cost": 10.0,
                                    "first_notional": 50000.0,
                                    "tranches": 2}}}), encoding="utf-8")
    s2, broker2, _ = _make_scanner(tmp_path, monkeypatch, init=1_000_000.0)
    _seed_position(broker2, "600000.SH", 10.0, 5000)
    _wire_scan(monkeypatch, s2, {"600000.SH": _sig("600000.SH", 9.0)},
               {"600000.SH": 9.0})
    _run_scan(s2, scale_in_cfg=SCALE_IN_ON)
    assert len(_buys(broker2)) == 1, "批数已满不得再加仓"


def test_scale_in_respects_single_position_cap(tmp_path, monkeypatch):
    """加仓不得突破单票 25% 上限：已被放到 24% 的持仓，加仓额度被裁剪到剩余空间内。"""
    s, broker, _ = _make_scanner(tmp_path, monkeypatch, init=1_000_000.0)
    _seed_position(broker, "600000.SH", 10.0, 24000)   # 240,000 = 24%
    _wire_scan(monkeypatch, s, {"600000.SH": _sig("600000.SH", 9.4)},
               {"600000.SH": 9.4}, ma10s={"600000.SH": 9.7})

    _run_scan(s, scale_in_cfg=SCALE_IN_ON)

    pos = broker.positions["600000.SH"]
    total_after = broker.query_asset()["total_asset"]
    weight = pos.volume * 9.4 / total_after
    assert weight <= 0.25 + 1e-9, f"加仓后单票权重 {weight:.2%} 超过 25%"
    assert pos.volume > 24000, "剩余空间内应仍然加仓（不是一刀切跳过）"
    # 若未裁剪：50% × 24000×10 = 120,000 / 9.4 → 12700 股；裁剪后明显更少
    assert pos.volume - 24000 < 12700


@pytest.mark.parametrize("total_asset,expected", [
    (75_000.0, True),     # 峰 100k → 当前 75k = 恰好 25% → 触发
    (75_100.0, False),    # 24.9% → 未触及
    (90_000.0, False),
])
def test_mdd_guard_thresholds(tmp_path, monkeypatch, total_asset, expected):
    """MDD 守卫在 25% 处准确触发（24.9% 不触发）。"""
    s, broker, _ = _make_scanner(tmp_path, monkeypatch, init=total_asset)
    _wire_scan(monkeypatch, s, {"600000.SH": _sig("600000.SH", 10.0)},
               {"600000.SH": 10.0})
    _wire_curve(tmp_path, [("2026-08-20", 100_000.0)])

    summary = _run_scan(s, scale_in_cfg=SCALE_IN_ON)

    payload = json.loads((tmp_path / "outputs" / "mdd_guard.json").read_text(
        encoding="utf-8"))
    assert payload["triggered"] is expected
    assert payload["mdd_limit"] == 0.25 and payload["mdd_limit_pct"] == "25%"
    assert summary["mdd_guard"]["triggered"] is expected
    # 与守卫一致：触发则不买入，未触发则正常买入
    assert (len(_buys(broker)) == 0) is expected
    if expected:
        assert any(r["name"] == "MDD 守卫" for r in summary["rejected"])
        assert any("MDD 守卫" in a["title"] for a in s.alerter.history)
    else:
        assert not any("MDD 守卫" in a["title"] for a in s.alerter.history)


def test_mdd_guard_seeds_from_init_capital(tmp_path, monkeypatch):
    """首次启用（无权益曲线）：以初始资金为高水位锚点，识别"启用前已发生"的回撤。

    否则曲线从启用日才开始，账户已从 10 万跌到 7.2 万的既有回撤会被漏判。
    """
    s, broker, _ = _make_scanner(tmp_path, monkeypatch, init=100_000.0)
    _seed_position(broker, "600000.SH", 10.0, 7000)     # 成本 7 万
    # 持仓票现价 6（-40%）→ 总资产 ≈ 7.2 万；信号票为另一只（现价 10）
    _wire_scan(monkeypatch, s, {"600000.SZ": _sig("600000.SZ", 10.0)},
               {"600000.SZ": 10.0, "600000.SH": 6.0})
    n_buys_before = len(_buys(broker))

    summary = _run_scan(s, scale_in_cfg=SCALE_IN_ON)

    payload = json.loads((tmp_path / "outputs" / "mdd_guard.json").read_text(
        encoding="utf-8"))
    total = broker.query_asset()["total_asset"]
    assert payload["seeded_from_init_capital"] is True
    assert payload["init_capital"] == 100_000.0
    assert payload["mdd"] == pytest.approx(1 - total / 100_000.0, abs=1e-6)
    assert payload["mdd"] > 0.25
    assert payload["triggered"] is True
    assert len(_buys(broker)) == n_buys_before        # 守卫阻断买入
    assert _sells(broker) == []                       # 绝不自动平仓
    assert summary["mdd_guard"]["triggered"] is True


def _wire_curve(tmp_path, points):
    data = tmp_path / "data"
    data.mkdir(parents=True, exist_ok=True)
    (data / "equity_curve.json").write_text(json.dumps({
        "points": [{"date": d, "total_asset": v} for d, v in points],
    }), encoding="utf-8")


def test_mdd_guard_exact_25_percent_and_no_auto_close(tmp_path, monkeypatch):
    """25% 触发时：写审计文件 + 告警 + 一个买单都不下 + 不卖出任何持仓。"""
    s, broker, _ = _make_scanner(tmp_path, monkeypatch, init=75_000.0)
    _seed_position(broker, "600000.SH", 10.0, 1000)   # 已有持仓 1 万
    n_buys_before = len(_buys(broker))
    _wire_scan(monkeypatch, s, {"600000.SZ": _sig("600000.SZ", 10.0)},
               {"600000.SZ": 10.0, "600000.SH": 10.0})
    _wire_curve(tmp_path, [("2026-08-20", 100_000.0)])

    summary = _run_scan(s, scale_in_cfg=SCALE_IN_ON)

    payload = json.loads((tmp_path / "outputs" / "mdd_guard.json").read_text(
        encoding="utf-8"))
    total_now = broker.query_asset()["total_asset"]
    expected_mdd = 1.0 - total_now / 100_000.0   # 已有持仓的买入佣金会让回撤略高于 25%
    assert payload["triggered"] is True
    assert payload["mdd"] == pytest.approx(expected_mdd, abs=1e-6)
    assert payload["mdd"] >= 0.25
    assert payload["mdd_pct"] == f"{expected_mdd:.2%}"
    assert payload["policy"] == MDD_GUARD_POLICY
    assert payload["policy"]["auto_close_position"] is False
    assert payload["book"] == "main"
    # 告警推送
    assert any("MDD 守卫" in a["title"] and a["level"] == "error"
               for a in s.alerter.history)
    # 不买入（含新仓与加仓）——守卫触发后本轮买单数为 0
    assert len(_buys(broker)) == n_buys_before
    # 绝不自动平仓
    assert _sells(broker) == []
    # 权益曲线追加当日点（峰不动 → 回撤保持）
    curve = json.loads((tmp_path / "data" / "equity_curve.json").read_text(encoding="utf-8"))
    assert len(curve["points"]) == 2
    assert curve["points"][-1] == {"date": str(LATEST), "total_asset": broker.query_asset()["total_asset"]}
    # summary 同时登记
    assert summary["scale_in"]["adds"] == []


def test_mdd_guard_alert_once_per_trigger_state(tmp_path, monkeypatch):
    """同一触发状态不重复告警（首次触发告警一次；同日重跑幂等）。"""
    s, broker, _ = _make_scanner(tmp_path, monkeypatch, init=75_000.0)
    _wire_scan(monkeypatch, s, {"600000.SH": _sig("600000.SH", 10.0)},
               {"600000.SH": 10.0})
    _wire_curve(tmp_path, [("2026-08-20", 100_000.0)])

    for _ in range(2):
        _run_scan(s, market_state="下跌", market_filter=True, mode="block",
                  scale_in_cfg=SCALE_IN_ON)

    alerts = [a for a in s.alerter.history if "MDD 守卫" in a["title"]]
    assert len(alerts) == 1, f"重复告警 {len(alerts)} 次"
    curve = json.loads((tmp_path / "data" / "equity_curve.json").read_text(encoding="utf-8"))
    assert len(curve["points"]) == 2, "同日权益点幂等覆盖，不得重复追加"


def test_drawdown_helpers_exact():
    """回撤纯函数：峰值/当前/最大回撤口径。"""
    m = _drawdown_metrics([
        {"date": "2026-01-03", "total_asset": 75_000},
        {"date": "2026-01-01", "total_asset": 80_000},
        {"date": "2026-01-02", "total_asset": 100_000},
    ])
    assert m["peak"] == 100_000 and m["peak_date"] == "2026-01-02"
    assert m["max_drawdown"] == pytest.approx(0.25)
    assert m["current_drawdown"] == pytest.approx(0.25)
    assert m["trough_date"] == "2026-01-03"
    assert _drawdown_metrics([])["max_drawdown"] is None
    assert _drawdown_metrics([{"date": "x", "total_asset": 0}])["max_drawdown"] is None
    # 同日覆盖（幂等）
    assert _merge_equity_point(
        [{"date": "2026-01-01", "total_asset": 1.0}], "2026-01-01", 2.0
    ) == [{"date": "2026-01-01", "total_asset": 2.0}]


# ============================================================
# 5. 结构边界：只买不卖 / 默认关闭时无副作用
# ============================================================

def test_scale_in_and_guard_never_sell():
    """结构断言：分批建仓/MDD 守卫全路径无卖出调用，且 policy 明确不自动平仓。"""
    for fn in (DailyScanner._place_scale_in_buy, DailyScanner._scale_in_candidate,
               DailyScanner._mdd_guard):
        src = inspect.getsource(fn)
        for forbidden in (".sell(", "broker.sell", "auto_close"):
            assert forbidden not in src, f"{fn.__name__} 不得出现 {forbidden}"
    src = inspect.getsource(sched_mod._scale_in_plan)
    assert ".sell(" not in src
    assert MDD_GUARD_POLICY["auto_close_position"] is False
    assert MDD_GUARD_POLICY["auto_reduce_position"] is False
    assert MDD_GUARD_POLICY["auto_modify_config"] is False


def test_scan_impl_off_path_does_not_read_scale_in_files(tmp_path, monkeypatch):
    """关闭时连状态文件都不读（预置脏状态也不影响成交口径）。"""
    data = tmp_path / "data"
    data.mkdir(parents=True, exist_ok=True)
    (data / "scale_in_state.json").write_text(json.dumps({
        "positions": {"600000.SH": {"first_cost": 999.0, "first_notional": 1e9,
                                    "tranches": 1}}}), encoding="utf-8")
    (data / "equity_curve.json").write_text(json.dumps({
        "points": [{"date": "2026-08-20", "total_asset": 100_000.0}]}),
        encoding="utf-8")

    s, broker, _ = _make_scanner(tmp_path, monkeypatch, init=50_000.0)
    _wire_scan(monkeypatch, s, {"600000.SH": _sig("600000.SH", 10.0)},
               {"600000.SH": 10.0})

    summary = _run_scan(s)   # 默认关闭：即便曲线显示 -50%，也不得触发守卫

    assert len(_buys(broker)) == 1
    assert "mdd_guard" not in summary
    assert not (tmp_path / "outputs" / "mdd_guard.json").exists()
